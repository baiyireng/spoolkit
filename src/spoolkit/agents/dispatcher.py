"""分派器：决定这次改动由主循环自己做，还是交给子智能体。

判断不由启发式规则做，而是让模型产出一份**结构化任务说明**，再由闸门决定
能不能派发。理由是启发式只能看改动规模，看不出「这次改动是否值得独立上下文」；
而结构化说明顺带解决了另一件事——它就是子智能体唯一的信息来源。

闸门是关键：没有验收标准就不派发。没有可执行的判断依据，实现者做到什么
程度都算完成，审查者也无从判断，整条流水线会退化成两个模型互相附和。
"""

import json
from dataclasses import dataclass, field
from typing import Any

from spoolkit.agents.runtime import (
    IMPLEMENTER,
    REVIEWER,
    TOO_BIG_MARKER,
    TaskSpec,
    run_role,
)
from spoolkit.config import Config
from spoolkit.context import templates as T
from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.types import ChatRequest, Message
from spoolkit.tools.registry import ToolRegistry

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "delegate": {"type": "boolean"},
        "reason": {"type": "string"},
        "goal": {"type": "string"},
        "targets": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "acceptance": {"type": "string"},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["delegate", "reason", "goal"],
}

PLAN_PROMPT = T.DISPATCH

# 审查结论必须结构化。审查者的 final 是自由文本，直接拿来判「过没过」
# 只能靠关键词猜；而「过得含糊」和「没过」是两种完全不同的处置。
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "fix_goal": {"type": "string"},
    },
    "required": ["verdict", "reasons"],
}


@dataclass(frozen=True)
class Review:
    """一次审查的判定结果。"""

    passed: bool
    reasons: tuple[str, ...] = ()
    fix_goal: str = ""
    # 审查没有得出结论（比如审查者自己撞了步数上限）。这和「审查后判定不合格」
    # 是两件完全不同的事：前者说明这次审查根本没发生，拿它去触发修复重派，
    # 会把一处正确的实现判成失败——实测第一次跑就撞上了。
    inconclusive: bool = False


def judge_review(
    gateway: ModelGateway, acceptance: str, reviewer_final: str, max_tokens: int = 512
) -> Review:
    """把审查者的自由文本结论判成 pass / fail。

    解析不出来时按**未通过**处理：含糊的通过等于没有审查，而它带来的
    代价是把一处未验证的改动当成已验证的交付出去。
    """
    response = gateway.chat(
        ChatRequest(
            messages=(
                Message(
                    role="user",
                    content=T.JUDGE_REVIEW.format(
                        acceptance=acceptance or "（没写验收标准）",
                        review=reviewer_final or "（审查者没有给出结论）",
                    ),
                ),
            ),
            max_tokens=max_tokens,
            response_schema=REVIEW_SCHEMA,
        )
    )
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError:
        return Review(False, ("审查结论无法解析，按未通过处理",))
    if not isinstance(payload, dict):
        return Review(False, ("审查结论格式不对，按未通过处理",))
    verdict = str(payload.get("verdict", "")).strip()
    reasons = tuple(str(item) for item in payload.get("reasons") or () if item)
    fix_goal = str(payload.get("fix_goal") or "").strip()
    if verdict == "pass" and reasons:
        return Review(True, reasons)
    return Review(False, reasons or ("审查没有给出通过的依据",), fix_goal)


@dataclass(frozen=True)
class DispatchPlan:
    """一次分派决策。"""

    delegate: bool
    reason: str
    spec: TaskSpec | None = None
    blocked: str = ""


def _parse_plan(text: str, goal: str) -> DispatchPlan:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return DispatchPlan(False, "无法解析分派计划，改为自己完成")
    if not isinstance(payload, dict):
        return DispatchPlan(False, "分派计划格式不对，改为自己完成")

    reason = str(payload.get("reason", "")).strip()
    if not payload.get("delegate"):
        return DispatchPlan(False, reason or "不需要派发")

    spec = TaskSpec(
        goal=str(payload.get("goal") or goal).strip(),
        targets=tuple(payload.get("targets") or ()),
        constraints=tuple(payload.get("constraints") or ()),
        acceptance=str(payload.get("acceptance") or "").strip(),
        out_of_scope=tuple(payload.get("out_of_scope") or ()),
    )
    problem = spec.validate()
    if problem is not None:
        # 闸门拦下就退回自己完成，而不是带着缺验收标准的说明去派发。
        return DispatchPlan(False, f"分派被拦下：{problem}", blocked=problem)
    return DispatchPlan(True, reason, spec=spec)


def _flatten(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit] + "…" if len(flat) > limit else flat


