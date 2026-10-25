"""运行记录：一轮里落成的每一处改动 + 每处的 baseline + 起止时间 + 最终验证结论。

为什么要有它（真实经历，不是设想）：用户**接受了一份「最终验证未通过」的改动**，
事后反悔，用系统外的手段（自己找备份 / 编辑器）把它退回去——而 `spool revert`
明明存在。三个缺口里最要命的是**粒度**：一轮里常常改 N 处（多步 replace_lines），
`revert` 只认「上一次」，要回退整轮得反复 revert，还得自己理清顺序。

这份记录补的就是这件事：`.agent/runs/<id>/run.json`。它**不另造一套回滚**——
每处的 baseline 就是 `tools/edit.py` 里那套 `BaselineEntry`，回退时逐处调用
同一个 `revert()`；本模块只负责回答「哪一轮、按什么顺序退、退得干不干净」。

三条硬要求写在这个文件里：

1. **按改动逆序**逐处回退。同一路径可能在一轮里被改多次（后一处改的是前一处
   刚写下的内容），顺序反了就会把中间态当成原文写回去。
2. **冲突不静默**：某一处落盘之后又被别人改过（当前内容指纹与记录里的
   「落盘后指纹」不符）→ 拒绝覆盖它，逐条列出冲突路径并说明人工怎么处理。
3. **删除过的文件要能被恢复**：删除那一处的 baseline 存的是**删除前的全文**，
   回退把它原样写回磁盘，而不是留一个空文件。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

# 运行记录放在 `.agent/runs/<id>/run.json`。
RUNS_DIR = "runs"
RECORD_NAME = "run.json"

# 「这个路径上现在什么都没有」也是一个必须能被指纹表达的状态：拿空串冒充它，
# 会把「刚被删掉」与「刚被写成空文件」混成一条，而回退的动作正好相反。
ABSENT = "<不存在>"

# 回退结果里的三种状态。用常量而不是散落的字面量：报告、退出码与写回记录的
# 状态位都读它们，字面量写散了迟早会分叉。
DONE = "done"
SKIPPED = "skipped"
CONFLICT = "conflict"
FAILED = "failed"

# 状态 → 报告里的动词。
_VERB = {
    DONE: "已恢复",
    SKIPPED: "跳过",
    CONFLICT: "冲突",
    FAILED: "失败",
}


def fingerprint(text: str | None) -> str:
    """内容指纹：sha1 前 12 位；`None` 表示这个路径上什么都没有。

    只用于「还是不是当初那一份」这个判断，所以不需要行数——行数会随
    换行风格变化，那会让一次无害的规范化被报成冲突。
    """
    if text is None:
        return ABSENT
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def now_iso(now: float | None = None) -> str:
    stamp = time.localtime(time.time() if now is None else now)
    return time.strftime("%Y-%m-%dT%H:%M:%S", stamp)


def new_run_id(now: float | None = None) -> str:
    """运行 id：`20261004-183012-473921`（最后一段是**微秒**）。

    可读的时间前缀是刻意的：`--last-run` 按名字排序就等于按时间排序，而人在
    报告里看到的那个 id 本身就能读出「这是哪一天哪一次」。

    精度必须到微秒：一次运行里可能落很多轮（按步执行那条路每步一份记录），
    同一秒里建出来的两个 id 只差后缀名时，「名字排序」就不再等于时间排序——
    而 `--last-run` 挑错一轮就是退错一批文件。
    """
    moment = time.time() if now is None else now
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(moment))
    micros = int(round((moment - int(moment)) * 1_000_000)) % 1_000_000
    return f"{stamp}-{micros:06d}"


@dataclass(frozen=True)
class RunEntry:
    """一轮里落成的**一处**改动：改动前的样子 + 落盘后的指纹。

    `old_text` / `existed` 就是既有 `BaselineEntry` 的两位，刻意不另起一套：
    回退时把它们装回 `Baseline` 交给 `revert()`，走的还是同一条回滚路径。
    """

    path: str
    old_text: str | None
    existed: bool
    deleted: bool
    after: str
    declared: str = ""
    # 回退时写回的状态（空串 = 还没退过）。
    status: str = ""

    @property
    def lines(self) -> int:
        return len((self.old_text or "").splitlines())

    def _baseline_entry(self):
        """装回既有的 `BaselineEntry`——回退走的是同一个 `revert()`。"""
        from spoolkit.tools.edit import BaselineEntry

        return BaselineEntry(path=self.path, old_text=self.old_text, existed=self.existed)

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "old_text": self.old_text,
            "existed": self.existed,
            "deleted": self.deleted,
            "after": self.after,
            "declared": self.declared,
            "status": self.status,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "RunEntry":
        return cls(
            path=str(payload["path"]),
            old_text=payload.get("old_text"),
            existed=bool(payload.get("existed", payload.get("old_text") is not None)),
            deleted=bool(payload.get("deleted", False)),
            after=str(payload.get("after", "")),
            declared=str(payload.get("declared", "")),
            status=str(payload.get("status", "")),
        )


@dataclass(frozen=True)
class RunRound:
    """一轮落盘。一个进程里可能落很多轮（按步执行那条路每步都落一次）。"""

    index: int
    at: str
    entries: tuple[RunEntry, ...]

    def to_json(self) -> dict:
        return {
            "index": self.index,
            "at": self.at,
            "entries": [entry.to_json() for entry in self.entries],
        }

    @classmethod
    def from_json(cls, payload: dict) -> "RunRound":
        return cls(
            index=int(payload.get("index", 0)),
            at=str(payload.get("at", "")),
            entries=tuple(
                RunEntry.from_json(item) for item in payload.get("entries", [])
            ),
        )


@dataclass
class RunRecord:
    """一次运行的记录。

    `verification` 是**最终验证结论**：`{"passed": bool, "summary": str}`。
    它是这份记录存在的第三个理由——收尾报告要把「最终验证：未通过」摆到最前，
    而那句话的出处就是这里。
    """

    run_id: str
    session: str = ""
    goal: str = ""
    started_at: str = ""
    finished_at: str = ""
    verification: dict | None = None
    rounds: list[RunRound] = field(default_factory=list)
    last_reverted_at: str = ""

    def entries(self) -> list[RunEntry]:
        """按**落盘顺序**展开全部改动（回退时倒着用）。"""
        return [entry for item in self.rounds for entry in item.entries]

    def to_json(self) -> dict:
        return {
            "id": self.run_id,
            "session": self.session,
            "goal": self.goal,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "verification": self.verification,
            "last_reverted_at": self.last_reverted_at,
            "rounds": [item.to_json() for item in self.rounds],
        }

    @classmethod
    def from_json(cls, payload: dict) -> "RunRecord":
        return cls(
            run_id=str(payload.get("id", "")),
            session=str(payload.get("session", "")),
            goal=str(payload.get("goal", "")),
            started_at=str(payload.get("started_at", "")),
            finished_at=str(payload.get("finished_at", "")),
            verification=payload.get("verification"),
            rounds=[RunRound.from_json(item) for item in payload.get("rounds", [])],
            last_reverted_at=str(payload.get("last_reverted_at", "")),
        )


def take_run_id(holder) -> str:
    """一次入口（一次 CLI 调用）= **一轮**：同一个进程里的每一次落盘都记进同一个 id。

    为什么 id 不是「每个待确认清单一个」：按步执行那条路每一步都会新建一个
    待确认清单，而用户眼里的「这一轮」是**整次调用**——他反悔时想退掉的是刚才
    那一整批，不是最后一步。id 定在入口这一层，落盘按轮追加进同一份记录，
    于是 `spool revert --run <id>` 退的是整次调用，顺序仍然是逆序。

    `holder` 只需要是一个能挂属性的东西（argparse 的 Namespace 就是）。
    """
    current = str(getattr(holder, "run_id", "") or "")
    if current:
        return current
    created = new_run_id()
    try:
        holder.run_id = created
    except AttributeError:  # pragma: no cover - 只读 holder（测试里的替身）
        return created
    return created


def runs_root(root: Path) -> Path:
    return Path(root) / ".agent" / RUNS_DIR


def run_dir(root: Path, run_id: str) -> Path:
    # 运行 id 直接当目录名，所以把路径分隔符挡掉：一个带 `..` 的 id 能让
    # 「回退」去写记录目录之外的东西。
    safe = PurePosixPath(str(run_id).replace("\\", "/")).name
    return runs_root(root) / safe


def record_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / RECORD_NAME


def load_run(root: Path, run_id: str) -> RunRecord | None:
    path = record_path(root, run_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return RunRecord.from_json(payload)


def save_run(root: Path, record: RunRecord) -> Path:
    path = record_path(root, record.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def run_ids(root: Path) -> list[str]:
    """现有的运行 id，**按时间倒序**（最新的在前）。

    排序键是名字：id 前缀 `%Y%m%d-%H%M%S` 加微秒，字典序等于时间序。
    """
    base = runs_root(root)
    if not base.is_dir():
        return []
    return sorted(
        (
            item.name
            for item in base.iterdir()
            if item.is_dir() and (item / RECORD_NAME).is_file()
        ),
        reverse=True,
    )


def latest_run_id(root: Path) -> str | None:
    found = run_ids(root)
    return found[0] if found else None


def append_landing(
    root: Path,
    run_id: str,
    entries,
    *,
    session: str = "",
    goal: str = "",
    started_at: str = "",
    verification: tuple[bool, str] | None = None,
) -> Path:
    """把这一轮落成的改动追加进运行记录（没有就建一份）。

    验证结论与起止时间**每次都刷新**：一次运行的最后一次验证就是收尾报告要
    引用的那一次，而它必然发生在最后一次落盘之后。
    """
    record = load_run(root, run_id) or RunRecord(run_id=run_id)
    record.session = session or record.session
    record.goal = goal or record.goal
    record.started_at = record.started_at or started_at or now_iso()
    record.finished_at = now_iso()
    if verification is not None:
        passed, summary = verification
        record.verification = {"passed": bool(passed), "summary": str(summary)}
    record.rounds.append(
        RunRound(
            index=len(record.rounds) + 1,
            at=now_iso(),
            entries=tuple(entries),
        )
    )
    return save_run(root, record)


# 状态 → 报告里的动词。
_VERB = {
    DONE: "已恢复",
    SKIPPED: "跳过",
    CONFLICT: "冲突",
    FAILED: "失败",
}


@dataclass(frozen=True)
class UndoStep:
    """一处改动的回退结果。逐处报告读的就是它。"""

    path: str
    status: str
    detail: str
    # 预演时的动词（「会恢复」/「会删除」）。空串表示按 status 取常态动词。
    verb: str = ""

    def render(self) -> str:
        verb = self.verb or _VERB.get(self.status, self.status)
        return f"{verb}：{self.path}——{self.detail}" if self.detail else f"{verb}：{self.path}"


@dataclass(frozen=True)
class UndoResult:
    run_id: str
    dry_run: bool
    steps: list[UndoStep]

    def count(self, status: str) -> int:
        return sum(1 for step in self.steps if step.status == status)

    @property
    def conflicts(self) -> list[UndoStep]:
        return [step for step in self.steps if step.status == CONFLICT]

    @property
    def changed(self) -> bool:
        return self.count(DONE) > 0


def _read_or_missing(target: Path) -> tuple[str | None, str]:
    """磁盘上现在的内容：返回（内容 或 None 表示不存在, 读不出来的原因）。"""
    if not target.exists():
        return None, ""
    try:
        return target.read_text(encoding="utf-8"), ""
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{type(exc).__name__}"


def _undo_one(
    root: Path, entry: RunEntry, dry_run: bool, sim: dict[str, str | None]
) -> UndoStep:
    """退一处。**先比指纹再动手**，对不上就拒绝覆盖。

    走的是既有的 `revert(root, Baseline(entries=(...)))`——一处一个基线，
    也就是「只退这一处」。删除那一处靠同一段代码把全文写回去。

    预演时用 `sim` 记账：同一份内容在一轮里被改过多次时，**前面那一处该退到
    什么，取决于后面那一处退完之后长什么样**。不模拟这一步，预演会把「还没轮
    到它退」误报成冲突——而那正好把用户吓退。
    """
    from spoolkit.tools.edit import Baseline, BaselineEntry, revert

    target = (
        Path(entry.path) if Path(entry.path).is_absolute() else root / entry.path
    )
    key = str(target)
    if dry_run and key in sim:
        current, unreadable = sim[key], ""
    else:
        current, unreadable = _read_or_missing(target)
    if unreadable:
        return UndoStep(
            entry.path,
            CONFLICT,
            f"现在读不出来（{unreadable}），不敢覆盖——请人工确认它是什么之后自己处理",
        )

    if fingerprint(current) != entry.after:
        # 内容已经是「改动前」的样子：多半是上一次回退退到这里就断了，
        # 或者人自己先改回来了。这一处不需要再退，如实说，不报成冲突。
        if current is None and not entry.existed:
            return UndoStep(entry.path, SKIPPED, "它本来就不存在，不需要再退")
        if entry.existed and current == entry.old_text:
            return UndoStep(entry.path, SKIPPED, "已经是改动前的样子，不需要再退")
        return UndoStep(
            entry.path,
            CONFLICT,
            f"这一轮落盘之后它又被改过（现在是 {fingerprint(current)}，"
            f"记录里落盘后的指纹是 {entry.after}），拒绝覆盖",
        )

    if dry_run:
        sim[key] = entry.old_text if entry.existed else None
        return UndoStep(
            entry.path,
            DONE,
            _would_do(entry),
            verb="会删除" if not entry.existed else "会恢复",
        )

    try:
        revert(root, Baseline(entries=(entry._baseline_entry(),)))
    except OSError as exc:  # pragma: no cover - 磁盘满/权限这类环境问题
        return UndoStep(entry.path, FAILED, f"{type(exc).__name__}: {exc}")
    return UndoStep(entry.path, DONE, _did_do(entry))


def _would_do(entry: RunEntry) -> str:
    if not entry.existed:
        return "会删除（它是这一轮新建的）"
    return f"会把改动前的全文写回去（{entry.lines} 行）"


def _did_do(entry: RunEntry) -> str:
    if not entry.existed:
        return "它是这一轮新建的，已删除"
    return f"改动前的全文已写回（{entry.lines} 行）"


def revert_run(
    root: Path, run_id: str, *, dry_run: bool = False
) -> UndoResult | None:
    """回退一整轮（`run_id` 指定的那次运行），逐处报告结果。

    顺序是**改动逆序**：同一路径在一轮里被改过多次时，必须先把后一处退掉，
    前一处才有它当初落盘时的那份内容可退。状态会写回记录，所以重复调用
    不会重复恢复同一批文件（也就不会冲掉之后的手工改动）。
    """
    record = load_run(root, run_id)
    if record is None:
        return None

    steps: list[UndoStep] = []
    # 倒着退：先按轮倒序，轮内再倒序。
    ordered: list[RunEntry] = []
    for item in reversed(record.rounds):
        ordered.extend(reversed(item.entries))

    # 预演用的模拟状态（路径 → 退到这一步之后的内容）。真退时用不到它。
    sim: dict[str, str | None] = {}
    for entry in ordered:
        if entry.status == DONE:
            steps.append(
                UndoStep(entry.path, SKIPPED, "上一次回退已经退过它了，不重复动手")
            )
            continue
        steps.append(_undo_one(root, entry, dry_run, sim))

    if not dry_run:
        save_run(root, _with_statuses(record, ordered, steps))

    return UndoResult(run_id=run_id, dry_run=dry_run, steps=steps)


def _with_statuses(
    record: RunRecord, ordered: list[RunEntry], steps: list[UndoStep]
) -> RunRecord:
    """把这次的回退状态贴回记录（位置对位置，不靠内容去认）。

    为什么按位置而不是按 (路径, 指纹)：同一路径在同一轮里只会出现一次，
    但**跨轮**会出现多次且内容可以完全相同——按内容去认会把状态贴错行，
    而「哪些退过了」这件事认错，下一次回退就会重复动手或漏掉一处。

    已经退过的（DONE）**只升不降**：重复跑同一条命令时，那一处的这一步只会是
    「跳过」，把它降成 skipped 会让第三次调用把它当成「没退过」而去退第二次。
    """
    statuses = [step.status for step in steps]
    rounds: list[RunRound] = []
    cursor = 0
    for item in reversed(record.rounds):
        size = len(item.entries)
        chunk = statuses[cursor : cursor + size]
        cursor += size
        entries = tuple(
            replace(
                entry,
                status=(
                    DONE
                    if entry.status == DONE
                    else chunk[size - 1 - index]
                ),
            )
            for index, entry in enumerate(item.entries)
        )
        rounds.append(RunRound(index=item.index, at=item.at, entries=entries))
    record.rounds = list(reversed(rounds))
    record.last_reverted_at = now_iso()
    return record
