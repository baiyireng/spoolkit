"""自动审查层 P2 后半：L3 语义审查（设计稿 §5 L3 / §3.1 / §3.2.0 / §2.5）。

L1（`spoolkit.agent.audit`）判静态一致性，L2（`spoolkit.agent.review_behavior`）
判行为回归。两层都是机械的，因而有一条共同盲区：**「看起来对、其实没做到」**。
典型现场就是探针 `rename-cross-file` 首轮——

    结论写着「整个项目里都统一了」，而 README 与 docs 里的旧名一个没动；
    L1 的残留检测当时跳过了本文件与已扫路径（§1.7 G2），而「统一了」这句话
    本身没有任何机械判据能证伪。

这一层补的就是它，用模型，但三条纪律一条都不能少：

1. **独立上下文**（§9.2「不要做模型自己审自己」）。审查者**只看** diff +
   用户请求 + 证据（跑过什么、结果如何、工作区最终状态），**不看实现者的
   自我陈述**：`ReviewMaterials` 里**没有** `final` 这个字段，主循环那一段
   总结一个字都进不来。这不是省事，是判据本身——把总结喂进去，它只会附和
   那一段（督导证据包里刻意不含对话历史是同一个理由，
   `agents/supervisor.py::Evidence`）。
2. **结构化发现**（§7.6「不引入 LLM 自由文本判定」）。每条 =
   `file:line` + 问题 + 严重度 + 证据；解析不出来**按未通过**处理
   （沿用 `agents/dispatcher.py:96` 的口径），「没结论」与「判不合格」分开
   （同文件 `:67`）。
3. **每条事实性断言都要过机械核对**（§1.3、§4「宁可漏判也不误报」）。
   `agents/supervisor.py` 的 `RunFacts` / `fact_conflicts` / `sanitize_verdict`
   就是被督导幻觉坑出来的那一套，这里**直接复用**，只把断言形状扩到 L3 需要的
   几种：说工具不存在、说零改动、说**某一处声称的改动在 diff 里找不到**。
   与记录冲突的断言 → 那条发现**降级为 `unverified`**（severity 降到提示档）、
   从「据此阻断完成声明」的那一份里剔除，并把「与记录不符」记进事件与报告。

**触发条件**（§5：**不是每步都跑**，成本随步数线性放大）：
L1/L2 有发现 / L2 回归 / 改动规模越线 / 存在没有证据的完成声明 / 显式要求。
`decide_triggers()` 是判据本体，阈值全部走 `limits.py` 的旋钮。

**阻断语义与 L1/L2 完全一致**（§3.1 第 3 条、§3.2.0）：高严重度发现未被处理时
**不许宣称完成**（「未完成为 + 逐条 `file:line`」），但**成果照落盘**、可回退、
报告逐条列出（§2.5 的硬不变量：绝不留空工作区）。这一层**不碰落盘判定**，
唯一的出口是 `findings` 与 `report.render()`。

**§2.5 的反向条款**（本模块的模型可见文本同样适用，见 `render_unverified`）：
拦的是「完成声明」与危险动作，**不是成果**——删死代码、合并重复实现、
删已被替代的文件都是正常编辑，照样落盘、照样可回退、报告照样逐条列出。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

from spoolkit.agents.supervisor import (
    FactConflict,
    RunFacts,
    fact_conflicts,
)
from spoolkit.agent.audit import HIGH, INFO, Finding

# ---- 严重度（L3 schema 的三档；与 `audit` 的两档显式对上）--------------------
#
# schema 用 `high|medium|low`（设计稿 §5 L3 的发现形状逐字如此），而账本与
# 收尾判据只认 `audit` 的两档（`高` / `提示`）。两套名字之间的关系写在一处，
# 免得每个消费点各自猜一遍「medium 算不算拦完成声明」。
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

#: L3 严重度 → 账本严重度。**只有 high 会拦完成声明**（§3.1 第 3 条：
#: 「L3 高严重度发现未被处理」），medium/low 是给人看的读数——把 medium 也
#: 算成阻断，等于把「值得看一眼」升级成「不许说做完」，告警疲劳就是这么来的
#: （§4「宁可漏判也不误报」）。
SEVERITY_TO_AUDIT: dict[str, str] = {
    SEVERITY_HIGH: HIGH,
    SEVERITY_MEDIUM: INFO,
    SEVERITY_LOW: INFO,
}
AUDIT_SEVERITIES = (SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW)

# ---- 判据名与事件流 kind -----------------------------------------------------
#
# 与 L1/L2 同一套做法：判据名在产出处定义，事件流的 kind 由它映射
# （`audit.note_kind`），探针**按 kind 数**、不读散文。
#: L3 的发现（探针进 `review_findings`，并单独数 `l3_findings`）。
L3_FINDING_KIND = "semantic_finding"
#: L3 的过程读数（触发条件、跑没跑、被剔除几条、为什么没给结论）——
#: **不是发现**，与 L2 的 `behavior_readings` 同一个口径。
L3_READINGS_KIND = "l3_readings"
#: L3 发现的**结构化**那一份（每条带 `file`/`line`/`severity`/`evidence` 与
#: 「过没过事实核对」）。它与上面那条发现**成对**：发现那条是给人读的一句话
#: （与 L1/L2 同一个出口），这一条是给壳与探针读的字段。缺了它，探针就只能去
#: 解析散文来数「每条发现过没过核对」（§4 明说不许这么干）。
L3_DETAIL_KIND = "l3_detail"
#: 「L3 判了一次，结论是拦下完成声明」。
L3_BLOCKED_KIND = "l3_blocked"

#: 事实核对的四种冲突形状（每条冲突带一个 id，事件与探针按它分档）。
CONFLICT_TOOL_MISSING = "tool_missing"
CONFLICT_NO_EDIT = "no_edit"
CONFLICT_CHANGE_MISSING = "change_missing"
CONFLICT_UNKNOWN_FILE = "unknown_file"

#: 「查不了」的既有字面量（`tools/verify.CANNOT_CHECK`），**不另造一套**。
CANNOT_CHECK = "【查不了】"

#: 兜底阈值：登记表里给了标定值就用标定值（`Config.limit`），这是没登记时的数。
#: 与 `review_gate.DEFAULT_TRIGGER_FILES` 同量级（设计稿 §5 第 3 条要求
#: 「与 `review_limit` 同量级」）。
DEFAULT_TRIGGER_FILES = 5
DEFAULT_MAX_FINDINGS = 12
DEFAULT_MAX_TOKENS = 700

#: 喂给审查者的 diff 总量上限（字符）。它是**噪声控制**，不是判据：改动太大时
#: 一次审查也读不完，超出的部分截断并如实标明——静默截断会让审查者以为
#: 自己看到了全部（§4）。
MAX_DIFF_CHARS = 40000
#: 单条证据/问题在报告里的摘录上限。
_EXCERPT = 200


class ChatLike(Protocol):
    """一次模型调用。判据只依赖这一个方法，所以用例注入假 provider 即可。

    与 `agents/supervisor.supervise` 拿整个 `ModelGateway` 不同：这一层只需要
    「发一段话、拿一段文字」，把接口收到最窄，注入面就最小。
    """

    def chat(self, request: Any) -> Any:  # pragma: no cover - 协议声明
        ...


# ---- 送审材料（独立上下文的落点）--------------------------------------------


@dataclass(frozen=True)
class ChangeFacts:
    """一处改动的机械事实。**只由改动本身与磁盘状态算出**，不看模型怎么说。"""

    path: str
    #: 改动前有内容（既有文件被改写）。
    edited: bool = False
    #: 这次是新建（改动前不存在）。
    created: bool = False
    #: 这次是删除。
    deleted: bool = False
    added: int = 0
    removed: int = 0

    @property
    def verb(self) -> str:
        if self.deleted:
            return "删除"
        if self.created:
            return "新建"
        return "修改"


@dataclass(frozen=True)
class ReviewMaterials:
    """交给 L3 审查者的**全部**信息。

    **刻意没有 `final`（实现者的自我陈述）**：喂进去它就只会附和那一段，
    而这一层存在的唯一理由是回答「它声称做到了，做到了吗」。
    这不是「暂时没接」，是这一层的独立上下文边界（§9.2）。
    """

    #: 用户/调用方原话。
    goal: str = ""
    #: 这次改动的机械事实（逐处）。
    changes: tuple[ChangeFacts, ...] = ()
    #: 改动差异全文（`PendingChange.diff` 拼起来，超上限截断）。
    diff: str = ""
    #: L1 机械一致性的发现（判据 + `file:line` + 摘要）。
    mechanical: tuple[str, ...] = ()
    #: L2 行为回归的读数与发现。
    behavior: tuple[str, ...] = ()
    #: 自动验证跑过什么、结果如何（**原话**，不是总结）。
    checks_run: tuple[str, ...] = ()
    #: 这一轮给不给得出整轮回退命令（§2.5 第 2 条）。
    revert_command: str = ""
    #: 有没有回退能力。缺回退时成果不许落盘（T13），报告也要点名。
    has_baseline: bool = True
    #: 工作区最终状态逐条（`_file_state_line` 的那份）。
    workspace: str = ""
    #: 更严的上限：把 diff 截到多少字符（用例可调小来证伪「截断会标明」）。
    max_diff_chars: int = MAX_DIFF_CHARS

    @property
    def blocked(self) -> bool:
        """这次运行**没有任何产出**：没有改动就没有「做到了没有」可审。"""
        return not self.changes

    def facts(self, tools: frozenset[str] = frozenset()) -> ReviewFacts:
        """这些材料里**机械可核对**的那部分事实。

        `tools` 由调用方给（主循环传的是**注册表**里的名字，不是模型叙述里的
        字）——材料本身不含工具清单，因为那是运行台账，不是送审材料：审查者
        不需要看到它能调哪些工具，但事实核对需要。
        """
        return ReviewFacts(
            tools=tools,
            changed=frozenset(item.path for item in self.changes),
            edits=sum(1 for item in self.changes if item.edited or item.created),
            deleted=frozenset(item.path for item in self.changes if item.deleted),
            checks_run=tuple(self.checks_run),
        )


@dataclass(frozen=True)
class ReviewFacts:
    """L3 事实核对用的记录。**是 `RunFacts` 的扩展，不是另一套。**

    `as_run_facts()` 把共有的三项交回 `supervisor.fact_conflicts` ——被督导
    幻觉坑出来的那两条判据（「工具不存在」「0 处改动」）因此**逐字复用**，
    不在这一层重写一遍。多出来的三项是 L3 才需要的形状：

    - `deleted`：被删的路径。结论说「已删除 X」时，X 在不在这一份里可查。
    - `checks_run`：跑过哪些检查。结论拿某个检查当依据时，它得真跑过。
    - `tools`：注册过的工具名（与 `RunFacts` 同名同义，直接透传）。
    """

    tools: frozenset[str] = frozenset()
    edits: int = 0
    changed: frozenset[str] = frozenset()
    deleted: frozenset[str] = frozenset()
    checks_run: tuple[str, ...] = ()

    def as_run_facts(self) -> RunFacts:
        """交给 `supervisor` 那一套判据的视图（共有三项）。"""
        return RunFacts(tools=self.tools, edits=self.edits, changed=self.changed)


# ---- 事实核对：与记录冲突的断言 ---------------------------------------------
#
# 三种形状各自机械可查，判据只看记录（`ReviewFacts`），不看措辞是否漂亮：
#
#   1. 说某把工具不存在 → 注册表里有没有（复用 supervisor 的那一条）；
#   2. 说这次运行零改动 → 改动台账（同样复用）；
#   3. 说**某一处改动在 diff 里找不到** → diff 的路径集合。
#
# 第 3 条是 L3 特有的、也是最要紧的一条：claim-vs-artifact 的反面——
# 审查者自己也会说错（它可能把「L1 没报」读成「diff 里没有这处改动」）。
# 与记录冲突的发现不据以阻断，只降级、标「与记录不符」（§4）。

#: 断言「**这处改动不存在**」。措辞刻意收得很窄——只认「否认这处改动本身」的
#: 说法。写成「diff 里没有 X」（X 是**文件**）是另一回事：那是一条「文件漏改了」
#: 的发现，把它读成「断言这处改动不存在」正好把这一层最该抓到的东西筛掉
#: （`rename-cross-file` 那类现场里，最值钱的一条就是「README 没在 diff 里」）。
_NO_CHANGE_CLAIMS = (
    "没有这处改动",
    "没有该处改动",
    "没有这个改动",
    "没有这项改动",
    "这处改动不存在",
    "该处改动不存在",
    "这个改动不存在",
    "这处修改不存在",
    "这处改动不在 diff",
    "没有做这处修改",
    "没有做这个修改",
    "没有做这处改动",
    "没有做这个改动",
    "该文件没有被改",
    "这个文件没有被改",
    "该文件未被修改",
    "这个文件未被修改",
)

#: 断言「全部/所有都改了」这类**全局完成声明**。它是 §3.1 第 3 条真正要拦的
#: 形状（首轮 `rename-cross-file` 就是它），但这一层**不靠关键词判它真假**：
#: 只把它当线索送给 L1/L2 的机械判据（残留引用 / 行为回归），那两条才是判据。
_GLOBAL_COMPLETION = (
    "所有",
    "全部",
    "都改了",
    "都统一",
    "已经统一",
    "统一了",
    "一处不漏",
    "无残留",
    "没有残留",
    "every",
    "all call sites",
)


def _declares_global_completion(text: str) -> bool:
    """这句话是不是一条**全局完成声明**（供报告与读数标注，不作判据）。"""
    return any(word in text for word in _GLOBAL_COMPLETION)


def _claims_no_change(text: str) -> bool:
    return any(word in text for word in _NO_CHANGE_CLAIMS)


def _mentioned_paths(text: str, known: Sequence[str]) -> list[str]:
    """这句话点了哪些**已知路径**（`known` 是这次改动的路径集合）。"""
    probe = text.replace("\\", "/")
    return sorted(
        {
            path
            for path in known
            if path and path.replace("\\", "/") in probe
        }
    )


def review_conflicts(finding: Any, facts: ReviewFacts) -> list[FactConflict]:
    """这条发现里有没有与运行记录冲突的事实性断言（复用督导那一套形状）。

    返回非空 → 该条**降级为 `unverified`**：severity 落到提示档、从「据此阻断
    完成声明」的那一份里剔除，但**不删**——它仍进事件流与报告，只是标上
    「与记录不符」，好让人一眼看出这条判词不成立（`supervisor._rewrite`
    是同一个处置）。
    """
    text = " ".join(
        str(part or "")
        for part in (
            getattr(finding, "problem", ""),
            getattr(finding, "evidence", ""),
            getattr(finding, "suggestion", ""),
        )
    )
    if not text.strip():
        return []
    found: list[FactConflict] = []
    # ①② 复用督导那两条判据（工具不存在 / 零改动），逐字不重写。
    found.extend(fact_conflicts(text, facts.as_run_facts()))
    # ③ 说某处改动不存在，而记录里它明明改过。
    if facts.edits > 0 and _claims_no_change(text):
        where = "、".join(sorted(facts.changed)) or "（未记到路径）"
        found.append(
            FactConflict(
                claim="声称某一处改动不存在（diff 里没有）",
                truth=(
                    f"记录：本次运行有 {facts.edits} 处改动落成（{where}）；"
                    "「不在 diff 里」与改动台账相反"
                ),
            )
        )
    return found


def _conflict_kind(conflict: FactConflict) -> str:
    """一条冲突归到哪个形状（事件与探针按它分档）。"""
    claim = conflict.claim
    if "工具不存在" in claim:
        return CONFLICT_TOOL_MISSING
    if "0 处改动" in claim:
        return CONFLICT_NO_EDIT
    return CONFLICT_CHANGE_MISSING


# ---- 发现（结构化，每条带 file:line 与证据）---------------------------------


@dataclass(frozen=True)
class SemanticFinding:
    """一条 L3 发现。**形状就是 schema 的形状**（设计稿 §5 L3 那个 JSON）。

    `line` 可以是 0（审查者只知道文件、指不出行）。那时 `line_known` 为假，
    `where` 只给文件——**不编造一个行号**（§4：每条发现必须带 `file:line`
    与机械证据，否则不上报；编出来的行号比不给更坏）。
    """

    file: str
    line: int
    problem: str
    severity: str = SEVERITY_MEDIUM
    suggestion: str = ""
    evidence: str = ""
    #: 这条断言过没过机械核对（False → 已降级为提示档，不据以阻断）。
    verified: bool = True
    #: 与记录冲突的那几句（降级理由；`verified=True` 时为空）。
    conflicts: tuple[FactConflict, ...] = ()

    @property
    def line_known(self) -> bool:
        return self.line > 0

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}" if self.line_known else self.file

    @property
    def audit_severity(self) -> str:
        """落到账本上的严重度（`高` / `提示`）。"""
        if not self.verified:
            return INFO
        return SEVERITY_TO_AUDIT.get(self.severity, INFO)

    @property
    def blocking(self) -> bool:
        """这条发现算不算「未处理的高严重度」（§3.1 第 3 条）。"""
        return self.verified and self.severity == SEVERITY_HIGH

    @property
    def global_claim(self) -> bool:
        """这句话是不是一条全局完成声明（读数用，不作判据）。"""
        return _declares_global_completion(self.problem)

    def conflict_note(self) -> str:
        """「与记录不符」那一行（空串 = 没有冲突）。"""
        if not self.conflicts:
            return ""
        return "；".join(f"{item.claim}→{item.truth}" for item in self.conflicts)

    def render(self) -> str:
        head = "【高危】" if self.blocking else "·"
        note = "（与记录不符，已按运行记录降级）" if not self.verified else ""
        body = f"{head} {self.where} {self.problem}{note}"
        tail = f"（证据：{self.evidence}）" if self.evidence else ""
        return body + tail

    def to_payload(self) -> dict[str, Any]:
        """进事件流的形状（探针与网页壳按这些键读，**不读散文**）。"""
        return {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "verified": self.verified,
            "blocking": self.blocking,
            "global_claim": self.global_claim,
            "problem": self.problem[:_EXCERPT],
            "suggestion": self.suggestion[:_EXCERPT],
            "evidence": self.evidence[:_EXCERPT],
            "conflicts": [
                {"kind": _conflict_kind(item), "claim": item.claim, "truth": item.truth}
                for item in self.conflicts
            ],
        }


# ---- 触发条件（§5：满足其一才跑，不是每步都跑）-------------------------------


@dataclass(frozen=True)
class TriggerDecision:
    """跑不跑 L3，以及为什么。**两个方向都要有理由**（可证伪）。"""

    run: bool
    reasons: tuple[str, ...] = ()
    skipped: str = ""

    @property
    def note(self) -> str:
        if self.run:
            return "；".join(self.reasons)
        return self.skipped or "没有命中任何触发条件"


#: 触发条件 id（与设计稿 §5 的五条一一对应）。事件流与报告点名用。
TRIGGER_IDS: dict[str, str] = {
    "F1": "L1/L2 有发现",
    "F2": "L2 行为回归",
    "F3": "改动规模越线",
    "F4": "存在没有证据的完成声明",
    "F5": "调用方显式要求",
}


def decide_triggers(
    *,
    finding_count: int = 0,
    regression: bool = False,
    files: int = 0,
    lines: int = 0,
    evidence_gaps: int = 0,
    explicit: bool = False,
    max_files: float = DEFAULT_TRIGGER_FILES,
    lines_per_file: float = 80,
) -> TriggerDecision:
    """这次该不该跑 L3。判据全部机械（§5 的五条）。

    **「不每步都跑」是这一层的成本纪律**：一次审查 ≈ 主循环一步的量级，
    每步都跑就是成本×步数（§5、§7.5）。所以默认路径（没有发现、没有回归、
    规模小、没有无证据的完成声明、没显式要求）一次模型调用都不发生。

    `lines_per_file` 把「行数」折成同一个规模线：`max_files × 80`——量级依据
    见 `review_gate.trigger_lines`（5 × 80 = 400 行）。
    """
    reasons: list[str] = []
    if explicit:
        reasons.append(f"F5 {TRIGGER_IDS['F5']}（--review）")
    if finding_count > 0:
        reasons.append(f"F1 {TRIGGER_IDS['F1']}（{finding_count} 条）")
    if regression:
        reasons.append(f"F2 {TRIGGER_IDS['F2']}")
    threshold = max(1.0, float(max_files))
    line_threshold = max(1.0, float(lines_per_file)) * threshold
    if int(files) >= int(threshold) or int(lines) >= int(line_threshold):
        reasons.append(
            f"F3 {TRIGGER_IDS['F3']}（文件 {int(files)}/{int(threshold)}，"
            f"行数 {int(lines)}/{int(line_threshold)}）"
        )
    if evidence_gaps > 0:
        reasons.append(f"F4 {TRIGGER_IDS['F4']}（{evidence_gaps} 条）")
    if reasons:
        return TriggerDecision(True, tuple(reasons))
    return TriggerDecision(
        False,
        (),
        (
            "没有命中任何触发条件（L1/L2 无发现、无回归、规模 "
            f"{int(files)} 个文件/{int(lines)} 行未越线、无未验证的完成声明、"
            "没有显式要求）——**这一次没有调用模型**"
        ),
    )


# ---- 送审材料 → 提示 ---------------------------------------------------------


@dataclass(frozen=True)
class SemanticReport:
    """一次 L3 审查的完整读数。**它只报事实**，判成什么由 `findings` 决定。"""

    ran: bool = False
    #: 跑没跑的理由（`TriggerDecision.note`）。
    trigger: str = ""
    #: 审查者给出的结论（`pass` / `fail` / `""` = 没给）。
    verdict: str = ""
    reasons: tuple[str, ...] = ()
    findings: tuple[SemanticFinding, ...] = ()
    #: 被事实核对降级的条数（探针的 `l3_unverified`）。
    unverified: int = 0
    #: 冲突分档（形状 → 条数）。
    conflicts: dict[str, int] = field(default_factory=dict)
    #: 「查不了」/「没得出结论」的具体理由（空 = 给结论了）。
    cannot_check: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def blocking(self) -> tuple[SemanticFinding, ...]:
        """据以拦「完成声明」的那一份（已过事实核对的高严重度发现）。"""
        return tuple(item for item in self.findings if item.blocking)

    @property
    def blocked(self) -> bool:
        return bool(self.blocking)

    def by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.findings:
            out[item.severity] = out.get(item.severity, 0) + 1
        return out

    def render(self) -> str:
        """一段人读的读数（过程材料：进事件流与 trace，不进结论报告）。"""
        if not self.ran:
            return f"{CANNOT_CHECK}L3 语义审查：{self.trigger or '没跑'}"
        if self.cannot_check:
            return f"{CANNOT_CHECK}L3 语义审查：{self.cannot_check}"
        severity = "、".join(f"{key} {value}" for key, value in self.by_severity().items())
        parts = [
            f"L3 语义审查跑了（{self.trigger}）：{len(self.findings)} 条发现"
            + (f"（{severity}）" if severity else "")
        ]
        if self.unverified:
            parts.append(f"其中 {self.unverified} 条与记录不符、已降级")
        if self.verdict:
            parts.append(f"审查者结论 {self.verdict}")
        return "；".join(parts)


#: L3 的提示词。**三项材料 + 输出契约**，仅此而已。
#:
#: 为什么模板写在模块里而不是 `context/templates.py`：那棵树被
#: `tests/test_prompt_mechanics.py` 逐字钉着（system 区段的 token 冻结线），
#: 而这一段**不进 main 循环的 system 区段**——它只发给审查者那一次调用。
#: 放在这个模块里，判据、契约与措辞同生共死。
REVIEW_PROMPT = """你是一次独立代码审查。你**只看**三样东西：用户的请求、改动差异（diff）、
已经跑过的机械检查及其结果。**你看不到实现者的总结，也不要假设它说的是对的**——
你唯一的任务就是回答一个问题：**「它声称做到了，做到了吗」。**