def _evidence(artifact: str, verify) -> str:
    """交给审查者的证据：改动差异 + 自动验证的结果。

    实测不给测试结果时，审查者会自己一路翻文件去还原「刚才发生了什么」，
    把整轮预算耗光，最后连结论都给不出来。它要的是证据，不是自由度。
    """
    if verify is None:
        return artifact
    try:
        result = verify()
    except Exception as exc:  # 验证出问题不该让审查整个跑不起来
        return f"{artifact}\n\n自动验证没能跑起来（{type(exc).__name__}）：{exc}"
    status = "通过" if result.ok else "未通过"
    return f"{artifact}\n\n自动验证结果：{status}\n{result.content[:600]}"


def plan_dispatch(
    gateway: ModelGateway,
    goal: str,
    context: str = "",
    max_tokens: int = 1024,
) -> DispatchPlan:
    """让模型产出分派计划，并经闸门校验。"""
    response = gateway.chat(
        ChatRequest(
            messages=(
                Message(
                    role="user",
                    content=PLAN_PROMPT.format(goal=goal, context=context or "无"),
                ),
            ),
            max_tokens=max_tokens,
            response_schema=PLAN_SCHEMA,
        )
    )
    return _parse_plan(response.text, goal)


@dataclass
class DelegatedResult:
    """一次分派执行的完整结果。"""

    plan: DispatchPlan
    implementer_final: str = ""
    reviewer_final: str = ""
    # 两个角色各自花了多少步、多少次调用。**必须交出来**：审查没给出结论时，
    # 「它把预算花在验证动作上了」和「它判断不出来」是两件完全不同的事，
    # 而只看一句结论分不清——实测三次批量派发全是「没结论」，原因一直不明。
    implementer_steps: int = 0
    reviewer_steps: int = 0
    review: Review | None = None
    rounds: int = 0
    trace: list[str] = field(default_factory=list)
    # 子智能体明确说了「这件事我独立做不完」时的说明与建议拆法。
    # 与「审查未通过」是两件事：前者是**任务太大**，后者是**改动不对**。
    too_big: str = ""
    # 这一趟花掉的模型开销。要报回主循环——否则用量表会漏掉子智能体整段，
    # 而主循环看到的「2 次调用」会让人以为派发是免费的。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0

    @property
    def usage(self):
        from spoolkit.tools.types import Usage

        return Usage(self.prompt_tokens, self.completion_tokens, self.model_calls)

    @property
    def rejected(self) -> bool:
        """审查最终判了不通过。

        注意不含「审查没得出结论」——那种情况改动既没被否定，也没被确认，
        该交给用户判断，而不是拿去当失败处理。
        """
        return (
            self.review is not None
            and not self.review.passed
            and not self.review.inconclusive
        )


def plan_repair(
    gateway: ModelGateway,
    spec: TaskSpec,
    review: Review,
    max_tokens: int = 1024,
) -> DispatchPlan:
    """把审查结论打回给主循环，由它决定要不要再派一次修复。

    这个判断刻意交回给模型，不用「凡不通过就重试」的规则：有些问题
    重试多少次都是同一个结果，而模型能看到审查具体说了什么。
    闸门照旧——再派出去的任务说明仍然必须有验收标准。
    """
    response = gateway.chat(
        ChatRequest(
            messages=(
                Message(
                    role="user",
                    content=T.REPAIR.format(
                        goal=spec.goal,
                        acceptance=spec.acceptance,
                        reasons="；".join(review.reasons) or "（没给理由）",
                    ),
                ),
            ),
            max_tokens=max_tokens,
            response_schema=PLAN_SCHEMA,
        )
    )
    return _parse_plan(response.text, spec.goal)


