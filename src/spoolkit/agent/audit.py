"""自动审查层 L1 的种子：把「这一轮验证的结论有什么可疑」机械化地判出来。

为什么要有它（这一条是探针复跑逼出来的，不是设计出来的）：

`rename-cross-file` 在同一条冻结代码上跑 4 次，1 绿 3 红。红的那几次轨迹形状
一样——**多文件改名分多步落盘**：

1. 先落 `pipeline/fetch.py`（新名字），此时测试文件还是旧内容，
   自动验证如实报 `ERROR tests/test_pipeline.py`；
2. 模型把这个**中间态**当成「我的代码有问题」；
3. 为了让测试过，它把 `render()` 里的表头行**删掉了**——迎合测试而改坏业务逻辑；
4. 而 `run_command` 跑在**磁盘视图**上（那里旧测试文件按「按原始内容判定」
   的规矩还原了），于是模型以为自己没坏——同一个事实，系统给了它两个答案。

所以这一层只做一件事：**在回灌验证结论的那一刻，把「这次失败是什么性质」
说清楚**。三条判据都是机械的、可证伪的：

- **回退**（`scope_fallback_note`）：范围收窄到子目录后导不进项目根时，
  由 `verify.py` 在项目根重跑一次，这里只负责把「为什么换了范围」写成一句人话。
- **中间态**（`landing_in_progress`）：验证是按**原始测试内容**判定的
  （见 `spoolkit.tools.exec._restore_originals`），如果失败正是由这次改名
  造成的「旧名找不到」，那不是代码问题，是**改名还没铺完**。
- **迎合测试**（`tampering_suspected`）：测试没过，同时**业务代码比磁盘上的
  原始版本更短**（删逻辑最常见的形状）。高严重度，但只说事实、不阻断——
  合法的大改（合并、去重、删死代码）也会变短，所以它是一条**要人看的证据**，
  而不是一次拒绝。
- **越界写**（`boundary_write`）：这次改动落在了**受保护路径**上。判据同样
  机械：数据/资产类扩展名（`.csv` / `.xlsx` / `.db` / `.sqlite` …）或工作区
  配置里点名的路径，**并且**这个文件不是本次运行自己创建的。现场是
  `add-feature` 在 t=0 那一次改掉了 `data/expenses.csv`（哈希变）：探针侧抓到了
  （guard_hashes 报下限红），系统侧却什么都没说——这一条补的就是系统侧那一声。
  高严重度，**不阻断**（改数据文件也可能是任务本身），但要看得见。

刻意不铺大：不做 LLM 语义审查、不做风险分级放行、不写证据账本。这一层只把
判据、严重度与输出行做出来，升级挂点见 `escalate`。
"""

import ast
import configparser
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from spoolkit.tools.edit import (
    BROKEN_IMPORT_KIND,
    DELETED_REFERENCE_KIND,
    STALE_REFERENCE_KIND,
    STALE_REFERENCES_KIND,
    is_test_path,
)

# 严重度。只有两档：这一轮要做的是「检测 + 报出来」，
# 分级放行（T1–T12 那一套）是后面几层的事，这里不假装有。
HIGH = "高"
INFO = "提示"

#: 事件流里这类发现的 note kind。探针据它统计「高危信号出现几次」。
AUDIT_KIND = "audit"

#: 「测试被改弱」这一类 note 的 kind。与 audit 分开是因为探针要单独数它
#: （`tests_weakened` 字段），而混在 audit 里就只能靠读散文认。
TESTS_WEAKENED_KIND = "tests_weakened"

#: 「完成声明没有证据」这一类 note 的 kind（探针的 `evidence_gaps` 字段）。
EVIDENCE_GAPS_KIND = "evidence_gaps"

#: 判据名 → 事件流里的 note kind。**探针按 kind 数，不读散文**。
_WEAKENED_KINDS = frozenset(
    {
        "test_deleted",
        "test_data_deleted",
        "test_data_changed",
        "test_removed",
        "test_skipped",
        "assert_loosened",
        "fixture_removed",
        "conftest_collection_changed",
        "pytest_addopts_weakened",
        "pytest_config_changed",
        "pytest_config_deleted",
    }
)

#: 判据名 → 事件流里的 note kind（探针按它数，见 `note_kind`）。
_NOTE_KIND_BY_KIND = {
    **{kind: TESTS_WEAKENED_KIND for kind in _WEAKENED_KINDS},
    # 残留引用与「删掉的文件还有人引用」是同一类读数（`stale_references`）。
    STALE_REFERENCE_KIND: STALE_REFERENCES_KIND,
    DELETED_REFERENCE_KIND: STALE_REFERENCES_KIND,
    BROKEN_IMPORT_KIND: BROKEN_IMPORT_KIND,
}


