"""`spool memory`：记忆的迁移与体检。

P0 只有一条子命令 `migrate`，它管的是**旧 `memory.md`**：那份文件是人类可能
手改过的（设计稿 §9.2），所以迁移必须**可解释、可拒绝、可回滚**——

- **默认 `--dry-run`**：只解析、只报告，一个字节都不写（文件不改、库不建、
  不改名、不生成 `.bak`）。「想执行」必须显式 `--yes`——默认写文件是这类
  命令最常见的事故面。
- **`--yes` 的两步顺序不可颠倒**：① 原文件**改名为 `memory.md.bak-<日期>`**
  （同名已存在就加序号，**绝不覆盖任何既有文件**）→ ② 写新的 `memory.md`。
  先备份后写：写失败也丢不了原文。备份是**改名**，不是复制，所以备份与原文件
  **逐字节相同**是构造上的事实（同一串字节换了个名字），不是巧合。
- **只动 `memory.md` 一个文件**：不建表、不写 `memory` 行、不动 `user_version`。
  降级条目**不重写**——它们的正文早在 `episode.summary` 里了（设计稿 §9.2 第 2 步
  「重定向而不是新写」），所以报告只对每条做一次**只读**核对并写出 `episode#<id>`，
  没命中就如实写「未命中」。

**离线路径（硬约束）**：本命令**不定义 `provider` 属性**。`app.py` 里"缺供应商
就弹向导 + `settings.describe_missing()` + 解析 `.env` 密钥"那一整段都在
`hasattr(args, "provider")` 之后，没有这个属性就一个字节的 `.env` 都不会被读；
`--root` 仍照既有口径在 `app.py` 里解析一次（口径同 `session`）。
dry-run 打开 `memory.db` 用的是**只读 URI**（`?mode=ro`），因为既有的 `open_db`
总会创建/可写——用它就等于"看一眼就把库动了一遍"。
"""

import argparse
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from spoolkit.cli.term import print_out
from spoolkit import limits
from spoolkit.memory.gate import Verdict, content_hash, judge, normalize_policy
from spoolkit.memory.hot import read_hot, write_hot
from spoolkit.memory.store import open_db_readonly

# 降级条目在报告里做只读核对的匹配长度：`episode.summary` 的结论段受
# `episode_final_chars` 截断，拿整条去 `LIKE` 会假阴性。取前 20 个字符
# （与计划 Task 6 的要点一致）。
_EPISODE_PROBE_CHARS = 20

# 没有实质判据的说明：P0 判不了 J2，报告必须写明，否则读报告的人会以为
# 每一条都过了「有出处」这道检查（计划「待用户拍板 10 / 风险 10」）。
_UNJUDGED = "J2（evidence）与 scope 属 P1，本报告不据此判任何一条"

_RULE_ORDER = ("J1", "J3", "J4", "J5")


@dataclass(frozen=True)
class Item:
    """`memory.md` 里的一条及其结论。"""

    kind: str
    text: str
    line: int
    verdict: Verdict

    @property
    def rule_id(self) -> str:
        for rule in _RULE_ORDER:
            if self.verdict.reason.startswith(rule):
                return rule
        return "拒" if not self.verdict.ok else "收"


@dataclass(frozen=True)
class Duplicate:
    """判重命中：这条与**同一次迁移里更早的那条**归一化后相同。"""

    item: Item
    anchor_line: int
    anchor_accepted: bool

    @property
    def reason(self) -> str:
        tail = "" if self.anchor_accepted else "（锚点自己也没入库：判据拒）"
        return f"与 memory.md:{self.anchor_line} 规范化后相同，不重复收{tail}"


@dataclass
class MigrationReport:
    """迁移预演报告：解析 → 判重 → 判据 → 结论，全是只读动作的产物。"""

    items: list[Item] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)
    # 判据通过但库里已有同一条：行号 → `memory#<id>`
    already: dict[int, int] = field(default_factory=dict)
    # 降级条目：行号 → "episode#<id>" 或 "未命中"
    episode_refs: dict[int, str] = field(default_factory=dict)
    db_note: str = ""

    @property
    def parsed(self) -> int:
        return len(self.items) + len(self.duplicates)

    @property
    def promoted(self) -> list[Item]:
        """真要收进新热记忆的：判据过 **且** 库里没有同一条。"""
        return [
            item
            for item in self.items
            if item.verdict.ok and item.line not in self.already
        ]

    @property
    def in_library(self) -> list[Item]:
        return [item for item in self.items if item.line in self.already]

    @property
    def rejected(self) -> list[Item]:
        """判据拒的（走降级出口）。"""
        return [item for item in self.items if not item.verdict.ok]

    @property
    def downgraded(self) -> list[Item]:
        """降级为 episode 的 = 判据拒的（P0 的降级出口，设计稿 §5.3 ②）。"""
        return self.rejected

    @property
    def refused(self) -> int:
        """「判重 / 判据拒」那一栏的合计。"""
        return len(self.rejected) + len(self.duplicates)


