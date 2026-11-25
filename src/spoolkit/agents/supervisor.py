"""督导会话：预算用尽或它在原地打转时，由它决定接着走、改道还是收手。

原先的做法是撞上步数上限就停，然后提示用户 `--resume` 再跑一次。那等于把
系统的判断成本推给用户，而且把一件事拆成两次对话——第二次还得从头理解一遍。
这里换成一个**独立上下文的会话**来判断：它看不到主循环的推理过程，只看证据
（目标、任务状态、最近发生了什么、机械统计），然后给一份结构化结论。

**为什么是一次调用，不是一个子智能体**：它不需要翻文件，需要的是判断。
一个紧凑的证据包加一份结构化结论，代价约等于主循环的一步，换回来的是一整轮
预算。让它自己去翻文件，只会把「判断」变成「又一次探索」——那正是它要判断
的那个问题。

**为什么机械信号还得留着**：督导是一次模型调用，会失败、也会看错。重复调用、
空回合、原地漫游这些**可检验**的信号仍然在循环里先挡一道；督导只在「机械层
已经说了有事、模型自己没纠正」的时候被叫起来——以及预算用尽时被叫一次。

两条安全线：

- 续期是有限的（总步数上限），否则一个卡住的任务能把 GPU 烧一整夜；
- 督导自己出问题（调用失败、结论解析不出来）一律退回原行为——它坏了不该让
  任务跟着坏，但这件事要说出来。
"""

import json
from dataclasses import dataclass, replace
from typing import Any

from spoolkit import limits as _limits
from spoolkit.context import templates as T
from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.types import ChatRequest, Message

EXTEND = "extend"
REDIRECT = "redirect"
STOP = "stop"
# 「它已经做完了，只是自己没说」。
#
# 这条是实测逼出来的：审查者完成了核对、自动验证也通过了，但它在收尾前
# 用完了回合，于是循环记下的是「任务没做完，收手的原因：任务已完成」——
# 一句自相矛盾的话，而后果是这次审查被当成了「没得出结论」。
# 督导看得见证据，就该有办法说「它做完了」。
FINISH = "finish"

# 一次最多给多少步。给太多等于把上限取消掉：真正的用处是「够走完剩下的事」，
# 而它下一轮还会被问一次，不必一次给足。
#
# 默认值来自登记表（limits.KNOBS 的 supervisor_max_grant），这里留名字给外部
# 导入；实际用多少由调用方按本次运行的覆盖给（强模型可以一次多给些）。
MAX_GRANT = int(_limits.require_knob("supervisor_max_grant").default)

# 「它说这是正常探索、却要收手」时改成续期，一次给多少步。
#
# 取 3 而不是上限：这条的目的是让它**落到一处改动上**，不是给出一轮新预算。
# 给多了等于把「判断」换成「再跑一会儿」，而它下一轮还会被问一次。
_EXPLORE_GRANT = int(_limits.require_knob("explore_grant_steps").default)

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [EXTEND, REDIRECT, FINISH, STOP]},
        "steps": {"type": "integer"},
        "reason": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["action", "reason"],
}