def note_kind(kind: str, default: str = AUDIT_KIND) -> str:
    """这条发现该用哪个 note kind 发出去。

    **探针按这个 kind 数**（`stale_references` / `tests_weakened` /
    `broken_import`）——映射写在一处，事件流与探针字段不会各认一套。
    """
    return _NOTE_KIND_BY_KIND.get(kind, default)

#: 高严重度发现要单独顶出来一行。它同时是**升级挂点**的形状：
#: 以后接「先请示后落盘 / 阻断」时，拦的就是这一行对应的发现。
ESCALATION_MARKER = "【高危】"


@dataclass(frozen=True)
class Finding:
    """一条机械判出来的发现。"""

    kind: str          # 判据名（英文标识符，事件流与测试都按它取）
    severity: str      # HIGH / INFO
    summary: str       # 一句话，人看
    detail: str = ""   # 证据（谁、差了多少、哪个文件）
    escalate: bool = False  # 是否已到「该动硬手段」的门槛（本轮只挂点）
    scope: str = ""    # 这条发现是在哪个验证范围里得出的（回退那条要它）
    # **每条发现都要能指到 `文件:行` 与判据**（设计稿 §4：指不到就不上报）。
    # 老判据（回退 / 中间态 / 迎合测试）按验证范围给，路径留空即可。
    path: str = ""
    line: int = 0

    @property
    def where(self) -> str:
        if not self.path:
            return ""
        return f"{self.path}:{self.line}" if self.line else self.path

    def render(self) -> str:
        head = f"{ESCALATION_MARKER}" if self.severity == HIGH else "·"
        spot = f"{self.where} " if self.where else ""
        line = f"{head} {spot}{self.summary}"
        return f"{line}（{self.detail}）" if self.detail else line


@dataclass(frozen=True)
class Audit:
    """一次验证之后的审查结论。"""

    findings: tuple[Finding, ...] = ()

    @property
    def high(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity == HIGH)

    @property
    def blocking(self) -> bool:
        """有没有「该先请示」的发现。本轮没有自动阻断，只用于事件流与日志。"""
        return any(item.escalate for item in self.findings)

    def render(self) -> str:
        return "\n".join(item.render() for item in self.findings)


def landing_in_progress(
    *,
    ran_scope: str,
    failed_test_paths: list[str],
    reverted_paths: list[str],
    renamed: dict[str, str],
    detail: str,
) -> Finding | None:
    """这次失败是不是「改名只落了一半」的产物。

    判据（三条都要成立，缺一条都不报）：

    1. 失败出在**测试文件**上（收集期 ERROR 或测试体报错），或者测试文件正在
       按原始内容判定；
    2. 报错里出现的旧名，正是本次待确认改动**改掉的那个名字**
       （`renamed` 由 `pending_renames` 算出，不是猜的）；
    3. 旧名在新内容里确实已经不存在了——所以测试找不到它，不是代码坏了。

    报出来的时候**必须说清两套世界**：验证按原始测试内容判定，而磁盘/试跑副本
    里已经是新内容。不说这句，模型会以为「测试文件本来就是这样」，
    于是去改业务代码（实测它把表头行删了）。

    `detail` 要传**原始报错**，不是给模型看的那份短摘要：短摘要里只剩
    `ERROR tests/test_pipeline.py`，真正的 `ImportError: cannot import name
    'get_rows'` 已经被 `condense_test_output` 丢掉了。
    """
    if not renamed:
        return None
    # 报错原文里出现了「本次被改掉的那个名字」才算。`renamed` 由
    # `pending_renames` 从待确认改动的新旧正文里算出来，不是猜的。
    hits = {old: new for old, new in renamed.items() if old in detail}
    if not hits:
        return None
    # 报错落在哪个测试文件上：优先取报错原文里点到的那些，没有就说「测试」。
    mentioned = [
        path for path in failed_test_paths if any(old in path for old in hits)
    ] or list(failed_test_paths)
    pairs = "、".join(f"{old}→{new}" for old, new in sorted(hits.items()))
    where = "、".join(mentioned) or "测试"
    reverted = "、".join(reverted_paths)
    world = (
        f"验证按这些测试文件的**原始内容**判定（{reverted}）"
        if reverted
        else "测试文件的原始内容里还写着旧名"
    )
    return Finding(
        kind="landing_in_progress",
        severity=INFO,
        summary=(
            f"这是**中间态**，不是代码失败：{pairs} 已经落盘，"
            f"而 {where} 还在用旧名找不到它（{world}）。"
        ),
        detail=(
            "改名天生要多步，先把整个项目里的引用改名铺完再判；"
            "不要为了让测试通过去删改业务逻辑。"
        ),
    )