def run_delegated(
    plan: DispatchPlan,
    gateway: ModelGateway,
    tokenizer,
    registry: ToolRegistry,
    config: Config,
    artifacts: str = "",
    verify=None,
    max_rounds: int | None = None,
) -> DelegatedResult:
    """先让实现者做，再让审查者在独立上下文里验。

    写者与验者用同一个模型的两个独立上下文——不是不同模型。
    审查者拿到的是需求、约束、验收标准和改动差异，**不含实现者的推理过程**，
    否则它只是在附和那个推理。

    **审查不通过就打回给主循环**：由它决定要不要再派一次修复任务。
    循环有上限（config.review_rounds）——「不通过就重试」本身会变成
    一个自动的无限循环，而有些问题重试多少次都是同一个结果。
    """
    if not plan.delegate or plan.spec is None:
        raise ValueError("这份计划不包含可派发的任务说明")

    # 把网关包一层来记账：实现、审查、判定、打回**四处**的调用都要算进来。
    # 逐个函数去传累加器容易漏（判定和打回就是各一次 chat，很容易忘），
    # 包一层则一处不漏。
    gateway = _Counting(gateway)
    result = DelegatedResult(plan=plan)
    limit = config.review_rounds if max_rounds is None else max_rounds
    spec = plan.spec

    for index in range(max(1, limit)):
        round_no = index + 1
        implemented = run_role(
            IMPLEMENTER, spec, gateway, tokenizer, registry, config, verify=verify
        )
        result.implementer_final = implemented.final
        result.implementer_steps = implemented.steps
        result.trace.extend(f"[实现 {round_no}] {line}" for line in implemented.trace)

        # 两条「别送审」的路，都是**任务规模**问题，不是改动质量问题：
        #   1. 它自己说了做不完（【太大】）；
        #   2. 它撞了步数上限（没 finished）——那是在审一个半成品。
        # 送审会把「任务太大」误报成「实现不合格」，然后触发一轮注定失败的
        # 修复重派。真正该做的是把话带回主循环：拆小再派。
        if not implemented.finished or TOO_BIG_MARKER in implemented.final:
            reason = (
                implemented.final.strip()
                if TOO_BIG_MARKER in implemented.final
                else (
                    f"做不完：它用完了 {implemented.steps} 步预算仍然没给出结论"
                    f"（预算 {config.subagent_steps} 步/轮）。"
                )
            )
            result.too_big = reason
            result.rounds = round_no
            result.trace.append(f"[规模] 不送审——{_flatten(reason, 300)}")
            break

        review_spec = TaskSpec(
            goal=f"独立审查这次改动：{spec.goal}",
            targets=spec.targets,
            constraints=spec.constraints,
            acceptance=spec.acceptance,
            out_of_scope=spec.out_of_scope,
            artifact=_evidence(artifacts or implemented.final, verify),
        )
        reviewed = run_role(
            REVIEWER, review_spec, gateway, tokenizer, registry, config, verify=verify
        )
        result.reviewer_final = reviewed.final
        result.reviewer_steps = reviewed.steps
        result.trace.extend(f"[审查 {round_no}] {line}" for line in reviewed.trace)

        if reviewed.finished:
            review = judge_review(gateway, spec.acceptance, reviewed.final)
        else:
            # 审查者自己没跑完：把它的 final 当成「审查判定不合格」会把一处
            # 正确的实现判死——实测第一次跑就是这样，三个文件都改对了却被打回。
            #
            # 原因那半句用**它自己的话**，不要替它猜：没跑完可能是撞步数上限、
            # 也可能是督导让它收手、或者卡在空回合里。实测记下来的就是一句
            # 错归因——「审查者自己撞上了步数上限（8 步）」，而它的上限是 20 步，
            # 真实原因是督导判断该收手了。
            review = Review(
                False,
                # reasons 是**元组**，传字符串会被 "；".join 按字符拆开
                # （实测日志里就出现了「审；查；没；有；得；出；结；论」）。
                (
                    "审查没有得出结论（它自己没跑完，不是判定改动不合格）："
                    + _flatten(reviewed.final, 160),
                ),
                inconclusive=True,
            )
        result.review = review
        result.rounds = round_no
        result.trace.append(
            f"[判定 {round_no}] "
            + ("通过" if review.passed else ("审查无结论" if review.inconclusive else "未通过"))
            + (f"：{'；'.join(review.reasons)}" if review.reasons else "")
        )
        # 审查没结论时不该触发修复重派：什么都没查出来，
        # 重派一次只是把同样的活再干一遍。
        if review.passed or review.inconclusive or round_no >= limit:
            break

        # 打回给主循环：由它决定还要不要修。决定不修就停在这里，
        # 把没通过的改动和理由一并交给用户。
        repair = plan_repair(gateway, spec, review)
        result.trace.append(
            f"[打回] {'再派一次修复' if repair.delegate else '不再修'} —— {repair.reason}"
        )
        if not repair.delegate or repair.spec is None:
            break
        spec = repair.spec

    result.prompt_tokens = gateway.prompt_tokens
    result.completion_tokens = gateway.completion_tokens
    result.model_calls = gateway.calls
    # 记一笔派发战绩。放在这里而不是放在 dispatch 工具里：派发有**两条路**
    # （会话内的 dispatch 工具、计划里 executor=subagent 的步骤），
    # 记在其中一条上，另一条就永远是空的——实测就是这么漏掉的。
    from spoolkit.tools.dispatch import record_dispatch

    record_dispatch(
        config.project_root,
        kind=(
            "太大"
            if result.too_big
            else (
                "没结论"
                if (result.review and result.review.inconclusive)
                else ("通过" if (result.review and result.review.passed) else "未通过")
            )
        ),
        calls=result.model_calls,
        rounds=result.rounds,
        targets=len(spec.targets),
        note=result.too_big or (
            "；".join(result.review.reasons) if result.review else ""
        ),
        implementer_steps=result.implementer_steps,
        reviewer_steps=result.reviewer_steps,
    )
    return result


class _Counting:
    """把网关包一层，数清这一趟花了多少。

    只转 chat：其余属性（context_window / token_counter）原样透传，
    因为子角色也需要它们。
    """

    def __init__(self, inner: ModelGateway) -> None:
        self._inner = inner
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    def chat(self, request):
        response = self._inner.chat(request)
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        self.calls += 1
        return response

    def __getattr__(self, name):
        return getattr(self._inner, name)