def analyze(
    root: Path,
    *,
    overrides: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> MigrationReport:
    """解析 `memory.md` 并逐条给结论。**只读**：不写文件、不写库、不建库。"""
    report = MigrationReport()
    hot_path = root / ".agent" / "memory.md"
    if not hot_path.exists():
        report.db_note = f"没有找到 {hot_path}"
        return report

    raw = hot_path.read_text(encoding="utf-8")
    sections = read_hot(hot_path)
    # 任务已结束的"进行中"条目没有迁移价值，且与 archive 的下沉口径一致
    # （archive.py 也是先把 `doing` pop 掉再裁容量）。
    sections.pop("doing", None)

    policy = normalize_policy(overrides)
    anchors: dict[str, Item] = {}
    # 行号一次配对到底：逐字相同的条目各自报自己的行号（真实工作区
    # `memory.md:4/5/8` 三条寒暄文本一样，报告必须分别写 `:4`、`:5`、`:8`）。
    flat = [(kind, text) for kind, texts in sections.items() for text in texts]
    positions = _find_lines(raw, [text for _, text in flat])
    for (kind, text), line in zip(flat, positions):
        digest = content_hash(text, overrides, policy)
        if digest and digest in anchors:
            anchor = anchors[digest]
            report.duplicates.append(
                Duplicate(
                    item=Item(kind, text, line, Verdict(True)),
                    anchor_line=anchor.line,
                    anchor_accepted=anchor.verdict.ok,
                )
            )
            continue
        item = Item(
            kind=kind, text=text, line=line, verdict=judge(kind, text, overrides=overrides)
        )
        if digest:
            anchors[digest] = item
        report.items.append(item)

    if conn is not None:
        _attach_library(report, conn, overrides)
        _probe_episodes(report, conn)
    else:
        report.db_note = (
            "没找到 .agent/memory.db：本报告不含「库里已有」与 episode 核对"
            "（dry-run 不会创建它）"
        )
    return report


def _attach_library(
    report: MigrationReport, conn: sqlite3.Connection, overrides: dict | None
) -> None:
    """库里已有同一条 → 也不必再收进热记忆（**只读**核对，不 touch、不写）。"""
    known: dict[str, int] = {}
    for row in conn.execute("SELECT id, text FROM memory").fetchall():
        digest = content_hash(row["text"], overrides)
        if digest:
            known.setdefault(digest, row["id"])
    for item in report.items:
        digest = content_hash(item.text, overrides)
        if item.verdict.ok and digest and digest in known:
            report.already[item.line] = known[digest]


def _probe_episodes(report: MigrationReport, conn: sqlite3.Connection) -> None:
    """每条降级条目去 `episode.summary` 里**只读**核对一次，如实写命中与否。"""
    for item in report.downgraded:
        probe = " ".join(item.text.split())[:_EPISODE_PROBE_CHARS]
        row = conn.execute(
            "SELECT id FROM episode WHERE summary LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{probe}%",),
        ).fetchone()
        report.episode_refs[item.line] = (
            f"episode#{row['id']}" if row is not None else "未命中"
        )


def _find_lines(raw: str, texts: list[str]) -> list[int]:
    """把每条条目对到它的**行号**（报告要能指回 `memory.md:<行号>`）。

    `parse_hot` 的粒度就是"一个 `- ` 行 = 一条"（续行会被忽略），所以这里
    逐行取正文比对。做成**一次配对到底**、并按顺序记录已占用的行，是因为
    逐字相同的条目会出现多次：真实工作区 `memory.md:4/5/8` 三条寒暄文本一样，
    一条一条找而不排除已占用的行，第二条又会落回第一条的行号上（报告里就
    只剩一个 `:4`，另外两条"消失"了）。

    对不上的条目（文件被人手工改坏、正文与解析结果不一致）退回"上一条的
    行号 + 1"：不崩、也不静默丢条目。
    """
    targets = [text.strip() for text in texts]
    lines = raw.splitlines()
    bullets = [
        (index + 1, line.strip()[2:].strip())
        for index, line in enumerate(lines)
        if line.strip().startswith("- ")
    ]
    used = [False] * len(bullets)
    found: list[int] = []
    cursor = 0
    for target in targets:
        line = 0
        for position in range(cursor, len(bullets)):
            if used[position]:
                continue
            if bullets[position][1] == target:
                used[position] = True
                # **只在命中时前进**：没命中就交给下面的退路（上一条行号 + 1），
                # 不要把后面的行也一并划走——那会让"原文里对不上"的一条把
                # 后面几条的行号全顶掉（报告里就会出现一串错位的 `:5`、`:6`…）。
                cursor = position + 1
                line = bullets[position][0]
                break
        if not line:
            line = (found[-1] + 1) if found else (bullets[0][0] if bullets else 1)
        found.append(line)
    return found