用户的请求：
{goal}

改动差异（{change_count} 处改动{diff_note}）：
{diff}

已经跑过的机械检查与结果：
{evidence}

要求：

1. **对抗性**：专门找「声称做到了但没做到」。例如：改动自称覆盖了全部调用点，
   而 diff 里还留着没改的那一处；结论里的行为与 diff 实际做的事不一致。
2. **每条发现必须给出 `file` 与 `line` + 依据（`evidence`）**。指不出位置、
   或只是风格偏好，就不要写。风格问题最多算 `low`，并且不要拿它拦任何事。
3. **只说问题**：不要复述改动做对了什么，不要给通过意见。
4. **判不出来就说判不出来**：材料不足以判断时，`findings` 给空表，
   在 `reasons` 里写清缺什么。不要凭猜测编一条发现。
5. 只输出 JSON，不要任何解释文字。没有问题时 `verdict` 用 `pass`。

JSON 形状：
{{"findings": [{{"file": "路径", "line": 12, "problem": "问题一句话",
  "severity": "high|medium|low", "suggestion": "建议", "evidence": "依据（引用 diff 或某条检查结果）"}}],
  "verdict": "pass|fail", "reasons": ["..."]}}
"""

#: 响应契约（约束解码用）。`severity` 用 enum，取值与 `SEVERITY_TO_AUDIT` 对齐。
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "problem": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": [SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW],
                    },
                    "suggestion": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["file", "problem", "severity"],
            },
        },
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "verdict"],
}


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def render_diff(materials: ReviewMaterials) -> tuple[str, bool]:
    """拼出送审的 diff；返回（正文, 是否截断）。

    截断**必须说出来**：静默截断会让审查者以为自己看到了全部改动，
    于是它会把「我没看到」说成「没有这处改动」——那正好是这一层要防的形状。
    """
    body = materials.diff
    if not body:
        body = "\n".join(
            f"{item.verb} {item.path}（+{item.added} / -{item.removed}）"
            for item in materials.changes
        )
    return _clip(body, max(200, int(materials.max_diff_chars)))


def _render_evidence(materials: ReviewMaterials) -> str:
    lines: list[str] = []
    if materials.checks_run:
        lines += [f"- 跑过的检查：{one}" for one in materials.checks_run]
    else:
        lines.append(f"- {CANNOT_CHECK}这次运行没有跑过任何检查")
    if materials.mechanical:
        lines += [f"- L1 机械一致性：{one}" for one in materials.mechanical]
    else:
        lines.append("- L1 机械一致性：没有报出发现")
    if materials.behavior:
        lines += [f"- L2 行为回归：{one}" for one in materials.behavior]
    else:
        lines.append("- L2 行为回归：没有报出发现")
    lines.append(
        "- 回退能力："
        + (
            f"有（{materials.revert_command}）"
            if materials.has_baseline and materials.revert_command
            else "没有（按 T13，这一处不许落盘，报告里要点名）"
        )
    )
    if materials.workspace:
        lines.append(f"- 工作区最终状态：{materials.workspace}")
    return "\n".join(lines)


def build_prompt(materials: ReviewMaterials) -> str:
    """把材料拼成一次审查的提示。

    **只喂 diff + 用户请求 + 证据**：`ReviewMaterials` 里根本没有实现者的
    自我陈述，所以「不把总结喂给审查者」是**结构上**成立的，不靠调用方记得
    少传一个参数（§9.2）。
    """
    diff, truncated = render_diff(materials)
    note = "，**已截断**（后面的改动你没看到，不要据此断言某处不存在）" if truncated else ""
    return REVIEW_PROMPT.format(
        goal=materials.goal or "（没有给出请求原文）",
        change_count=len(materials.changes),
        diff_note=note,
        diff=diff or "（没有改动）",
        evidence=_render_evidence(materials),
    )


# ---- 解析（解析不出来按未通过；「没结论」与「判不合格」分开）----------------


@dataclass(frozen=True)
class ParsedReview:
    """解析结果。`ok=False` 时 `why` 说明是「没结论」还是「格式不对」。"""

    ok: bool
    findings: tuple[SemanticFinding, ...] = ()
    verdict: str = ""
    reasons: tuple[str, ...] = ()
    why: str = ""


def _as_line(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0
    return max(0, int(raw))


def _as_text(raw: Any) -> str:
    if isinstance(raw, str):
        return " ".join(raw.split())
    if isinstance(raw, (list, tuple)):
        return " ".join(" ".join(str(item).split()) for item in raw if item)
    if raw is None:
        return ""
    return " ".join(str(raw).split())


def _clean_path(raw: Any, root: Path | None) -> str:
    """把审查者给的路径规范化成**工作区相对 posix**。

    绝对路径能算相对就算（模型常给绝对路径）；算不出来就原样保留——
    那一条会被 `unknown_file` 那条判据看见（它不是这次改动的路径）。
    """
    text = _as_text(raw).replace("\\", "/")
    # 去掉常见的前缀写法（`` `a/b.py` `` / `./a/b.py`）。
    text = text.strip("`'\" ")
    while text.startswith("./"):
        text = text[2:]
    if root is not None and text:
        candidate = Path(text)
        if candidate.is_absolute():
            try:
                return candidate.resolve().relative_to(Path(root).resolve()).as_posix()
            except (ValueError, OSError):
                return text
    return text


def parse_review(
    text: str, *, root: Path | None = None, max_findings: int = DEFAULT_MAX_FINDINGS
) -> ParsedReview:
    """把审查者那段 JSON 判成发现清单。

    两条纪律：

    - **解析不出来按未通过处理**，但**「没结论」与「格式不对」分开**
      （`agents/dispatcher.py:67` 的教训）。这里两者都不据以阻断完成声明
      ——它们只是「这次审查没给出东西」，把它当成缺陷会让每次失败的调用
      都去拦一次完成声明，那是另一种误报（§4）。理由如实带上。
    - **发现条数设上限**（`max_findings`，§4 的噪声控制）：超出的部分丢掉，
      但把丢了几条如实报出来——超上限说明改动太大、应当拆。
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ParsedReview(False, why="审查结论无法解析（不是合法 JSON），按未通过处理")
    if not isinstance(payload, dict):
        return ParsedReview(False, why="审查结论格式不对（不是对象），按未通过处理")
    raw_findings = payload.get("findings")
    if raw_findings is None:
        return ParsedReview(
            False,
            verdict=str(payload.get("verdict") or ""),
            why="审查结论里没有 findings（没有结论，不等于判不合格）",
        )
    if not isinstance(raw_findings, list):
        return ParsedReview(False, why="findings 不是列表，按未通过处理")

    findings: list[SemanticFinding] = []
    kept: set[tuple[str, int, str]] = set()
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        problem = _as_text(item.get("problem"))
        path = _clean_path(item.get("file"), root)
        if not problem and not path:
            # 位置与问题都没有：**这条不上报**（§4「指不到就不上报」）。
            continue
        severity = _as_text(item.get("severity")).lower() or SEVERITY_MEDIUM
        if severity not in AUDIT_SEVERITIES:
            severity = SEVERITY_MEDIUM
        line = _as_line(item.get("line"))
        signature = (path, line, problem)
        if signature in kept:
            continue
        kept.add(signature)
        findings.append(
            SemanticFinding(
                file=path or "（未给文件）",
                line=line,
                problem=problem or "（没写清问题）",
                severity=severity,
                suggestion=_as_text(item.get("suggestion")),
                evidence=_as_text(item.get("evidence")),
            )
        )
    reasons = tuple(
        _as_text(one) for one in (payload.get("reasons") or []) if _as_text(one)
    )
    verdict = _as_text(payload.get("verdict")).lower()
    if len(findings) > max(1, int(max_findings)):
        findings = findings[: max(1, int(max_findings))]
        reasons = reasons + (
            f"（发现条数超过上限 {int(max_findings)}，超出部分已丢弃——"
            "改动规模建议拆小）",
        )
    return ParsedReview(True, tuple(findings), verdict, reasons)


