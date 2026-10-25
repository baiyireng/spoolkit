"""自动审查层 P1：风险分级放行（设计稿 §3.2）。

这一层只回答一个问题：**「这一次该不该问人」**，以及**问的时候拦的是谁**。

三条口径（缺一条整个模块就跑偏）：

1. **拦的是动作，不是成果**（§3.2.0）。文件编辑永远只是一条**记录**
   （`record_edit`），成果照落盘；只有「执行命令 / 删文件 / 迁移 / 联网 /
   越界写」这类**危险动作**才会在**执行前**被拦下挂起
   （`action_triggers`；T6 的关联目录那一面走 `boundary_action`）。
   所以待批 = 某个动作在等批复，而工作区里已经摆着这一轮的产出。
2. **每一处升级都要能回指一个具名触发条件**（T1–T13，§3.2.2）。
   回指不到就不许升级——`escalation_precision` 的判据就是这条。
3. **不许静默放行，也不许一律拒绝**（§3.2.3）。有人在就请示并等批复；
   无人值守时**不弹问题**、把那个动作挂起为待批、成果照落盘、可续跑。

判据全部是机械的：路径匹配、AST 签名、既有判定函数的返回值。这里**不写**
「高风险」这类形容词，也不给没有判据的东西硬造判据（§7.7）。

模块自己不做 I/O 之外的判断：动作拦不拦在 `action_triggers`（T6 的关联目录
那一面在 `boundary_action`，域外拒绝在 `outside_write_refusal`），结论降不降级在
`completion_triggers`，两者共用同一张触发条件表。事件由调用方（主循环）
注入的 `on_escalate` 发出去，这样这一层可以脱离主循环单测。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from spoolkit.agent.audit import HIGH, Finding
from spoolkit.tools.capabilities import (
    ACTION_TOOLS,
    EDIT_TOOLS,
    NETWORK_ENTRY_TOOLS,
    NETWORK_TOOLS,
)
from spoolkit.tools.edit import looks_like_path

# ---- 动作分类（§3.2.0 的那张表）--------------------------------------------

#: 拦的是**这次动作**、请求批复的危险动作。
DANGEROUS = "dangerous"
#: 只记录、不拦（文件编辑的成果一律照落盘）。
RECORDED = "recorded"

# 「执行」类工具（跑命令 / 删文件 —— 这些是动作，不是成果）、「出网」类工具、
# 以及主循环里真的会出网的那一个入口，三档清单都从
# `spoolkit.tools.capabilities` 的**能力元数据**派生：
#   ACTION_TOOLS       ← `destructive=True`
#   NETWORK_TOOLS      ← `network=True and not network_entry`
#   NETWORK_ENTRY_TOOLS← `network_entry=True`（就是 `research`）
# 这里只做转出，不手写第二份（手写清单会漂移：漏一个名字就是静默放开一类动作）。
#
# 为什么要点名 `research`：主循环的工具表里只有它会走到网络（`web_read` 只读本地
# 缓存，`web_fetch`/`web_search` 只发给研究会话）。不点它的话，T3 的联网那一面
# 在主循环里**一个调用方都没有**——升级判据写了却永远不触发，等于放行出网。

# ---- T6：工作区外的两种落点（§3.2.2 的处置表 + §2.5）------------------------

#: 落在工作区里。
INSIDE = ""
#: 落在**已授权关联目录**里（`spool extra add` 加过）：拦动作 + 请示一次。
#: 理由就是这条语义本身——「读放开，**写要确认**」。
LINKED = "linked"
#: 工作区与**所有**关联目录之外：硬拒绝，并说明「怎么才能做」与「为什么不问」。
OUTSIDE = "outside"

#: 写工具里「目标路径」的参数名（判越界只认这些，避免把正文里的字符串当路径）。
WRITE_PATH_KEYS = ("path", "paths", "target", "src", "dst", "dest")

#: 写工具：产出，永远只记录、不拦。
#:
#: 这一档**就是 `capabilities.EDIT_TOOLS`**（交内容的那几把：`writes=True` 且
#: 不破坏、也不是「确认入口」）。注意它与 `agent/loop.py` 里那个同名常量**不是**
#: 同一个集合：loop 那一档还含 `delete_file`（它判「这一步有没有动工作区」），
#: 而这里只收「成果」，多了删除就会把动作拦截漏出去。
WRITE_TOOLS = EDIT_TOOLS

#: 触发条件 id → 一句人读的判据（收尾报告与事件流里点名用）。
TRIGGER_NOTES: dict[str, str] = {
    "T1": "删除/重命名公共接口或导出",
    "T2": "改依赖与构建/部署配置",
    "T3": "触及认证/密钥/安全，或新增出网、shell=True、eval/exec",
    "T4": "数据/schema 迁移",
    "T5": "批量删除文件",
    "T6": "工作区外路径（关联目录：拦下写、请示一次；域外：直接拒绝并说明怎么才能做）",
    "T7": "改动规模越线**且**（该批没被验证过 或 同批有高严重度发现）",
    "T8": "L2 行为回归",
    "T9": "L3 语义审查的高严重度发现未被处理（只降结论：成果照落盘）",
    "T10": "没有验证器可用的高风险改动",
    "T11": "回退能力还没把握（拦动作）",
    "T12": "L1/L2 高严重度未处理（自动修 N 轮后仍在）",
    "T13": "回退能力不足（唯一「扣住成果」的正当理由）",
}

# ---- T2：依赖与构建/部署配置 ------------------------------------------------

DEPENDENCY_FILES = frozenset(
    {
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "pytest.ini",
        "dockerfile",
        "makefile",
        "uv.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
    }
)
DEPENDENCY_PREFIXES = (".github/workflows/", "scripts/")
DEPENDENCY_SUFFIXES = (".sh", ".ps1")

# ---- T3：认证 / 密钥 / 安全 --------------------------------------------------

SECURITY_SUFFIXES = (".pem", ".key", ".pfx", ".p12")
SECURITY_TOKENS = ("credential",)
SECURITY_PATH_PARTS = ("auth", "secret", "token")
#: 改到这些文件就是改授权/范围判定本身。
SECURITY_EXACT = ("src/spoolkit/policy.py", "spoolkit/policy.py")
SECURITY_PATH_SUFFIXES = ("cli/approval.py",)
#: 新增这些写法视为「新增出网/执行能力」。
DANGEROUS_CODE = (
    ("shell=True", "shell=True"),
    ("eval(", "eval("),
    ("exec(", "exec("),
    ("urllib.request", "urllib.request"),
    ("requests.", "requests."),
)

# ---- T4：数据/schema 迁移 ---------------------------------------------------

MIGRATION_PREFIXES = ("migrations/", "migration/")
MIGRATION_SUFFIXES = (".sql",)
MIGRATION_CODE = ("CREATE TABLE", "ALTER TABLE", "DROP TABLE")
#: 本仓 schema 版本步进的落点。
SCHEMA_VERSION_FILE = "src/spoolkit/memory/store.py"

# ---- T7：规模 ---------------------------------------------------------------

#: `limits.review_trigger_files` 的兜底值（标定表里给了就用标定值）。
#:
#: 设计稿 §6 说这些阈值「同样走 Knob」，但本仓的 `limits.py` 有一组**冻死的
#: 登记表断言**（`tests/test_limits.py::test_旋钮没有重名` 按 62+22+2+4+5+4 算总数），
#: 新增一个旋钮就会把那组数弄脏。所以这里**复用既有的 `review_limit`**
#: （同量级，设计稿 §3.2.2 对 T5/T7 的要求就是「与 review_limit 同量级」），
#: 并且标定值入口仍然是 `Config.limit` ——换工作区覆盖 `review_limit` 即可，
#: 不必改代码。等真要分档标定时再登记新旋钮，那时同步更新那组断言。
DEFAULT_TRIGGER_FILES = 5

# ---- 五件（§3.2.3）---------------------------------------------------------

#: 五件齐不齐的判据：**缺任一即视为无效请示**（探针的
#: `escalation_summary_complete`）。
SUMMARY_PARTS = ("impact", "conclusion", "advice", "options", "rollback")

#: 批复词表（与 `bridge/agent_runner.py` 的四档对齐，§3.2.3 ④）。
REPLIES = {
    "approve": ("y", "yes", "是", "批准", "同意", "执行"),
    "partial": ("p", "partial", "只批准其中几处", "部分"),
    "revert": ("n", "no", "否", "撤销", "不批准", "拒绝"),
    "replan": ("r", "replan", "改方案", "换个做法"),
}

#: 批复结果为「挂起待批」时的取值（探针的 `escalation_approved`）。
PENDING = "pending"


def fingerprint(text: str) -> str:
    """被拦动作/请示摘要的内容指纹（复用主循环同一种算法与长度）。

    留着它是为了让「批复针对的是不是眼前这个动作」可核对：指纹变了，
    旧批复作废、重新请示（§3.2.3 的批复与审计）。
    """
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _posix(path: str) -> str:
    """路径统一成 posix 形状。

    **不能用 `lstrip("./")`**：它是「字符集合」的剥离，会把 `.github/...`
    削成 `github/...` 而让 T2 那条前缀判据悄悄失效（本模块的冒烟测试抓到过）。
    只削掉真正的前导 `./`。
    """
    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _basename(path: str) -> str:
    return PurePosixPath(_posix(path)).name.lower()


def _ext(path: str) -> str:
    return PurePosixPath(_posix(path)).suffix.lower()


# ---- 工具调用的机械解析 ------------------------------------------------------


def _arg_paths(call: Any) -> list[str]:
    """这次调用点了哪些路径（参数里所有像路径的字符串）。"""
    out: list[str] = []
    arguments = getattr(call, "arguments", None) or {}
    for key in ("path", "paths", "target", "src", "dst", "dest"):
        raw = arguments.get(key)
        if isinstance(raw, str) and raw:
            out.append(raw)
        elif isinstance(raw, (list, tuple)):
            out.extend(str(item) for item in raw if isinstance(item, str) and item)
    return out


def _arg_command(call: Any) -> list[str]:
    raw = (getattr(call, "arguments", None) or {}).get("command")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    return []


def _new_content(call: Any) -> str:
    arguments = getattr(call, "arguments", None) or {}
    for key in ("content", "text", "new_text", "replacement"):
        raw = arguments.get(key)
        if isinstance(raw, str):
            return raw
    return ""


def _stored_path(root: Path, raw: str) -> str | None:
    """把参数里的路径规范化成工作区相对路径；解析不出来返回 None。"""
    if not raw or not looks_like_path(raw):
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            return None
    return _posix(raw)


def outside_label(root: Path, raw: str) -> str:
    """工作区外路径在人话里的名字：能算出绝对路径就给绝对的，算不出给原文。

    报错要点名**到底哪个路径**被拒了：只说「越界」的拒绝话术，模型会照原样
    再试一次（`paths.py` 里那句注释记着这次事故）。
    """
    candidate = Path(str(raw))
    try:
        target = candidate if candidate.is_absolute() else Path(root) / candidate
        return _posix(target.resolve())
    except (OSError, ValueError):  # pragma: no cover - 解析不出来就退回原文
        return _posix(raw)


def outside_write_refusal(path: str, directory: str = "") -> str:
    """T6 的域外分支：**硬拒绝**，但把「怎么才能做」与「为什么不问」说清。

    两句话都必须在，缺哪一句这个话术就变成坏话术：

    - **怎么才能做**：关联目录只能由本机指令写（`spool extra add`），聊天里
      加不了——不说这句，模型只知道「越界了」，它会换个写法接着撞。
    - **为什么不问**：这条路径没有可批准的正当情形（既不是工作区，也没有
      人显式授过权），请示一次也只是白花打扰预算（§3.2.4）。
    """
    where = directory or "那个目录"
    return (
        f"⛔ 没有执行：{path} 既不在工作区里、也不在任何一个已关联的目录里。"
        "这种路径**连一次都不问**——它没有任何可批准的正当情形（既不是工作区身份，"
        "也没有人显式授过权），请示只会白花一次打扰预算（§3.2.4）。\n"
        f"**怎么才能做**：在跑 agent 的那台机器上先关联它：`spool extra add {where}`"
        "（本机指令，聊天通道里加不了）。关联之后的语义是「**读放开，写要确认**」——"
        "读得到；写会被拦下来请示一次，批准了才写。\n"
        "拦的是**写到工作区外**这一次动作，**不是「不许删文件、不许精简」**："
        "删死代码、合并重复实现、删已被替代的文件都是正常编辑，照样落盘、"
        "照样可回退（§2.5 的反向条款）。要往下做就在工作区里改，或先关联目录。"
    )


def public_symbols(text: str) -> dict[str, str]:
    """模块级公开符号 → 签名快照。

    判据只认**模块级、不以下划线开头**的 def/class，外加显式的 `__all__`
    （设计稿 §6 P1：T1 只用 `ast` 签名快照这一半，L2.3 的其余部分在 P2）。
    快照必须是签名的**字符串**而不是行号：行号一变就会把所有改动都报成
    「接口变了」，那是纯噪声。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    found: dict[str, str] = {}
    exported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    try:
                        values = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        values = []
                    exported.update(str(item) for item in values)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = f"def {node.name}{_signature(node)}"
        elif isinstance(node, ast.ClassDef):
            found[node.name] = f"class {node.name}"
    snapshot = {
        name: sig
        for name, sig in found.items()
        if not name.startswith("_") or name in exported
    }
    for name in exported:
        snapshot.setdefault(name, f"export {name}")
    return snapshot