@dataclass(frozen=True)
class Evidence:
    """给督导看的证据包。

    刻意不含主循环的对话历史与推理过程：它要判断的是「这些动作有没有进展」，
    读到推理过程只会让它附和那个推理——和审查者必须是独立上下文同一个理由。
    """

    goal: str
    step: int
    limit: int
    ceiling: int
    state: str = ""
    trace: tuple[str, ...] = ()
    edits: int = 0
    calls: int = 0
    resets: int = 0
    repeats: int = 0
    no_edit: int = 0
    tokens: int = 0
    asking: str = "预算用完了"
    cycle: str = ""
    oscillations: int = 0
    same_query: int = 0
    # 它对上一次督导建议的表态（接受 / 不接受 + 理由），以及我们转给它的
    # 那次建议。**这两样必须给督导看**：它是唯一能判断「建议是不是被当成
    # 命令」的地方——看不到表态，它只能看到「我又建议了一次，它没照做」，
    # 下一个结论就会从「建议」滑向「命令」（加长、加重、最后收手）。
    assessment: str = ""
    suggestion: str = ""
    # 它这一次写的理由。兜底判据只看它——**不看 trace**。
    #
    # 曾经让判据在 trace 里也找关键词，那是错的：trace 里是工具输出与报错，
    # 那些字不是它的判断（`survey` 的失败话术里就有「意图」「先读后计划」
    # 这类字眼），拿它们去凑关键词会让兜底在没有依据时触发。
    reason: str = ""
    # 这次运行真正注册的工具名。vague-optimize 真机现场：督导照 SUPERVISE
    # 模板里的示例建议「按批交给 dispatch」，而那个运行根本没注册 dispatch——
    # 模型拿到一个不存在工具的建议，原地空转。清单必须来自注册表，
    # 不能是模板里的静态举例。
    tools: tuple[str, ...] = ()
    # 只给它看最近这些行。再往前翻，它看到的是同一些动作的重复。
    trace_tail: int = 40

    @property
    def room(self) -> int:
        """总上限里还剩多少步。"""
        return max(0, self.ceiling - self.step)

    def render(self) -> dict[str, Any]:
        tail = self.trace[-self.trace_tail :]
        return {
            "goal": self.goal,
            "asking": self._asking_with_history(),
            "step": self.step,
            "limit": self.limit,
            "ceiling": self.ceiling,
            "room": self.room,
            "state": self.state or "（它还没记录任何状态）",
            "trace": "\n".join(tail) if tail else "（还没有发生任何事）",
            "edits": self.edits,
            "calls": self.calls,
            "resets": self.resets,
            "repeats": self.repeats,
            "no_edit": self.no_edit,
            "tokens": self.tokens,
            "cycle": self.cycle,
            "oscillations": self.oscillations,
            "same_query": self.same_query,
            "tools": "、".join(self.tools) if self.tools else "（未提供）",
        }

    def _asking_with_history(self) -> str:
        """把「上次建议 + 它的表态」接到当前情况后面。

        折进 `asking` 而不是新开一个占位符，有两个理由：占位符集合是**冻结的**
        （tests/test_prompt_mechanics.py 逐字钉着），而这段文字与「当前情况」
        本来就是同一件事——上次建议了什么、它怎么答的、所以现在为什么又叫你。
        """
        if not self.suggestion and not self.assessment:
            return self.asking
        answered = (
            f"它上一次的表态：{self.assessment}"
            if self.assessment
            else "它上一次没有表态（它还没走到回答那一步）"
        )
        parts = [self.asking]
        if self.suggestion:
            parts.append(f"你上一次给它的建议：{self.suggestion}")
        parts.append(answered)
        return "；".join(parts)


@dataclass(frozen=True)
class Verdict:
    """督导的结论。"""

    action: str
    reason: str
    steps: int = 0
    message: str = ""
    # 问一次督导也是真花钱的。不记进来的话，用量报表会少算一笔，
    # 而调这块时看的正是那张表。
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def grants(self) -> int:
        """这次能给多少步（0 表示不续期）。"""
        if self.action in (EXTEND, REDIRECT):
            return self.steps
        return 0


# ---- 事实性断言必须能被机械核对 -------------------------------------------
#
# 用户点名的第 4 条发现：督导会说出与记录相反的话——「尝试使用不存在的
# confirm_rewrite 工具」（那个工具就在注册表里）、「自动验证通过（0 处改动）」
# （记录里明明有改动）。前一句直接把它引到错方向，后一句把一次真实的落盘
# 说成没发生。
#
# 这类句子有一个共同点：**它们是可以被记录的**。工具在不在注册表里、
# 有没有改动落成、哪些文件被改过——主循环手上有全部原料。所以判据不是
# 「让督导说话小心点」（那推不动），而是注入之前逐句核对：
#   与记录冲突的断言 → 丢弃；该结论仍然有用（换个做法/收手）→ 保留行动，
#   但换成**记录里的事实**重述一遍，并标明「与记录不符」。
#
# 为什么不是直接丢掉整条结论：一条错话夹在一条正确建议里时，丢掉整条比
# 留一条被改写过的更亏——它下一轮还会给出同样的建议，而这一轮什么都没发生。
# 标出来至少让下一步不建立在假事实上。