# ---- 事实核对之后：降级与剔除 ------------------------------------------------


@dataclass(frozen=True)
class CheckedFindings:
    """过了机械核对的那一份，以及被降级/剔除的那些。"""

    kept: tuple[SemanticFinding, ...] = ()
    unverified: tuple[SemanticFinding, ...] = ()
    conflicts: dict[str, int] = field(default_factory=dict)

    @property
    def notable(self) -> tuple[SemanticFinding, ...]:
        """据以拦「完成声明」的那一份（已过核对的高严重度发现）。"""
        return tuple(item for item in self.kept if item.blocking)


def check_findings(
    findings: Sequence[SemanticFinding], facts: ReviewFacts
) -> CheckedFindings:
    """逐条过事实核对。冲突 → 降级为 `unverified`（**不删**）。

    还有一条**位置判据**（不叫「事实冲突」，因为它不是「与记录相反」，
    而是「你指的位置这次没动过」）：发现指向的路径既不在这次改动的路径集合里，
    也不是工作区里存在的文件时，这条发现同样降级——审查者看着一份被截断的
    diff 说「你在 X 里漏了一处」，而 X 这次压根没动过，那是纯噪声（§4）。
    """
    kept: list[SemanticFinding] = []
    unverified: list[SemanticFinding] = []
    counters: dict[str, int] = {}
    for item in findings:
        found = review_conflicts(item, facts)
        if found:
            for conflict in found:
                key = _conflict_kind(conflict)
                counters[key] = counters.get(key, 0) + 1
            unverified.append(
                replace(item, verified=False, conflicts=tuple(found))
            )
            continue
        kept.append(item)
    return CheckedFindings(tuple(kept), tuple(unverified), counters)