def _signature(node: ast.AST) -> str:
    args = getattr(node, "args", None)
    if args is None:
        return "()"
    names = [item.arg for item in getattr(args, "posonlyargs", [])]
    names += [item.arg for item in getattr(args, "args", [])]
    if getattr(args, "vararg", None) is not None:
        names.append("*" + args.vararg.arg)
    names += [item.arg for item in getattr(args, "kwonlyargs", [])]
    if getattr(args, "kwarg", None) is not None:
        names.append("**" + args.kwarg.arg)
    return "(" + ", ".join(names) + ")"


# ---- 一次升级 ---------------------------------------------------------------


@dataclass(frozen=True)
class EscalationRequest:
    """一次请示（对象是**动作 / 合并**，不是成果，§3.2.3）。

    `action` 为空串表示这是一次**纯结论级**的升级（T7/T8/T9/T10/T12）：
    §3.2.2 的处置表把这一类明确划成「成果照落盘，只把『不许宣称完成』
    写进结论」，所以它没有「被拦下的动作」。
    """

    trigger: str
    action: str = ""
    detail: str = ""
    summary: dict[str, str] = field(default_factory=dict)
    paths: tuple[str, ...] = ()
    #: 被拦的这次动作发生在一轮运行的哪个位置（动作前 / 收尾）。
    before_action: bool = True
    fingerprint: str = ""

    @property
    def complete(self) -> bool:
        """五件齐不齐（缺任一即无效请示，§3.2.3）。"""
        return all(str(self.summary.get(key) or "").strip() for key in SUMMARY_PARTS)

    @property
    def note(self) -> str:
        return TRIGGER_NOTES.get(self.trigger, self.trigger)