_PLAIN = "与记录不符（已按运行记录改写）"

# 断言「某把工具不存在 / 没注册 / 用不了」。核对方式：注册表。
_TOOL_MISSING = (
    "不存在的工具",
    "工具不存在",
    "没有这个工具",
    "不存在的 confirm",
    "不存在的工具名",
    "未注册的工具",
    "工具未注册",
    "没有注册",
)
# 断言「一处改动都没有 / 文件没被改」。核对方式：本次运行的实际改动记录。
_NO_EDIT_CLAIMS = (
    "0 处改动",
    "零处改动",
    "0处改动",
    "没有任何改动",
    "没有实际修改",
    "文件零改动",
    "未做任何修改",
    "没有做任何修改",
    "未产生任何改动",
    # 中文里更常见的说法。漏了它，假事实照样端出去。
    "一处改动都没有",
    "一处改动也没有",
    "没有落成任何改动",
)


@dataclass(frozen=True)
class RunFacts:
    """核对督导断言用的**运行记录**。

    每一条都是机械可查的：工具名来自注册表，改动与文件来自循环自己的账
    （不是模型的叙述，也不是 trace 里的字）。
    """

    #: 这次运行真正注册过的工具名。
    tools: frozenset[str] = frozenset()
    #: 真正落成（进了待确认清单）的改动次数。
    edits: int = 0
    #: 被改动过的路径。
    changed: frozenset[str] = frozenset()


def _claims_tool_missing(text: str) -> bool:
    # 一律按小写匹配：工具名在句子里的大小写不固定（`confirm_rewrite` 与
    # `Confirm_Rewrite` 是同一把工具），而漏判等于让假事实端给模型。
    probe = text.lower()
    return any(one in probe for one in _TOOL_MISSING)


def _claims_no_edit(text: str) -> bool:
    return any(one in text for one in _NO_EDIT_CLAIMS)


@dataclass(frozen=True)
class FactConflict:
    """一条与记录冲突的断言，以及据记录该怎么说。"""

    claim: str
    truth: str


def fact_conflicts(text: str, facts: RunFacts) -> list[FactConflict]:
    """这段文字里有没有与运行记录冲突的事实性断言。

    返回**入参不是结论**：判据只看记录。宁可漏判（那句话照旧送出去），
    也不要误判——误判会把一条对的建议改写成一句莫名其妙的话。
    所以只认「明确否定且记录与之相反」这两种形状。

    **自动审查层 L3 直接复用这一条**（`agent/review_semantic.py` 的
    `ReviewFacts.as_run_facts()` 把共有的三项交回来），并在它之上加了第三种
    形状（「某一处声称的改动在 diff 里找不到」）。判据本身在这里只有一份：
    它正是被督导幻觉坑出来的那两条，复制一遍就等于埋第二个坑。
    """
    if not text:
        return []
    found: list[FactConflict] = []
    if _claims_tool_missing(text):
        # 记录里说不存在的那把工具：先从文字里找出它点了哪个名字。
        probe = text.lower()
        named = [name for name in facts.tools if name.lower() in probe]
        if named:
            found.append(
                FactConflict(
                    claim=f"声称工具不存在：{'、'.join(sorted(named))}",
                    truth=(
                        f"记录：{'、'.join(sorted(named))} 在本次运行的工具里，"
                        "调用失败的原因不是「工具不存在」"
                    ),
                )
            )
    if facts.edits > 0 and _claims_no_edit(text):
        where = "、".join(sorted(facts.changed)) or "（未记到路径）"
        found.append(
            FactConflict(
                claim="声称这次运行 0 处改动",
                truth=(
                    f"记录：本次运行有 {facts.edits} 处改动落成（{where}），"
                    "自动验证不是「0 处改动」"
                ),
            )
        )
    return found