def tampering_suspected(
    *,
    shrunk: list[tuple[str, int, int]],
    changed_paths: list[str],
    renamed: dict[str, str],
    detail: str = "",
) -> Finding | None:
    """「测试没过，同时业务代码被改短」——迎合测试的最常见形状。

    `shrunk` 是（路径, 原始行数, 现在行数）三元组，由调用方拿磁盘基线算出来：
    行数是可证伪的，比措辞可靠。三条排除：

    - 这次失败就是**改名没铺完**造成的（`detail` 里出现了被改掉的旧名）时
      不报：那是中间态，报成高危等于教人忽略这条判据（那条由
      `landing_in_progress` 负责）。
    - 改动里**只**有测试文件时不算：那是另一类问题（验证层已按原始内容判定
      并写在结论里）。
    - 其余情况一律高严重度，哪怕这次改动里同时有改名：断言失败说明**行为
      真的变了**，而变短说明逻辑被拿掉了——那正是要人看的那种组合。
      只在证据里如实记一笔「同时还有改名」，免得人误判因果。

    高严重度**不阻断**：合并、去重、删死代码都是合法的大删。这一层只把
    事实摆出来，让模型与人都看得到「测试失败的同一步，业务逻辑变短了」。
    """
    if not shrunk:
        return None
    evidence = "；".join(
        f"{path} 从 {before} 行变成 {after} 行（少了 {before - after} 行）"
        for path, before, after in shrunk
    )
    if any(old in detail for old in renamed):
        # 失败点名了被改掉的旧名：这是中间态，不是迎合测试。
        return None
    extra = "；这次改动里同时有名字被改（先确认变短与该改名无关）" if renamed else ""
    return Finding(
        kind="tampering_suspected",
        severity=HIGH,
        summary=(
            "测试没过，**同时业务代码被改短**——像是为了让测试通过而删逻辑。"
            "先确认删掉的是真的多余，否则改回被删的部分，按报错改代码。"
        ),
        detail=(
            f"{evidence}（这次改过：{'、'.join(changed_paths)}{extra}）"
        ),
        # 升级挂点：以后接「先请示后落盘」时，这里就是拦下那次落盘的位置。
        escalate=True,
    )


# ---- 受保护路径 / 越界写（T6 那一类的机械闸门）-----------------------------
#
# 判据的形状（两条都要成立才报）：
#
#   1. 命中受保护路径：数据/资产类扩展名，**或**工作区配置里点名的路径；
#   2. 这个文件**不是本次运行自己创建的**——本次新建的文件是它的产出，
#      不该被当成越界写。
#
# 第 2 条是**必要条件**。它是「本次运行没有创建的文件」那句直白的落地：
# 一个运行自己造出来的东西不算越界；别人（或上一轮）留下的东西才算。
# 想要更严的口径（**凡是本次没创建过的一律受保护**，包括既有源码）也支持：
# 配置里把 `all_existing` 打开。默认关——那样一来每一次「改既有代码」都是
# 高危，高危泛化之后就没有人看了，而这一层的全部价值就在于那条线还认得出。

#: 工作区级配置的位置（与 `.agent/policy.json` / `.agent/limits.json` 同一层）。
PROTECTED_CONFIG = ".agent/protected.json"

#: 数据/资产类扩展名。挑它们的理由不是「危险」，是**不可复核**：数据文件被就地
#: 改写没有 diff 可读，二进制资产连「改了什么」都说不出来，而这两类恰恰是
#: 一次「改代码」的运行最容易顺手改坏、也最难事后核对的东西。
PROTECTED_SUFFIXES: tuple[str, ...] = (
    ".csv", ".tsv", ".xlsx", ".xls", ".db", ".sqlite", ".sqlite3",
    ".parquet", ".pkl", ".npy", ".npz", ".jsonl",
    ".zip", ".tar", ".gz", ".7z",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
    ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".mov",
)