def known_paths(materials: ReviewMaterials, root: Path | None = None) -> set[str]:
    """**白名单**：允许成为一条未被核对掉的发现的位置。

    两条来源，都是机械的：

    - 这次改动过的路径（改动台账）；
    - **工作区里真实存在的文件**。

    第二条是必须的，而且它是这条判据的对立面：`rename-cross-file` 那类现场里
    最值钱的一条发现恰恰是「README／docs 里的旧名没动」——那些文件**不在**
    diff 里，因为根本没人改它们。只用第一条的话，这条真发现会被当成「你指的
    位置这次没动过」而降级，那是把这一层最该抓到的东西筛掉（§4 的「宁可漏判
    也不误报」说的是**不要编造**，不是不要报「漏改」）。

    所以判据只排除**工作区里压根不存在**的位置：路径写错、跳出工作区、
    或者凭空编一个文件——那才是噪声。
    """
    out = {item.path.replace("\\", "/") for item in materials.changes}
    return out


def within_root(root: Path | None, path: str) -> bool:
    """这个位置在工作区里**真实存在**吗（判据是磁盘，不是措辞）。"""
    text = str(path or "").replace("\\", "/").strip()
    if not text or text.startswith("（"):
        return False
    if root is None:
        # 没有工作区可比时**不排掉**：宁可漏判也不误报的另一面是
        # 「拿不到记录就不能假装核对过了」（`supervisor.sanitize_verdict`
        # 对 `facts=None` 的处理就是这一条）。
        return True
    try:
        candidate = Path(text)
        target = (
            candidate.resolve()
            if candidate.is_absolute()
            else (Path(root).resolve() / text).resolve()
        )
        if not (target == Path(root).resolve() or Path(root).resolve() in target.parents):
            return False
        return target.is_file()
    except (OSError, ValueError):  # pragma: no cover - 解析不了就按不存在
        return False