def render_report(report: MigrationReport, *, hot_path: Path, dry_run: bool) -> str:
    """把报告渲染成给人看的文本（四栏 + 逐条理由）。"""
    mode = (
        "--dry-run：不写文件、不改库、不解析密钥"
        if dry_run
        else "--yes：写文件前的预演（下面这份就是新内容）"
    )
    lines: list[str] = [f"memory.md 迁移预演（{mode}）"]
    lines.append(f"  文件：{hot_path}")
    lines.append(f"  解析：{report.parsed} 条（{_line_list(report)}）")
    if report.parsed == 0:
        lines.append("  无可迁移：这份 memory.md 里没有可识别的分节条目。")
        return "\n".join(lines)

    lines.append(f"  收进热记忆（{len(report.promoted)} 条）")
    for item in report.promoted:
        lines.append(f"    - [{item.kind}] memory.md:{item.line} {_short(item.text)}")
    for item in report.in_library:
        lines.append(
            f"    - [已有的] memory.md:{item.line} {_short(item.text)}"
            f"（库里已有同一条：memory#{report.already[item.line]}）"
        )

    lines.append(f"  判重 / 判据拒（{report.refused} 条）")
    for item in report.rejected:
        lines.append(f"    - [{item.rule_id}] memory.md:{item.line} {_short(item.text)}")
        lines.append(f"      {item.verdict.reason}")
    for dup in report.duplicates:
        lines.append(
            f"    - [duplicate] memory.md:{dup.item.line} {_short(dup.item.text)}"
        )
        lines.append(f"      {dup.reason}")

    lines.append(f"  降级为 episode（{len(report.downgraded)} 条）")
    for item in report.downgraded:
        lines.append(f"    - [{item.rule_id}] memory.md:{item.line} {_short(item.text)}")
        lines.append(f"      {item.verdict.reason}")
        ref = report.episode_refs.get(item.line)
        if ref:
            lines.append(f"      已在 {ref} 的 summary 里（只读 LIKE 核对）")
    lines.append(f"  未判：{_UNJUDGED}")
    if report.db_note:
        lines.append(f"  库：{report.db_note}")
    if dry_run:
        lines.append(
            "  结论：新 memory.md 将只剩「收进热记忆」的条目；原文件改名为"
            " memory.md.bak-<日期>。本次是预演，没有写任何文件。"
        )
    else:
        lines.append("  结论：新 memory.md 已按上面这份写入，原文件已改名为备份。")
    return "\n".join(lines)


def _line_list(report: MigrationReport) -> str:
    numbers = [item.line for item in report.items] + [
        dup.item.line for dup in report.duplicates
    ]
    return "、".join(f"memory.md:{number}" for number in sorted(numbers))


def _short(text: str, width: int = 48) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[:width] + "…"


def backup_path(hot_path: Path, *, today: str | None = None) -> Path:
    """备份文件名：`memory.md.bak-<日期>`；同名已存在就加序号，**绝不覆盖**。"""
    stamp = today or date.today().isoformat()
    candidate = hot_path.with_name(f"{hot_path.name}.bak-{stamp}")
    index = 2
    while candidate.exists():
        candidate = hot_path.with_name(f"{hot_path.name}.bak-{stamp}-{index}")
        index += 1
    return candidate


def migrate(args: argparse.Namespace) -> int:
    """`spool memory migrate [--dry-run|--yes]`。默认 `--dry-run`。"""
    root = Path(args.root).resolve()
    state_dir = root / ".agent"
    hot_path = state_dir / "memory.md"
    execute = bool(getattr(args, "yes", False))
    overrides = limits.load_overrides(root)

    conn = open_db_readonly(state_dir / "memory.db")
    try:
        report = analyze(root, overrides=overrides, conn=conn)
    finally:
        if conn is not None:
            conn.close()

    if not hot_path.exists():
        print_out(f"{hot_path} 不存在：无可迁移（没有创建任何文件）。", markup=False)
        return 0

    print_out(render_report(report, hot_path=hot_path, dry_run=not execute), markup=False)
    if not execute:
        return 0

    kept: dict[str, list[str]] = {}
    for item in report.promoted:
        kept.setdefault(item.kind, []).append(item.text)
    # ① 先改名保留原文：改名不是复制，所以备份与原文件逐字节相同。
    backup = backup_path(hot_path)
    hot_path.rename(backup)
    # ② 再写新文件。
    write_hot(hot_path, kept)
    print_out(f"  原文件已保留为：{backup.name}（逐字节等于迁移前的 memory.md）", markup=False)
    print_out(
        "  新 memory.md 写入完成：只含「收进热记忆」的 "
        f"{sum(len(items) for items in kept.values())} 条。",
        markup=False,
    )
    return 0