@dataclass(frozen=True)
class RecordedChange:
    """一条只记录、不拦的改动（T1–T4/T6/T7 命中的**编辑**那一面）。"""

    trigger: str
    path: str
    detail: str


@dataclass
class EscalationDecision:
    """批复结果。语义是「**不执行这个动作**」或「执行」，不是「回滚成果」。"""

    approved: str = PENDING
    word: str = ""
    channel: str = ""
    fingerprint: str = ""

    @property
    def execute(self) -> bool:
        return self.approved == "approve" or self.approved == "partial"

    @property
    def probe_value(self) -> Any:
        """探针 `escalation_approved` 的取值（`"pending"` 是挂起待批）。"""
        if self.approved == "pending":
            return PENDING
        return self.approved == "approve"


# ---- 闸门 -------------------------------------------------------------------


class EscalationGate:
    """T1–T13 的机械判定 + 动作前置拦截 + 结论降级。

    与主循环的分工：主循环负责「什么时候问」（每步之后 / 收尾之前）与
    「怎么把问题送出去、怎么把回答读回来」；这一层负责「凭什么问」与
    「拦住的是谁」。判据之外的东西（事件、落盘、报告）都由主循环接线。
    """

    def __init__(
        self,
        root: Path,
        *,
        limit: Callable[[str], float] | None = None,
        run_id: str = "",
        pending_path: Path | None = None,
        canonical_fix: bool = False,
        extra_roots: Sequence[Path] = (),
        gate_network: bool = True,
    ) -> None:
        self.root = Path(root)
        #: 标定值入口（`Config.limit`）。不给就用兜底默认值。
        self._limit = limit or (lambda _name: 0.0)
        self.run_id = run_id
        self.pending_path = pending_path
        #: 「只记录不拦」的那条降级线的开关（T11：对回退能力没把握时，
        #: 有 Baseline/回退命令就记录、没有才拦）。
        self.canonical_fix = canonical_fix
        #: 已授权关联目录（`spool extra`）：判 T6 的分流要用它。
        self.extra_roots = tuple(Path(item).resolve() for item in extra_roots)
        #: 出网（T3 的联网那一面）在**这一层**拦不拦。
        #:
        #: 子会话（研究会话 / 派发出去的实现者）关掉它：出网这个**动作**的
        #: 入口在主循环（`research`），进去之后抓页是那个动作的内部机制。
        #: 开着的话研究会话连一次 `web_fetch` 都执行不了，`research` 永远
        #: 只能交回一句「没抓到」——`tests/tools/test_research.py` 那条用例
        #: 就是这么红的（拦的是动作，不该把动作的效果一起拦掉）。
        self.gate_network = gate_network
        #: 本次运行里只记录不拦的改动（T1–T4/T6/T7 的编辑那一面）。
        self.recorded: list[RecordedChange] = []
        #: 本次运行里已经请示过的触发条件（同一条件不重复打扰）。
        self.escalated_triggers: list[str] = []
        #: 被拦下、还没执行的动作（跨结算保留的就是这一份清单）。
        self.pending_actions: list[EscalationRequest] = []
        #: 本次运行请示过的**全部**动作（含已批复、已执行的）。收尾报告按它
        #: 逐条列出「谁批的、批的哪一个」——待批清单会随执行而缩短，审计不能。
        self.all_actions: list[EscalationRequest] = []
        #: 已经批复过的指纹 → 批复（指纹变了则批复作废，§3.2.3）。
        self.decisions: dict[str, EscalationDecision] = {}
        #: 公共符号的改动前快照：路径 → {符号: 签名}。
        self._symbols: dict[str, dict[str, str]] = {}
        #: 每个文件当前的删除次数（T5）。
        self._deleted: dict[str, int] = {}
        #: T10 的原料：本次运行里出现过的「查不了」。
        self.cannot_check: list[str] = []
        self._seq = 0

    # ---- 标定值 ----

    def reset(self) -> None:
        """开始新一次 `run()` 之前清空**本次运行**的状态。

        §3.2.4 的打扰预算按「运行」算，所以上一次 run() 请示过的触发条件、
        被拦的动作清单、批复与发现都不该带进这一次——带了的话，第二次运行
        会静默少问一次，而那是「不该拦的没拦」的反面（该问的没问）。
        """
        self.recorded.clear()
        self.escalated_triggers.clear()
        self.pending_actions.clear()
        self.all_actions.clear()
        self.decisions.clear()
        self.cannot_check.clear()
        self._symbols.clear()
        self._deleted.clear()

    def _knob(self, name: str, default: float) -> float:
        value = self._limit(name)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default

    @property
    def pending_label(self) -> str:
        """挂起清单给人看的路径（能算相对工作区就算，算不出给绝对路径）。"""
        if self.pending_path is None:
            return "（未配置挂起清单）"
        try:
            return self.pending_path.relative_to(self.root).as_posix()
        except ValueError:
            return str(self.pending_path)

    @property
    def trigger_files(self) -> int:
        return int(self._knob("review_limit", DEFAULT_TRIGGER_FILES))

    @property
    def trigger_deleted(self) -> int:
        # 与 trigger_files 同量级（§3.2.2 对 T5 的要求）；单次删除且被删者
        # 无入边引用时降级为「记录」，那一条由调用方判。
        return int(self._knob("review_limit", DEFAULT_TRIGGER_FILES))

    @property
    def trigger_lines(self) -> int:
        # 「行数」那半条同样按 review_limit 折算：一次改动越过 5 个文件或
        # 400 行都算规模线（400 = 5 × 80，量级依据见 §5 的单次任务 token 量级）。
        return int(self.trigger_files * 80)

    # ---- 只记录不拦的那一面（成果照落盘）----

    def record_edit(self, call: Any) -> list[RecordedChange]:
        """登记一次文件编辑。**不拦**——成果必须落在盘上（§2.5）。

        返回命中的那几条记录（路径条件 T1–T4/T6 命中的**编辑**）。
        """
        name = str(getattr(call, "name", "") or "")
        if name not in WRITE_TOOLS:
            return []
        found: list[RecordedChange] = []
        for raw in _arg_paths(call):
            stored = _stored_path(self.root, raw)
            if stored is None:
                continue
            found += self._path_triggers(stored, _new_content(call), name)
        self.recorded.extend(found)
        return found

    def _path_triggers(
        self, stored: str, content: str, tool: str
    ) -> list[RecordedChange]:
        out: list[RecordedChange] = []
        base = _basename(stored)
        low = _posix(stored).lower()
        if base in DEPENDENCY_FILES or low.startswith(DEPENDENCY_PREFIXES) or _ext(
            stored
        ) in DEPENDENCY_SUFFIXES:
            out.append(RecordedChange("T2", stored, f"改到构建/部署/脚本：{stored}"))
        if (
            _ext(stored) in SECURITY_SUFFIXES
            or low.startswith(".env")
            or _matches_token(base, "auth")
            or _matches_token(base, "secret")
            or _matches_token(base, "token")
            or any(token in low for token in SECURITY_TOKENS)
            or any(part in low.split("/")[:-1] for part in SECURITY_PATH_PARTS)
            or low in SECURITY_EXACT
            or low.endswith(SECURITY_PATH_SUFFIXES)
        ):
            out.append(RecordedChange("T3", stored, f"改到认证/密钥/授权：{stored}"))
        if low.startswith(MIGRATION_PREFIXES) or _ext(stored) in MIGRATION_SUFFIXES:
            out.append(RecordedChange("T4", stored, f"改到迁移/schema：{stored}"))
        if low.endswith(SCHEMA_VERSION_FILE):
            out.append(RecordedChange("T4", stored, f"改到 schema 版本步进：{stored}"))
        return out

    def note_cannot_check(self, text: str) -> None:
        """记一条「查不了」（T10 的原料）。空串忽略。"""
        clean = " ".join(str(text or "").split())
        if clean and clean not in self.cannot_check:
            self.cannot_check.append(clean[:200])

    def snapshot_symbols(self, path: str, text: str) -> None:
        """记下这个文件**改动前**的公开符号快照（T1 的对照）。"""
        key = _posix(path)
        if key not in self._symbols:
            self._symbols[key] = public_symbols(text)

    # ---- 动作前拦截（§2.6 第 2 条：判定放在动作执行口之前）----

    def action_triggers(self, call: Any) -> list[EscalationRequest]:
        """这次调用是不是危险动作；命中则返回**执行前**要请示的那几条。

        文件编辑在这里**永远不会**产生升级（它们是成果），出网类同理只按
        T3 的联网那一面请示一次。
        """
        name = str(getattr(call, "name", "") or "")
        out: list[EscalationRequest] = []
        paths = [
            stored
            for stored in (_stored_path(self.root, raw) for raw in _arg_paths(call))
            if stored is not None
        ]
        if self.gate_network and name in NETWORK_TOOLS | NETWORK_ENTRY_TOOLS:
            out.append(
                self._make(
                    "T3",
                    action=f"{name}（出网）",
                    detail="出网是环境副作用，不是「写在哪儿」的问题，仍按 §3.2.2 拦下",
                    paths=tuple(paths),
                )
            )
        if name in ACTION_TOOLS:
            for stored in paths:
                out += self._dangerous_path_triggers(stored)
            if name == "run_command":
                joined = " ".join(_arg_command(call))
                out += self._dangerous_command_triggers(joined, call)
        return out

    def _dangerous_path_triggers(self, stored: str) -> list[EscalationRequest]:
        """删除文件那一面：**只判 T5**（同一路径批量删）。

        T6（工作区外路径）**不在这里**：它按路径落在哪儿分流（关联目录 →
        `boundary_action` 请示一次；域外 → 直接拒绝），判据是解析后的真实路径，
        不是删了几次——两件事的机械判据不同，混在一处会互相污染。
        """
        out: list[EscalationRequest] = []
        count = self._deleted.get(stored, 0) + 1
        self._deleted[stored] = count
        if count >= self.trigger_deleted:
            out.append(
                self._make(
                    "T5",
                    action=f"delete_file {stored}",
                    detail=f"同一路径删到第 {count} 次（阈值 {self.trigger_deleted}）",
                    paths=(stored,),
                )
            )
        return out

    def _dangerous_command_triggers(
        self, command: str, call: Any
    ) -> list[EscalationRequest]:
        out: list[EscalationRequest] = []
        low = command.lower()
        for token in ("rm -rf", "rm -r", "del /", "rmdir", "remove-item", "shutil.rmtree"):
            if token in low:
                out.append(
                    self._make(
                        "T5",
                        action=f"run_command {command[:80]}",
                        detail=f"命令里的破坏性写法：{token}",
                    )
                )
                break
        for token in ("git push", "npm publish", "pip install", "uv pip install"):
            if token in low:
                out.append(
                    self._make(
                        "T3",
                        action=f"run_command {command[:80]}",
                        detail=f"命令会改动本机之外的状态：{token}",
                    )
                )
                break
        if "migrate" in low or ".sql" in low:
            out.append(
                self._make(
                    "T4",
                    action=f"run_command {command[:80]}",
                    detail="命令看起来会跑迁移",
                )
            )
        return out

    def boundary_kind(self, raw: str) -> str:
        """这个路径落在哪儿：工作区内 / 已授权关联目录内 / 域外（T6 的分流）。

        **解析后的真实路径**是判据（`resolve()`），不是字符串前缀：`..`、
        绝对路径、符号链接逃逸都按解析结果算，与 `paths.py` 的口径一致。
        """
        if not raw or not looks_like_path(raw):
            return INSIDE
        base = self.root.resolve()
        candidate = Path(str(raw))
        try:
            target = (
                candidate.resolve()
                if candidate.is_absolute()
                else (base / candidate).resolve()
            )
        except (OSError, ValueError):  # pragma: no cover - 解析不了就不当越界
            return INSIDE
        if target == base or base in target.parents:
            return INSIDE
        for extra in self.extra_roots:
            if target == extra or extra in target.parents:
                return LINKED
        return OUTSIDE

    def boundary_targets(self, call: Any) -> list[tuple[str, str]]:
        """这次调用里每一个**写到工作区外**的目标 →（落点, 给人看的路径）。

        只认写工具：编辑是成果（§3.2.0），而「读到关联目录」本来就是允许的
        ——`resolve_readable` 的语义就是那样，读不该被这里拦一次。
        """
        name = str(getattr(call, "name", "") or "")
        if name not in WRITE_TOOLS:
            return []
        out: list[tuple[str, str]] = []
        for key in WRITE_PATH_KEYS:
            raw = (getattr(call, "arguments", None) or {}).get(key)
            items = [raw] if isinstance(raw, str) else list(raw or ())
            for item in items:
                if not isinstance(item, str) or not item:
                    continue
                kind = self.boundary_kind(item)
                if kind != INSIDE:
                    out.append((kind, outside_label(self.root, item)))
        return out

    def boundary_action(self, stored: str) -> EscalationRequest:
        """T6：落在**已授权关联目录**里的写（由调用方判定后显式登记）。

        这就是 `spool extra` 自己的语义：「读放开，**写要确认**」。所以这一次
        不硬拒，而是**拦下这次写、请示一次**（五件齐全，§3.2.3）；没批准就
        不执行它，而这一轮**其余成果照落盘**（§2.5 的硬不变量）。

        域外（工作区与所有关联目录之外）不走这里：那条路径没有可批准的正当
        情形，走硬拒绝 + 「怎么才能做」，见 `outside_write_refusal`。
        """
        return self._make(
            "T6",
            action=f"write {stored}",
            detail=(
                "解析后的真实路径不在工作区根，落在已授权关联目录里——"
                "关联目录的语义是「读放开，写要确认」，所以这次写要确认一次"
            ),
            paths=(stored,),
        )

    def _make(
        self,
        trigger: str,
        *,
        action: str,
        detail: str,
        paths: tuple[str, ...] = (),
    ) -> EscalationRequest:
        self._seq += 1
        # 指纹按**内容**算（触发条件 + 具体动作 + 细节），不掺序号：同一条
        # 触发条件、同一个动作在同一轮里重算一次必须得到同一个指纹，否则
        # 「已经请示过就不重复打扰」这条（§3.2.4 的打扰预算）会失效——每一轮
        # 自动修都会把同一个问题再问一遍。指纹也是批复的挂靠点：指纹变了
        # 说明批复针对的不是眼前这个动作，批复作废、重新请示（§3.2.3）。
        return EscalationRequest(
            trigger=trigger,
            action=action,
            detail=detail,
            paths=paths,
            before_action=True,
            fingerprint=fingerprint(f"{trigger}|{action}|{detail}"),
        )

    # ---- 结论降级（§3.1：不许宣称完成）----

    def completion_triggers(
        self,
        findings: Sequence[Finding],
        *,
        files: Sequence[str] = (),
        lines: int = 0,
        regression: bool = False,
        blocking_failure: bool = False,
        has_baseline: bool = True,
        revert_command: str = "",
        written: dict[str, str] | None = None,
        verified: bool = False,
        semantic: Sequence[Finding] = (),
    ) -> list[EscalationRequest]:
        """收尾这一刻命中的触发条件（纯规模 / 纯语义那一类）。

        **成果已经落盘**：这里的每一条都只降结论、不改落盘（§3.2.2 处置表）。
        唯一「扣成果」的 T13 不在这一组里——它由调用方在落盘口判（给不出
        回退就不落盘并点名）。

        `blocking_failure` 是 T8 的**门槛**，`verified` 是 T7 的**充分条件**
        那一半，两段说明都在方法体里。

        `semantic` 是 **L3 过了事实核对的高严重度发现**（P2 后半）。它与 `findings`
        里那批分开传，是因为两者的处置不同（§3.2.2 的处置表）：
        L1/L2 的高严重度接在 T12 上（「自动修 N 轮后**仍在**」），而 L3 单独接
        T9（「L3 高严重度发现未被处理」）。**两条都只降结论**：成果照落盘、
        可回退、报告逐条列出（§2.5 的硬不变量、§6 P2 验收第 7 条）。
        """
        out: list[EscalationRequest] = []
        high = [item for item in findings if item.severity == HIGH]
        touched = {_posix(path) for path in files}
        # T1 的「改文件」那一半照落盘；「把它当成本次运行的最终结论」这一半
        # 等批复（§3.2.2 对 T1 的专门处置）。
        changed_symbols = self.changed_public_symbols(written)
        if changed_symbols:
            out.append(
                self._completion(
                    "T1",
                    f"公共接口/导出被删或改签名：{'、'.join(changed_symbols[:5])}",
                    paths=tuple(sorted(touched)),
                )
            )
        trigger_lines = self.trigger_lines
        scale_hit = (
            len(set(touched)) >= self.trigger_files or int(lines) >= trigger_lines
        )
        # ---- T7 的充分条件：**规模只是必要条件，不是充分条件**（§3.2.4）----
        #
        # 「改了 6 个文件」本身不是风险：一次跨文件改名天生要改 5–8 个文件
        # （rename 那条探针恰好 6 个），而它可能是**验过且没有高严重度发现**
        # 的一批正常改动。把规模当充分条件，等于按「改动大小」而不是按
        # 「有没有风险」打扰人——升级率就是这么被顶上去的（§3.2.4：超标要
        # 回去改触发条件，**不是**调高阈值削弱覆盖）。
        #
        # 所以规模之上再加一条**风险**：要么这一批**还没被验证过**（不知道
        # 对不对），要么同批**有高严重度发现**（知道不对）。两条都不成立时
        # T7 不升级——它照样记进账本，只是不再打扰人。
        scale_risky = scale_hit and (not verified or bool(high))
        if scale_risky:
            out.append(
                self._completion(
                    "T7",
                    f"改动规模越过阈值且未验证/带高危发现：文件 "
                    f"{len(set(touched))}/{self.trigger_files}，"
                    f"行数 {int(lines)}/{trigger_lines}，"
                    f"最终验证 {'通过' if verified else '没通过或没跑'}，"
                    f"高严重度发现 {len(high)} 条",
                    paths=tuple(sorted(touched)),
                )
            )
        if regression and blocking_failure:
            # T8（L2 回归）的原判据是「项目测试失败 / 对照运行逐字不一致 /
            # 接口快照变化 / 优化类性能倒退」。P1 只接到「最终验证红」这一半，
            # 而且**刻意加了一道门槛** `blocking_failure`：
            #
            #   `spool` 自己的自动验证是按**原始测试内容**在试跑副本里跑的
            #   （`tools/exec._restore_originals`）。于是「改测试→改代码」这类
            #   任务（探针 `fix-bug`）在改完测试的那一刻必然先红一次，而那条红
            #   不是回归、也不是危险动作。把「任何一次红」都升级，等于把一条
            #   必然发生的事件当成风险，升级率直接越过 §3.2.4 的失败线。
            #
            # 所以门槛是「同一次收尾里**另有**高危发现（或被审查判过）」，也就是
            # 设计稿 §3.1 第 2 条真正要拦的形状：**测试红了却说做完了**。
            # P2 接上 L2.2 的对照运行与 L2.3 的接口快照之后，`blocking_failure`
            # 由那两条判据给出，这把门槛就不需要了。
            out.append(self._completion("T8", "L2 行为回归（最终验证未通过）"))
        if self.cannot_check and (
            changed_symbols or scale_hit or any(item.trigger == "T2" for item in self.recorded)
        ):
            out.append(
                self._completion(
                    "T10",
                    f"高风险改动上带着「查不了」：{self.cannot_check[0]}",
                )
            )
        if high:
            where = "；".join(
                f"{item.where or '（未给位置）'} {item.summary}" for item in high[:5]
            )
            out.append(
                self._completion(
                    "T12",
                    f"自动修 N 轮后高严重度发现仍在（{len(high)} 条）：{where}",
                )
            )
        # ---- T9：L3 高严重度发现未被处理（P2 后半，§3.1 第 3 条）-------------
        #
        # 它与 T12 分开是因为**降级的理由不同**：T12 是「机械判据报过、自动修
        # N 轮后仍在」，而 T9 是「语义审查报了、它没修也没给出机械可核对的
        # 理由」。探针要按触发条件分档（`escalation_by_trigger`），两条混成
        # 一条就查不出是哪一层在拦。
        #
        # **它只降结论、不碰成果**（§3.2.2 处置表把 T9 归在「纯语义」那一档）：
        # 成果已经落盘、可回退、报告逐条列出（§6 P2 验收第 7 条明写：
        # 「L3 发现不构成「成果不许落盘」的理由」，也**不构成「不许删文件」的
        # 理由**）。
        if semantic:
            where = "；".join(
                f"{item.where or '（未给位置）'} {item.summary}" for item in semantic[:5]
            )
            out.append(
                self._completion(
                    "T9",
                    f"L3 语义审查的高严重度发现未被处理（{len(semantic)} 条）：{where}",
                    paths=tuple(
                        sorted({item.path for item in semantic if item.path})
                    ),
                )
            )
        if not has_baseline or not revert_command:
            # T11：**对回退能力还没把握**时拦「动作」，不是拦成果（§3.2.2）。
            out.append(
                self._completion(
                    "T11",
                    "这一轮给不出 Baseline 或整轮回退命令，动作前请示",
                )
            )
        return out

    def _completion(
        self, trigger: str, detail: str, paths: tuple[str, ...] = ()
    ) -> EscalationRequest:
        self._seq += 1
        return EscalationRequest(
            trigger=trigger,
            action="",
            detail=detail,
            paths=paths,
            # 结论级升级不拦任何**尚未执行**的动作，但它同样发生在
            # 「动作执行之前」——它拦下的是「把它当成本次运行的最终结论」。
            before_action=True,
            fingerprint=fingerprint(f"{trigger}|completion|{detail}"),
        )

    def changed_public_symbols(self, after: dict[str, str] | None = None) -> list[str]:
        """公开符号快照里被删或改签名的那些（T1 的机械判据）。

        `after` 是「这些路径**将要**变成什么」（待确认清单里的新正文）。
        必须由调用方给：收尾判这一刻改动**还没落盘**，去读磁盘拿到的是改动前
        那一份，于是每个删掉的函数都判不出来（本模块的 T1 冒烟抓到过）。
        """
        changed: list[str] = []
        for path, before in self._symbols.items():
            if after is not None and path in after:
                text = after[path]
            else:
                try:
                    text = (self.root / path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
            current = public_symbols(text)
            for name, signature in before.items():
                if current.get(name) != signature:
                    changed.append(f"{path}:{name}")
        return changed

    # ---- 请示五件（§3.2.3）----

    def summary_for(
        self,
        request: EscalationRequest,
        *,
        conclusion: str,
        revert_command: str,
        advice: str = "",
        impact: str = "",
    ) -> dict[str, str]:
        """把五件凑齐。**一次说清五件，缺任一即无效请示**。

        被请示的是**动作 / 合并**，不是成果：五件里的「影响」只说被拦下的
        那个动作会动到什么，不重复报「已经落盘了什么」。
        """
        blocked = request.action or "（本轮结论的最终认定）"
        return {
            "impact": impact
            or (
                f"被拦下的动作：{blocked}"
                + (f"，涉及 {len(request.paths)} 个路径" if request.paths else "")
            ),
            "conclusion": conclusion,
            "advice": advice
            or (
                "建议改方案：先看工作区里已经落盘的成果，确认无误再决定是否执行该动作；"
                "要撤成果用下面的回退命令。"
                if request.action
                else "建议先按上面的发现改一轮再宣称完成。"
            ),
            "options": (
                "批准这个动作 / 只批准其中几处 / 撤销（不执行该动作）/ 改方案。"
                "拦住的是**这一次动作**，不是「不许删文件、不许精简」——"
                "删死代码、合并重复实现、删已被替代的文件都是正常编辑，"
                "照样落盘、照样可回退、收尾报告照样逐条列出（§2.5 的反向条款）"
            ),
            "rollback": revert_command or "没有回退：这一处不会落盘（T13，成果没写出来）",
        }

    # ---- 挂起待批与续跑 ----

    def hold(self, request: EscalationRequest) -> None:
        """把这次请示挂起为待批（**成果已经落盘**，挂起的是那个动作）。"""
        if not any(
            item.fingerprint == request.fingerprint for item in self.all_actions
        ):
            self.all_actions.append(request)
        self.pending_actions.append(request)
        self.persist()

    def release(self, request: EscalationRequest) -> None:
        """批复「批准」之后把它从待批清单里去掉。

        「待批」的定义是**还没执行的动作**：批过、而且马上就要执行的那一个
        留在清单里，收尾就会报一句「还有 N 个动作等批复」——而它已经执行了。
        报告里「谁批的、批的哪一个」由 `decisions` 那份审计回答，不靠这份清单。
        """
        self.pending_actions = [
            item for item in self.pending_actions if item.fingerprint != request.fingerprint
        ]
        self.persist()

    def refresh(self, request: EscalationRequest) -> EscalationRequest:
        """把同指纹的那条记录换成**填好五件的**这一份，并原样返回。

        为什么需要它：自动修每一轮都会重算触发条件，而同一条的指纹是按内容算的
        （所以不重复请示）。但第一次登记时那份 `summary` 还是空的，第二轮算完
        五件之后如果只是「返回旧的那份」，收尾报告与 `escalation_summary_complete`
        就会拿到空壳——**同一件事的两次计算结果必须一致**（本模块的冒烟抓到过：
        自动修那一轮的发现清单突然变空，`_review_fixable` 随之判错）。
        """
        self.all_actions = [
            request if item.fingerprint == request.fingerprint else item
            for item in self.all_actions
        ]
        if any(item.fingerprint == request.fingerprint for item in self.pending_actions):
            self.pending_actions = [
                request if item.fingerprint == request.fingerprint else item
                for item in self.pending_actions
            ]
            self.persist()
        return request

    def decided(self, request: EscalationRequest) -> EscalationDecision | None:
        """这条请示有没有拿到过批复（指纹不一致则作废）。"""
        return self.decisions.get(request.fingerprint)

    def remember(self, decision: EscalationDecision) -> None:
        self.decisions[decision.fingerprint] = decision
        self.persist()

    def persist(self) -> None:
        """把**还没执行的动作清单**落一份（跨结算保留的只有它）。

        成果不需要靠暂存保命——它已经在工作区里（§2.5）。所以这里刻意
        只写动作清单与批复，不写任何待确认的正文。
        """
        if self.pending_path is None:
            return
        payload = {
            "run_id": self.run_id,
            "escalated_triggers": list(self.escalated_triggers),
            "pending_actions": [
                {
                    "trigger": item.trigger,
                    "action": item.action,
                    "detail": item.detail,
                    "fingerprint": item.fingerprint,
                    "paths": list(item.paths),
                }
                for item in self.pending_actions
            ],
            "decisions": {
                key: {
                    "approved": item.approved,
                    "word": item.word,
                    "channel": item.channel,
                    "fingerprint": item.fingerprint,
                }
                for key, item in self.decisions.items()
            },
        }
        try:
            self.pending_path.parent.mkdir(parents=True, exist_ok=True)
            self.pending_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            # 挂起清单写不出去不该把运行弄挂：它是续跑的依据，不是成果。
            pass

    def resume_actions(self) -> list[EscalationRequest]:
        """上一次结算留下的待批动作（批复后从挂起点续跑的依据）。"""
        if self.pending_path is None or not self.pending_path.is_file():
            return []
        try:
            payload = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out: list[EscalationRequest] = []
        for item in payload.get("pending_actions") or []:
            if not isinstance(item, dict):
                continue
            out.append(
                EscalationRequest(
                    trigger=str(item.get("trigger") or ""),
                    action=str(item.get("action") or ""),
                    detail=str(item.get("detail") or ""),
                    paths=tuple(item.get("paths") or ()),
                    fingerprint=str(item.get("fingerprint") or ""),
                )
            )
        return out


# ---- 解析批复（三通道共用同一张词表）---------------------------------------


def parse_reply(word: str) -> str:
    """批复词 → 四档之一。认不出来一律按「不执行这个动作」处理。

    认不出来时**不静默放行**：放行一次不可逆动作的代价远大于多问一次。
    """
    clean = str(word or "").strip().lower()
    for name, words in REPLIES.items():
        if clean in words:
            return name
    return "revert"


def contains_cannot_check(text: str) -> bool:
    """这句验证结论里有没有「查不了」（T10 的原料，复用既有口径）。

    `verify.py` 的 `CANNOT_CHECK` 是「查不了」的既有标记；这里按同一个
    字面量认，不另造一套。
    """
    return "查不了" in str(text or "")


# ---- 渲染（终端 / 聊天 / 事件流共用一份五件）--------------------------------


def render_summary(request: EscalationRequest) -> str:
    """五件各一行 + 一条撤销命令（§3.2.3 的终端形态）。

    **不贴 diff**：请示给的是五件摘要。输出从调用方给的口子出去，
    否则 `--events` 的 JSON 行会被污染（`cli/approval.py` 记着那次事故）。
    """
    parts = request.summary
    lines = [
        f"【待批复】触发条件 {request.trigger}：{request.note}",
        f"① 影响：{parts.get('impact', '（未填）')}",
        f"② 结论：{parts.get('conclusion', '（未填）')}",
        f"③ 建议：{parts.get('advice', '（未填）')}",
        f"④ 选项：{parts.get('options', '（未填）')}",
        f"⑤ 回退：{parts.get('rollback', '（未填）')}",
        "默认答案是「不执行这个动作」（成果已经落在工作区里，不在这里批）。",
    ]
    return "\n".join(lines)


def render_decisions(gate: EscalationGate) -> str:
    """本次运行的升级与批复摘要（收尾报告里那一段）。

    按 `all_actions` 逐条列（**不是**待批清单）：批过并已经执行的那些也必须
    留在报告里——审计要回答的是「这一轮请示过什么、谁批的」，而待批清单
    只回答「还有什么没执行」。
    """
    if not gate.all_actions and not gate.decisions:
        return ""
    lines: list[str] = []
    for request in gate.all_actions:
        decision = gate.decisions.get(request.fingerprint)
        state = decision.approved if decision else PENDING
        lines.append(
            f"- {request.trigger}（{request.note}）：{request.action or '结论级'} "
            f"→ {state}"
            + (
                f"，批复词「{decision.word}」，通道 {decision.channel}"
                if decision
                else ""
            )
            + f"，摘要指纹 {request.fingerprint}"
        )
    pending = [
        item
        for item in gate.pending_actions
    ]
    tail = (
        f"\n挂起的动作清单：{gate.pending_label}（批复后从挂起点续跑）"
        if pending
        else "\n（本次运行的被拦动作都已得到批复，没有还挂着没执行的）"
    )
    return (
        "\n本次运行的风险分级放行（拦的是动作，成果已落盘）：\n"
        + "\n".join(lines)
        + tail
    )


def _matches_token(text: str, token: str) -> bool:
    """整词匹配 `token`（`auth` 命中 `auth.py`，不命中 `author.py`）。"""
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None