def drop_unknown_locations(
    findings: Sequence[SemanticFinding], allowed: set[str], root: Path | None = None
) -> CheckedFindings:
    """发现指到的位置这次改过、**或者工作区里真有这个文件** → 保留。

    两者都不是（路径写错 / 跳到工作区外 / 凭空编一个）→ 降级为 `unverified`。
    降级而不是删除：同一条信息照样进事件流与报告，只是标明「这个位置查不到」。
    """
    kept: list[SemanticFinding] = []
    unverified: list[SemanticFinding] = []
    counters: dict[str, int] = {}
    for item in findings:
        path = _clean_path(item.file, root).replace("\\", "/")
        if path in allowed or within_root(root, path) or path.startswith("（"):
            kept.append(item)
            continue
        counters[CONFLICT_UNKNOWN_FILE] = counters.get(CONFLICT_UNKNOWN_FILE, 0) + 1
        evidence = "、".join(sorted(allowed)) or "（这次没有改动）"
        unverified.append(
            replace(
                item,
                verified=False,
                conflicts=(
                    FactConflict(
                        claim=f"指向的位置查不到：{path or item.where}",
                        truth=(
                            f"记录：这次改动的路径是 {evidence}，而工作区里也没有 "
                            f"{path or '这个路径'}——这一条不据此拦完成声明"
                        ),
                    ),
                ),
            )
        )
    return CheckedFindings(tuple(kept), tuple(unverified), counters)