#: 这些前缀下的东西由 harness 自己写（授权、检查点、索引、记忆库），
#: 不是模型的产出，也不该进这条闸门——报它们只会淹掉真正的越界写。
_NEVER_PROTECTED = (".agent/",)


@dataclass(frozen=True)
class ProtectedConfig:
    """工作区级的受保护路径配置（读不到配置时用这里的默认值）。"""

    suffixes: tuple[str, ...] = PROTECTED_SUFFIXES
    #: 配置里点名的路径（相对工作区的 posix 形状；给目录就按前缀匹配）。
    paths: tuple[str, ...] = ()
    #: 打开之后，**本次运行没有创建的文件**一律受保护（不只数据/资产类）。
    all_existing: bool = False
    #: 打开之后，连本次运行自己新建的文件也算受保护（默认关：自己造的产出不算越界）。
    protect_created: bool = False


def load_protected(root: Path) -> ProtectedConfig:
    """读工作区级配置 `.agent/protected.json`；读不到/写坏了就用默认值。

    写坏时**不抛异常**：这一层是附加的可见性，不该因为一个手改坏的 JSON 让
    整次运行起不来。认得出的键：

        {"suffixes": [".csv", ".parquet"], "paths": ["data/账本.csv", "assets/"],
         "all_existing": false, "protect_created": false}
    """
    path = Path(root) / PROTECTED_CONFIG
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ProtectedConfig()
    if not isinstance(raw, dict):
        return ProtectedConfig()

    def _strings(key: str) -> tuple[str, ...] | None:
        value = raw.get(key)
        if not isinstance(value, list):
            return None
        return tuple(str(one).strip() for one in value if str(one).strip())

    suffixes = _strings("suffixes")
    paths = _strings("paths")
    return ProtectedConfig(
        suffixes=suffixes if suffixes is not None else PROTECTED_SUFFIXES,
        paths=paths or (),
        all_existing=bool(raw.get("all_existing", False)),
        protect_created=bool(raw.get("protect_created", False)),
    )


def _protected_reason(path: str, config: ProtectedConfig) -> str:
    """命中受保护路径的理由；没命中返回空串。`path` 是工作区内的 posix 形状。"""
    said = str(path or "").replace("\\", "/")
    # 只剥一层 `./`：`lstrip("./")` 会把 `.agent/…` 的前导点也吃掉，
    # 于是 harness 自己写的那些文件（`.agent/index.db`）会掉进扩展名判据里。
    if said.startswith("./"):
        said = said[2:]
    said = said.lstrip("/")
    if not said or any(said.startswith(one) for one in _NEVER_PROTECTED):
        return ""
    for one in config.paths:
        if said == one or said.startswith(one.rstrip("/") + "/"):
            return "工作区配置里点名的受保护路径"
    lowered = said.lower()
    for suffix in config.suffixes:
        if lowered.endswith(str(suffix).lower()):
            return f"数据/资产类扩展名（{suffix}）"
    if config.all_existing:
        return "本次运行没有创建的文件（工作区开了 all_existing）"
    return ""


def boundary_write(
    *,
    path: str,
    created: bool,
    config: ProtectedConfig | None = None,
) -> Finding | None:
    """这次改动是不是一次「越界写」。判据见上面那一段注释。

    `created` = 这个文件是**本次运行自己创建的**（调用方按磁盘上改动之前有没有
    它判，`PendingChanges.baseline()` 给的就是这个事实）。

    报出来的是高严重度发现（`escalate=True` 就是升级挂点：以后接「先请示后落盘」
    时拦的就是这一条）。**不阻断**：任务本身就是「把这份 csv 修好」的时候，改了
    才是对的——所以它是一条要人看的证据，不是一次拒绝。
    """
    rules = config or ProtectedConfig()
    reason = _protected_reason(path, rules)
    if not reason:
        return None
    if created and not rules.protect_created:
        # 自己造出来的产出不算越界：这条判据管的是「动了不该动的东西」。
        return None
    return Finding(
        kind="protected_write",
        severity=HIGH,
        summary=f"这次改动了受保护路径 {path}（{reason}）",
        detail=(
            "数据/资产类文件被就地改写没有 diff 可读、也常常不可逆。"
            "先确认这处改动是有意的（任务就是要改它，或者用户点名要改）；"
            "不是的话，用 revert 把它退回去。"
        ),
        # 升级挂点：以后接「先请示后落盘」时，这里就是拦下那次写入的位置。
        escalate=True,
    )