def _rewrite(text: str, conflicts: list[FactConflict]) -> str:
    """把与记录冲突的断言改写成「记录里的说法」。"""
    body = text.rstrip()
    lines = [f"{body}"] if body else []
    for one in conflicts:
        lines.append(f"[{_PLAIN}] {one.truth}。")
    return "\n".join(lines)


def sanitize_verdict(
    verdict: Verdict, facts: RunFacts | None
) -> tuple[Verdict, tuple[FactConflict, ...]]:
    """注入之前核对一遍督导的结论。

    核对的字段是**要给人看、要发给模型**的那两个：`reason`（原样显示给
    用户）与 `message`（原样发给模型）。`action` 与 `steps` 不动——它们是
    判断，不是事实断言；核对不该顺手改掉一次续期或收手。

    `facts` 为 None（调用方拿不到记录）时**照旧返回**：核对是加强，不是
    新的必经环节；拿不到记录就不能假装核对过了。
    """
    if facts is None:
        return verdict, ()
    conflicts: list[FactConflict] = []
    reason = verdict.reason
    message = verdict.message
    for field in ("reason", "message"):
        text = reason if field == "reason" else message
        found = fact_conflicts(text, facts)
        if not found:
            continue
        conflicts.extend(found)
        if field == "reason":
            reason = _rewrite(reason, found)
        else:
            message = _rewrite(message, found)
    if not conflicts:
        return verdict, ()
    return replace(verdict, reason=reason, message=message), tuple(conflicts)