# ---- 主入口 ------------------------------------------------------------------


def review(
    materials: ReviewMaterials,
    *,
    root: Path | None = None,
    decision: TriggerDecision | None = None,
    chat: ChatLike | Callable[[str], Any] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_findings: int = DEFAULT_MAX_FINDINGS,
    schema: bool = True,
    tools: frozenset[str] = frozenset(),
) -> SemanticReport:
    """跑一次 L3 语义审查（或因触发条件不满足而跳过）。

    `chat` 是**注入面**：可以给一个 `ModelGateway`（有 `.chat(request)`），
    也可以给一个 `text -> text` 的普通函数。用例注入假 provider，判据不动
    ——真调模型的那条路只在产品里走。

    `decision` 不给时按 `materials` 自己判一遍触发条件（`decide_triggers`），
    调用方也可以先判、把理由带进来（主循环就是这么用的：它手上有发现条数、
    回归标志与账本缺口）。

    `tools` 是**运行台账里的工具名**（注册表），事实核对那条「说某把工具不存在」
    的判据要它。它**不进送审材料**：审查者不需要看到工具清单，事实核对需要。
    """
    decision = decision or decide_triggers(
        finding_count=len(materials.mechanical) + len(materials.behavior),
        files=len(materials.changes),
    )
    if not decision.run:
        return SemanticReport(ran=False, trigger=decision.note)
    if materials.blocked:
        # 没有产出就没有「做到了没有」可审：这一次不算跑过（读数如实说）。
        return SemanticReport(
            ran=False,
            trigger=decision.note,
            cannot_check="这一轮没有任何产出（待确认清单为空），没有可审的 diff",
        )
    if chat is None:
        return SemanticReport(
            ran=False,
            trigger=decision.note,
            cannot_check=f"{CANNOT_CHECK}没有接上模型通道，L3 没跑",
        )

    prompt = build_prompt(materials)
    try:
        text, prompt_tokens, completion_tokens = _ask(chat, prompt, max_tokens, schema)
    except Exception as exc:  # noqa: BLE001 - 审查坏了不该让运行跟着坏
        return SemanticReport(
            ran=True,
            trigger=decision.note,
            cannot_check=f"{CANNOT_CHECK}L3 这一次没能得出结果（{type(exc).__name__}: {exc}）",
        )
    parsed = parse_review(text, root=root, max_findings=max_findings)
    report = SemanticReport(
        ran=True,
        trigger=decision.note,
        verdict=parsed.verdict,
        reasons=parsed.reasons,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    if not parsed.ok:
        # 「没结论」与「判不合格」分开，两者都**不据此阻断**（理由见 parse_review）。
        return replace(report, cannot_check=parsed.why)
    facts = materials.facts(tools)
    checked = check_findings(parsed.findings, facts)
    located = drop_unknown_locations(
        checked.kept, known_paths(materials, root), root
    )
    kept = located.kept
    unverified = checked.unverified + located.unverified
    conflicts = dict(checked.conflicts)
    for key, value in located.conflicts.items():
        conflicts[key] = conflicts.get(key, 0) + value
    return replace(
        report,
        findings=kept + unverified,
        unverified=len(unverified),
        conflicts=conflicts,
    )


def _ask(
    chat: ChatLike | Callable[[str], Any],
    prompt: str,
    max_tokens: int,
    schema: bool,
) -> tuple[str, int, int]:
    """发一次审查请求，兼容两种注入形状（网关 / 纯函数）。"""
    if hasattr(chat, "chat"):
        from spoolkit.llm.types import ChatRequest, Message

        request = ChatRequest(
            messages=(Message(role="user", content=prompt),),
            max_tokens=max_tokens,
            response_schema=REVIEW_SCHEMA if schema else None,
        )
        response = chat.chat(request)  # type: ignore[union-attr]
        return (
            str(getattr(response, "text", "") or ""),
            int(getattr(response, "prompt_tokens", 0) or 0),
            int(getattr(response, "completion_tokens", 0) or 0),
        )
    text = chat(prompt)  # type: ignore[operator]
    if isinstance(text, tuple):  # 用例也可以给 (文本, 输入tok, 输出tok)
        head = str(text[0] or "")
        return (
            head,
            int(text[1]) if len(text) > 1 else 0,
            int(text[2]) if len(text) > 2 else 0,
        )
    return str(text or ""), 0, 0


# ---- 出口：账本发现 / 事件载荷 / 报告 ----------------------------------------


def as_findings(
    report: SemanticReport, *, note: str = ""
) -> list[Finding]:
    """把 L3 发现翻成账本发现（**过不了核对的那一份也在**，只是降级）。

    `kind` 一律是 `L3_FINDING_KIND`：`audit.note_kind` 据此把事件流的 kind
    给成同一个名字，探针按它数 `l3_findings`（不读散文）。

    `escalate` 只在**过了核对的高严重度**那一条上为真：它是升级挂点的形状
    （T9 / T12），与 L1/L2 完全一致。
    """
    out: list[Finding] = []
    for item in report.findings:
        detail = item.evidence
        if item.suggestion:
            detail = (detail + "；建议：" + item.suggestion).strip("；")
        if not item.verified:
            detail = (detail + "；" + item.conflict_note()).strip("；")
        out.append(
            Finding(
                kind=L3_FINDING_KIND,
                severity=item.audit_severity,
                summary=item.problem,
                detail=detail or (note or "L3 语义审查"),
                escalate=item.blocking,
                path=item.file if not item.file.startswith("（") else "",
                line=item.line,
            )
        )
    return out


def reading_payload(report: SemanticReport) -> dict[str, Any]:
    """过程读数那一条事件的载荷（**探针按这些键读**）。"""
    return {
        "text": report.render(),
        "ok": True,
        "kind": L3_READINGS_KIND,
        "ran": bool(report.ran),
        "trigger": report.trigger[:400],
        "verdict": report.verdict,
        "cannot_check": bool(report.cannot_check),
        "findings": len(report.findings),
        "by_severity": report.by_severity(),
        "unverified": report.unverified,
        "conflicts": dict(report.conflicts),
        "blocking": len(report.blocking),
        "prompt_tokens": report.prompt_tokens,
        "completion_tokens": report.completion_tokens,
    }


def blocked_payload(report: SemanticReport) -> dict[str, Any]:
    """「L3 拦下了完成声明」那一条事件的载荷（探针按它数 `l3_blocked`）。"""
    blocking = report.blocking
    return {
        "text": "L3 语义审查拦下了「完成声明」："
        + "；".join(f"{item.where} {item.problem}" for item in blocking[:5]),
        "ok": False,
        "kind": L3_BLOCKED_KIND,
        "trigger": "T9",
        "count": len(blocking),
        "where": [item.where for item in blocking],
    }


def render_report(report: SemanticReport) -> str:
    """收尾报告里那一段：**只放结论与逐条 `file:line`**（不贴 diff）。

    与 L2 的报告段同一个口径（问题③）：读数（触发条件原话、被剔除几条、
    「查不了」的理由）走事件流与 trace，报告里只留人真要读的东西。
    """
    blocking = report.blocking
    if not blocking:
        return ""
    lines = [
        "本次运行的 L3 语义审查（发现逐条带位置与依据）：",
    ]
    for item in blocking:
        tail = f"（依据：{item.evidence}）" if item.evidence else ""
        lines.append(f"- {item.where} {item.problem}{tail}")
    lines.append(
        "（拦的是「完成声明」与危险动作，**不是成果**：改动照落盘、可回退、"
        "报告逐条列出；删死代码、合并重复实现、删已被替代的文件都是正常编辑。）"
    )
    return "\n".join(lines)


def render_unverified(report: SemanticReport) -> str:
    """被事实核对降级的那几条：**说出来**，别静默丢（§4）。"""
    downgraded = [
        item for item in report.findings if not item.verified
    ]
    if not downgraded:
        return ""
    lines = ["L3 有发现与运行记录不符，已按记录降级（不据此拦完成声明）："]
    for item in downgraded:
        lines.append(f"- {item.where} {item.problem}——{item.conflict_note()}")
    return "\n".join(lines)


def finding_paths(report: SemanticReport) -> tuple[str, ...]:
    """这份报告里被点到的路径（去重，供上层做「指到哪儿了」的读数）。"""
    seen: list[str] = []
    for item in report.findings:
        pure = PurePosixPath(item.file.replace("\\", "/"))
        text = pure.as_posix()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def merge_reports(
    first: SemanticReport | None, second: SemanticReport
) -> SemanticReport:
    """两次审查的合并（自动修之后重算一轮时用：同一处不重复打扰）。

    只保留**位置 + 问题**相同的那一条（第一次那份），新的追加在后面。
    """
    if first is None or not first.findings:
        return second
    seen = {(item.file, item.line, item.problem) for item in first.findings}
    extra = [item for item in second.findings if (item.file, item.line, item.problem) not in seen]
    if not extra:
        return first
    return replace(first, findings=first.findings + tuple(extra))


def tool_names(text: str, facts: ReviewFacts) -> list[str]:
    """这句话点了哪些**注册表里真有**的工具名（核对「工具不存在」那条用）。

    直接复用 supervisor 判据里那句「从文字里找出它点了哪个名字」的写法，
    但做成公开函数：事件与报告要把「它说的是哪把工具」写出来。
    """
    probe = str(text or "").lower()
    return sorted(name for name in facts.tools if name.lower() in probe)


__all__ = [
    "AUDIT_SEVERITIES",
    "CANNOT_CHECK",
    "ChangeFacts",
    "CheckedFindings",
    "L3_BLOCKED_KIND",
    "L3_DETAIL_KIND",
    "L3_FINDING_KIND",
    "L3_READINGS_KIND",
    "ParsedReview",
    "REVIEW_SCHEMA",
    "ReviewFacts",
    "ReviewMaterials",
    "SEVERITY_HIGH",
    "SEVERITY_LOW",
    "SEVERITY_MEDIUM",
    "SEVERITY_TO_AUDIT",
    "SemanticFinding",
    "SemanticReport",
    "TRIGGER_IDS",
    "TriggerDecision",
    "as_findings",
    "blocked_payload",
    "build_prompt",
    "check_findings",
    "decide_triggers",
    "drop_unknown_locations",
    "finding_paths",
    "known_paths",
    "merge_reports",
    "parse_review",
    "reading_payload",
    "render_diff",
    "render_report",
    "render_unverified",
    "review",
    "review_conflicts",
    "tool_names",
]