# 回退原因那句话里「从哪个范围退回来」的形状：`verify.scope_fallback` 写的
# 就是「范围 tests 里测试导不进项目根（…）」。解析它比再传一个字段省事，
# 也让「文案即接口」这件事只有一处（`SCOPE_FALLBACK_MARKER` 那一段）。
_SCOPE_IN_REASON = re.compile(r"范围\s*(\S+?)\s*里")


def scope_fallback_note(*, fallback_reason: str) -> Finding:
    """范围回退必须**说出来**，别静默换判据。"""
    found = _SCOPE_IN_REASON.search(fallback_reason)
    scope = found.group(1) if found else "?"
    return Finding(
        kind="scope_fallback",
        severity=INFO,
        summary=f"验证范围从 {scope} 回退到项目根：{fallback_reason}",
        detail="结论以项目根那次为准，不是静默换了判据。",
        scope=scope,
    )


@dataclass
class AuditLog:
    """本次运行累积的发现账本。

    留着它是为了两件事：① 「迎合测试」这类高严重度信号在最终结论里也要提一句；
    ② 升级挂点需要一个**跨步**的落点（单步内判「反复」是判不出来的，
    所以这里只做累积与查询，判据仍在各步当场算）。
    """

    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding | None) -> Finding | None:
        if finding is not None:
            self.findings.append(finding)
        return finding

    def extend(self, audit: Audit) -> None:
        self.findings.extend(audit.findings)

    def of_kind(self, kind: str) -> list[Finding]:
        return [item for item in self.findings if item.kind == kind]

    def high(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == HIGH]

    def high_lines(self) -> list[str]:
        """高危发现**逐行**（带 `ESCALATION_MARKER`）：收尾报告引用的就是这一段。

        与 `render()` 分开是因为用途不同：`render()` 是给模型看的一句话摘要
        （验证失败时回灌），这一段是给人看的收尾报告——一行一条，标记醒目。
        """
        return [item.render() for item in self.high()]

    def render(self) -> str:
        return "; ".join(item.summary for item in self.high())


def audit(
    *,
    ok: bool,
    ran_scope: str,
    changed_paths: list[str],
    failed_test_paths: list[str] | None = None,
    reverted_paths: list[str] | None = None,
    renamed: dict[str, str] | None = None,
    shrunk: list[tuple[str, int, int]] | None = None,
    detail: str = "",
    raw_detail: str = "",
    fallback_reason: str = "",
) -> Audit:
    """一次验证之后的全部机械判据。顺序即输出的顺序（先解释回退，再说性质）。

    `detail` 是给模型看的那份（可能被 `condense_test_output` 截短过），
    `raw_detail` 是 pytest 的**原始输出**。判「改名是不是这条报错的原因」
    要用后者：短摘要里常常只剩 `ERROR tests/x.py`，真正的
    `ImportError: cannot import name 'get_rows'` 已经被丢掉了。
    """
    found: list[Finding] = []
    if fallback_reason:
        found.append(scope_fallback_note(fallback_reason=fallback_reason))
    if not ok:
        landing = landing_in_progress(
            ran_scope=ran_scope,
            failed_test_paths=list(failed_test_paths or []),
            reverted_paths=list(reverted_paths or []),
            renamed=dict(renamed or {}),
            detail=raw_detail or detail,
        )
        if landing is not None:
            found.append(landing)
        else:
            tamper = tampering_suspected(
                shrunk=list(shrunk or []),
                changed_paths=list(changed_paths),
                renamed=dict(renamed or {}),
                detail=raw_detail or detail,
            )
            if tamper is not None:
                found.append(tamper)
    return Audit(tuple(found))


# ---- L1.4 测试被改弱（P0：**只报不阻断**）-----------------------------------
#
# 现有防线是「按原测试判定」（`verify.exec._restore_originals` + 试跑副本），
# 它挡得住两种形状：把断言改成 `assert True`、删掉测试文件。它挡不住的是
# **不在测试文件判定口径里**的那些（设计稿 §1.7 G3）：
#
#   - `conftest.py`（删 fixture / 塞 collect_ignore）；
#   - `pyproject.toml` / `pytest.ini` / `tox.ini` / `setup.cfg` 里的
#     `addopts`（`-k` / `--ignore` / `--deselect` …）；
#   - 非 `.py` 的测试数据与 golden 文件；
#   - 测试文件本身被削弱：删用例、加 skip/xfail、放宽断言。
#
# 判据全部是**静态、可证伪**的：删了什么、少了几条断言、新增了哪个开关，
# 逐条都能指到 `文件:行`。这一层不做判断之外的处置——**不阻断、不落盘干预、
# 不改 `finished`**（那是 P1 的事）。
TEST_CONFIG_NAMES = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")