def parse_verdict(
    text: str,
    evidence: Evidence,
    max_grant: int = MAX_GRANT,
) -> Verdict | None:
    """把督导的一句话判成结论。解析不出来返回 None——调用方按「不敢续期」处理。

    含糊的续期比不续期危险：不续期只是停下来，而错误续期会把预算继续投进
    一个已经卡住的任务。所以这里的默认值是「不续」。

    兜底判据要看的「它自己写的理由」通过 `Evidence.reason` 传进来
    （`supervise` 解析出 response 之后回填），不另开一个参数：
    同一份事实有两个来源时，早晚会有一处忘了传。
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    action = str(payload.get("action", "")).strip().lower()
    if action not in (EXTEND, REDIRECT, FINISH, STOP):
        return None
    reason = str(payload.get("reason") or "").strip()
    message = str(payload.get("message") or "").strip()
    if action == FINISH:
        return Verdict(FINISH, reason or "证据显示任务已经完成", 0, message)

    raw = payload.get("steps")
    steps = int(raw) if isinstance(raw, (int, float)) else 0
    # 它想给多少不算数：上限由我们夹住。越权一次就等于上限不存在。
    steps = max(0, min(steps, max_grant, evidence.room))
    if action == STOP and _stopping_before_exploration(evidence):
        # 它自己刚说「这是正常探索」——那就不能收手。
        #
        # 实测（本机 27B，复跑用户的「优化一下旧文件的代码」）：预算 3 步时
        # 它给出的 reason 是「任务处于正常探索阶段，尚未提出改动……总步数预算
        # 充足（剩余 21 步）」，然后 action=stop。**自己说的和做的是两件事**——
        # 而 stop 的后果是把一次尚未开始的探索判死（用户正是要修这个）。
        # 这里改成续期：判断（这是探索）保留它的，动作（别收手）由我们兜住。
        # 续多少步：它自己报了一个正数就用它（已经被夹在单次上限与总余量里），
        # 没报就给一个小的默认值——够它落到一处改动上，不必一次给满。
        grant = steps if steps > 0 else min(_EXPLORE_GRANT, evidence.room)
        return Verdict(
            EXTEND,
            reason or "它处于正常探索阶段，先给几步让它落到改动上",
            grant,
            message,
        )
    if action in (EXTEND, REDIRECT) and steps <= 0:
        # **「没报步数」不等于「不续期」**，这一条是复跑里踩出来的：
        #
        # 那次它写的是 action=extend、reason 说「剩余 21 步充足」，
        # 但 payload 里**没有 steps 字段**（约束解码的 schema 里 steps 不是必填）。
        # 旧行为把它读成「不续期」→ stop → 一次尚未开始的探索被判死，
        # 而它自己明明说了要接着走。后果与「边探索边收手」一模一样。
        #
        # 所以：它**报了一个数**且数 <= 0 → 按它的意思 stop（那是明确的）；
        # 它**没报** → 按默认额度续期。安全线不动：总上限里没有余量时照旧停。
        asked = payload.get("steps")
        if isinstance(asked, (int, float)) and not isinstance(asked, bool):
            return Verdict(STOP, reason or "它给的步数是 0，不再续", 0, "")
        if evidence.room <= 0:
            return Verdict(STOP, reason or "总步数上限已经用完了，不能再续", 0, "")
        return Verdict(
            action,
            reason or "它要接着走，但没报步数：按默认额度续",
            min(_EXPLORE_GRANT, evidence.room),
            message,
        )
    return Verdict(action, reason, steps, message)


def _stopping_before_exploration(evidence: Evidence) -> bool:
    """它是不是一边说「这是正常探索」，一边要收手。

    判据只用**它自己写下来的话**（`Evidence.reason`，即它在结论里写的理由）
    与**我们问它的那一句**（`asking`，里面有「像是先读后计划」这样的措辞）。
    **不看 trace**——trace 里是工具输出与报错，那些字不是它的判断，
    拿它们凑关键词会让兜底凭空触发。

    中文措辞是脆弱的东西，所以这条只用于把 **stop 换成 extend**——方向是
    「多给几步」，判错一次代价是多花几步；反过来若拿它去 stop，判错一次
    就是把一件能做的任务判死。这正是用户第二条质疑要防的那件事。
    """
    if evidence.room <= 0:
        return False
    text = evidence.reason + " " + evidence.asking
    return any(
        word in text
        for word in ("正常探索", "先读后计划", "尚未提出改动", "尚未提出任何改动")
    )


def _reason_of(text: str) -> str:
    """从它那段 JSON 里取出 reason，取不到就给空串。

    单独抽出来是为了让「一边说这是正常探索、一边收手」那条判据能看到它
    **自己写的原话**，而不是我们替它总结过的字段。取不到不算错：
    解析失败那条路本来就会返回 None。
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("reason") or "")


def supervise(
    gateway: ModelGateway,
    evidence: Evidence,
    max_tokens: int = 512,
    max_grant: int = MAX_GRANT,
    facts: RunFacts | None = None,
) -> tuple[Verdict | None, tuple[FactConflict, ...]]:
    """问一次督导。

    调用失败或结论不合格式都返回 `(None, ())`。**不抛异常**：督导是增强，
    它坏了不该让整个任务跟着坏。

    第二项是与运行记录冲突的断言（可能被改写）。调用方要把它记下来——
    「督导说错话被拦下了」这件事本身是这块唯一能被看见的信号。
    """
    try:
        response = gateway.chat(
            ChatRequest(
                messages=(
                    Message(
                        role="user",
                        content=T.SUPERVISE.format(**evidence.render()),
                    ),
                ),
                max_tokens=max_tokens,
                response_schema=VERDICT_SCHEMA,
            )
        )
    except Exception:
        return None, ()
    # 它这一次写的理由要回填进证据：兜底的判据只看它（外加我们给它的提问），
    # 不看 trace——trace 里是工具输出与报错，那些字不是它的判断。
    reason = _reason_of(response.text)
    verdict = parse_verdict(
        response.text, replace(evidence, reason=reason), max_grant
    )
    if verdict is None:
        return None, ()
    # 注入之前机械核对一遍：与运行记录冲突的事实性断言在这一步被改写。
    verdict, conflicts = sanitize_verdict(verdict, facts)
    return (
        replace(
            verdict,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        ),
        conflicts,
    )