#: 只在这些键上判「配置被改」：它们直接决定「哪些测试会被收集」。
_NARROWING_KEYS = (
    "testpaths",
    "norecursedirs",
    "python_files",
    "python_classes",
    "python_functions",
)

#: 新增即视为「跳过了一批测试」的开关。判据是**新增**，不是出现。
_WEAKENING_SWITCHES = (
    "-k",
    "-m",
    "--ignore",
    "--deselect",
    "--lf",
    "--last-failed",
    "--ff",
    "--failed-first",
    "-p no:",
)

#: conftest 里新增这些构造 = 改掉了收集规则。
_COLLECT_HOOKS = (
    "collect_ignore",
    "pytest_collection_modifyitems",
    "pytest_ignore_collect",
)

#: 跳过类写法。`xfail` 也算：把一个用例标成「预期失败」就是把验收放宽。
_SKIP_PATTERNS = (
    r"pytest\.mark\.skip\b",
    r"pytest\.mark\.skipif\b",
    r"pytest\.mark\.xfail\b",
    r"pytest\.skip\(",
    r"pytest\.xfail\(",
    r"unittest\.skip\b",
)

_TEST_DEF_PATTERN = re.compile(
    r"(?m)^[ \t]*(?:async[ \t]+)?def[ \t]+(test[A-Za-z0-9_]*)[ \t]*\("
)
_FIXTURE_DEF_PATTERN = re.compile(
    r"(?m)^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)[ \t]*\("
)

#: golden 类文件的**路径段**。刻意要求整段相等（而不是「名字里含 golden」）：
#: `docs/expected-output.md` 不是测试数据，把它报成「测试数据被改」就是误报。
_GOLDEN_SEGMENTS = frozenset(
    {"golden", "goldens", "expected", "snapshot", "snapshots"}
)

_TEST_DIR_NAMES = ("test", "tests")


def _golden_like(pure: PurePosixPath) -> bool:
    """路径里有没有一段是 golden/expected/snapshot（含 `*.golden` / `*.snap`）。"""
    parts = [part.lower() for part in pure.parts[:-1]] + [pure.stem.lower()]
    return any(
        part in _GOLDEN_SEGMENTS or part.endswith((".golden", ".snap"))
        for part in parts
    )


def is_test_related(path: str) -> bool:
    """这个路径是不是「测试这一侧」的东西。

    比 `is_test_path` 宽：后者只认 `.py` 且只看 `test_*` / `tests/` 目录，
    于是 `conftest.py`、pytest 配置、测试数据与 golden 文件全落在口径之外
    ——那正是 G3 说「挡不住」的那一批。
    """
    pure = PurePosixPath(str(path).replace("\\", "/"))
    if is_test_path(str(path)):
        return True
    if pure.name == "conftest.py" or pure.name in TEST_CONFIG_NAMES:
        return True
    if any(part.lower() in _TEST_DIR_NAMES for part in pure.parts[:-1]):
        return True
    return pure.suffix != ".py" and _golden_like(pure)


def _def_line_map(text: str, pattern: re.Pattern[str]) -> dict[str, int]:
    """`名字 → 第一次定义的行号`（1 起）。"""
    out: dict[str, int] = {}
    for match in pattern.finditer(text):
        out.setdefault(match.group(1), text.count("\n", 0, match.start()) + 1)
    return out


def _assert_lines(text: str) -> list[int]:
    """这份正文里的 `assert` 语句行号（`ast` 判；解析不了退回行首写法）。"""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [
            index
            for index, line in enumerate(text.splitlines(), start=1)
            if line.strip().startswith("assert ")
        ]
    return sorted(
        {node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)}
    )


def _pytest_options(text: str, name: str) -> dict[str, str] | None:
    """这份 pytest 配置里的键值；读不出来返回 None（**判不了不报**）。"""
    if name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError):
            return None
        section = data.get("tool", {})
        section = section.get("pytest", {}) if isinstance(section, dict) else {}
        section = section.get("ini_options", {}) if isinstance(section, dict) else {}
        if not isinstance(section, dict):
            return None
        out: dict[str, str] = {}
        for key, value in section.items():
            if isinstance(value, (str, int, bool)):
                out[str(key)] = str(value)
            elif isinstance(value, list):
                out[str(key)] = " ".join(str(one) for one in value)
        return out
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read_string(text)
    except (configparser.Error, ValueError):
        return None
    for section in ("pytest", "tool:pytest"):
        if parser.has_section(section):
            return {key: str(value) for key, value in parser.items(section)}
    return {}


def _added_switches(before: str, after: str) -> list[str]:
    """`addopts` 里**新增**的跳过类开关。"""
    old = set(str(before or "").split())
    out: list[str] = []
    for token in str(after or "").split():
        if token in old:
            continue
        if any(
            token == switch or token.startswith(switch)
            for switch in _WEAKENING_SWITCHES
        ):
            out.append(token)
    return out


def _line_of(text: str, needle: str) -> int:
    """`needle` 第一次出现的行号；找不到返回 0。"""
    position = text.find(needle)
    if position < 0:
        return 0
    return text.count("\n", 0, position) + 1


def tests_weakened(
    *, path: str, before: str, after: str, deleted: bool = False
) -> list[Finding]:
    """这次改动把测试改弱了没有；逐条给 `文件:行`。

    **只报不改**：返回的发现进事件流与收尾报告，不参与落盘与 `finished`
    （P0 的纪律）。路径不是「测试这一侧」的东西时返回空表。
    """
    if not is_test_related(path):
        return []
    pure = PurePosixPath(str(path).replace("\\", "/"))
    found: list[Finding] = []

    def add(kind: str, severity: str, summary: str, detail: str, line: int) -> None:
        found.append(
            Finding(
                kind=kind,
                severity=severity,
                summary=summary,
                detail=detail,
                path=path,
                line=line,
            )
        )

    if deleted:
        if pure.name in TEST_CONFIG_NAMES:
            add(
                "pytest_config_deleted",
                INFO,
                f"pytest 配置 {path} 被删了",
                "测试怎么跑是由这份配置决定的，删掉它等于换了一套验收口径。",
                1,
            )
        elif pure.suffix == ".py":
            add(
                "test_deleted",
                HIGH,
                f"测试文件 {path} 被删了",
                "删除测试文件就是把这条验收标准撤掉（验证层还会按原始内容判定，"
                "但报告里必须看得见）。",
                1,
            )
        else:
            add(
                "test_data_deleted",
                HIGH,
                f"测试数据 {path} 被删了",
                "这类文件是验收标准的一部分，改动它等于改了判据。",
                1,
            )
        return found

    if pure.name in TEST_CONFIG_NAMES:
        old = _pytest_options(before, pure.name)
        new = _pytest_options(after, pure.name)
        if old is None or new is None:
            return []
        added = _added_switches(old.get("addopts", ""), new.get("addopts", ""))
        if added:
            flags = "、".join(added)
            add(
                "pytest_addopts_weakened",
                HIGH,
                f"pytest 配置新增了跳过类开关：{flags}",
                f"addopts 现在会缩小或跳过测试集合（{path}："
                f"{old.get('addopts', '')!r} → {new.get('addopts', '')!r}）。",
                _line_of(after, "addopts"),
            )
        for key in _NARROWING_KEYS:
            if key in old and old[key] != new.get(key):
                add(
                    "pytest_config_changed",
                    INFO,
                    f"pytest 配置的 {key} 被改了",
                    f"{old[key]!r} → {new.get(key)!r}（这决定哪些测试会被收集）。",
                    _line_of(after, key),
                )
        return found

    if not pure.suffix == ".py":
        add(
            "test_data_changed",
            INFO,
            f"测试数据 {path} 被改了",
            "非代码的测试数据/golden 文件被改：判据本身变了，不是实现变好。",
            1,
        )
        return found

    # ---- `.py`：删用例 / 加 skip·xfail / 放宽断言 ---------------------------
    before_tests = _def_line_map(before, _TEST_DEF_PATTERN)
    after_tests = _def_line_map(after, _TEST_DEF_PATTERN)
    for name in sorted(set(before_tests) - set(after_tests)):
        add(
            "test_removed",
            HIGH,
            f"测试用例 {name} 没了",
            f"改动前它在第 {before_tests[name]} 行；删用例等于撤掉验收标准。",
            before_tests[name],
        )
    if pure.name == "conftest.py":
        before_fixtures = _def_line_map(before, _FIXTURE_DEF_PATTERN)
        after_fixtures = _def_line_map(after, _FIXTURE_DEF_PATTERN)
        for name in sorted(set(before_fixtures) - set(after_fixtures)):
            add(
                "fixture_removed",
                HIGH,
                f"conftest 里的 fixture {name} 没了",
                f"改动前它在第 {before_fixtures[name]} 行；删 fixture 会让一批"
                "用例改走别的分支或直接报错。",
                before_fixtures[name],
            )
        for hook in _COLLECT_HOOKS:
            if hook in after and hook not in before:
                add(
                    "conftest_collection_changed",
                    HIGH,
                    f"conftest 新增了收集期构造 {hook}",
                    "它在收集阶段决定跑哪些用例，是「跳过测试」的另一种写法。",
                    _line_of(after, hook),
                )
    for pattern in _SKIP_PATTERNS:
        old_hits = len(re.findall(pattern, before))
        new_hits = [
            after.count("\n", 0, match.start()) + 1
            for match in re.finditer(pattern, after)
        ]
        if len(new_hits) <= old_hits:
            continue
        flag = pattern.replace("\\", "")
        add(
            "test_skipped",
            HIGH,
            f"新增了跳过/xfail 写法（{flag}）",
            f"改动前 {old_hits} 处、现在 {len(new_hits)} 处——测试被跳过就等于"
            "没跑。",
            new_hits[0],
        )
    old_asserts = _assert_lines(before)
    new_asserts = _assert_lines(after)
    if len(new_asserts) < len(old_asserts):
        gone = [
            line
            for line in old_asserts
            if before.splitlines()[line - 1].strip()
            not in {
                one.strip()
                for one in after.splitlines()
            }
        ]
        add(
            "assert_loosened",
            INFO,
            f"断言少了 {len(old_asserts) - len(new_asserts)} 条",
            f"改动前 {len(old_asserts)} 条、现在 {len(new_asserts)} 条"
            + (f"；第 {gone[0]} 行那条现在这个文件里找不到了" if gone else ""),
            gone[0] if gone else (new_asserts[0] if new_asserts else 1),
        )
    return found


# ---- L4 最小证据账本（P0：**只在报告层降级**）-------------------------------
#
# 「完成」是一句**声明**，不是事实。这一层只做一件很窄的事：把声明与
# 「跑过哪条检查、结果如何」对上；一条有结论的检查都没挂上时，那句完成
# 在收尾报告里降级为「未验证」。
#
# **它不改 `finished`、不改落盘判定、不新增请示流**——那些是 P1 的事。
PASS = "通过"
FAIL = "失败"
CANNOT = "查不了"
NOT_RUN = "没跑"


@dataclass(frozen=True)
class LedgerEntry:
    """一条「声明 ← 检查 ← 结果 ← 证据」。"""

    claim: str
    check: str
    result: str
    evidence: str = ""
    content_key: str = ""

    def render(self) -> str:
        spot = f"（{self.evidence}）" if self.evidence else ""
        return f"{self.check} → {self.result}{spot}"


@dataclass
class ReviewLedger:
    """本次运行的证据账本。"""

    entries: list[LedgerEntry] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)

    def claim(self, text: str) -> None:
        if text and text not in self.claims:
            self.claims.append(text)

    def record(
        self,
        claim: str,
        check: str,
        result: str,
        evidence: str = "",
        content_key: str = "",
    ) -> None:
        self.claim(claim)
        self.entries.append(
            LedgerEntry(
                claim=claim,
                check=check,
                result=result,
                evidence=evidence,
                content_key=content_key,
            )
        )

    def evidence_for(self, claim: str) -> list[LedgerEntry]:
        """这条声明挂上的**有结论**的证据（「查不了」不算证据）。"""
        return [
            item
            for item in self.entries
            if item.claim == claim and item.result in (PASS, FAIL)
        ]

    def gaps(self) -> list[str]:
        """没有证据的完成声明。"""
        return [claim for claim in self.claims if not self.evidence_for(claim)]

    def render(self) -> str:
        return "；".join(item.render() for item in self.entries)

    def downgrade_note(self) -> str:
        """没有证据的完成声明 → 一句话降级（**只降级这句话**）。"""
        gaps = self.gaps()
        if not gaps:
            return ""
        why = "；".join(
            item.render()
            for item in self.entries
            if item.result in (CANNOT, NOT_RUN)
        ) or "这次运行没有挂上任何一条能作证的检查"
        subject = "、".join(f"「{claim}」" for claim in gaps)
        return (
            f"⚠ 证据账本：{subject} 没有可核对的证据（{why}）"
            "——按规矩在报告里**降级为「未验证」**"
            "（本轮只降级这句话，不拦成果、不改落盘判定）。"
        )
