"""把一块活交给子智能体去做——**会话内的派发入口**。

主循环的上下文是最贵的东西：它背着整个任务的进度，一旦溢出就要重置。而
「读十几处代码才能改对的一处改动」这类活，塞进主循环的上下文里，代价是
主循环被细节挤满——它恰恰又有明确的验收标准，正适合放进一次性容器。

这和 `--delegate` 不是一回事：那是**启动前判断一次**「整件事要不要派人做」。
真实任务里该不该派、派哪一块，是做到一半才看得清的——所以入口得在会话里。

闸门照旧（`TaskSpec.validate`）：没有验收标准不允许派发。没有可执行的判断
依据，实现者做到什么程度都算完成，审查者也无从判断，整条流水线会退化成
两个模型互相附和。

**子智能体拿不到这个工具**（角色工具表里没有它），所以不存在自我派发的递归。
"""

from dataclasses import dataclass

from agents_dev.agents.dispatcher import DispatchPlan, run_delegated
from agents_dev.agents.runtime import TaskSpec
from agents_dev.config import Config
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.types import ToolResult, ToolSpec


def dispatch_spec(
    gateway: ModelGateway,
    registry: ToolRegistry,
    config: Config,
    tokenizer: TokenCounter,
    verify=None,
) -> ToolSpec:
    """构造派发工具。它绑定的是**主循环那一套**注册表。

    这一点很关键：子智能体写文件时用的是同一份待确认改动（`PendingChanges`
    在主循环装配时就绑在写工具上了）。所以它提出的改动会和主循环的改动
    汇成一份 diff，用户只确认一次，也不会出现两套互相矛盾的视图。
    """

    def handler(args: dict) -> ToolResult:
        spec = TaskSpec(
            goal=str(args.get("goal") or "").strip(),
            targets=tuple(str(item) for item in args.get("targets") or ()),
            constraints=tuple(str(item) for item in args.get("constraints") or ()),
            acceptance=str(args.get("acceptance") or "").strip(),
            out_of_scope=tuple(str(item) for item in args.get("out_of_scope") or ()),
        )
        problem = spec.validate()
        if problem is not None:
            return ToolResult(ok=False, content=f"不能派发：{problem}")

        plan = DispatchPlan(delegate=True, reason="主循环在会话中途派发", spec=spec)
        try:
            outcome = run_delegated(
                plan, gateway, tokenizer, registry, config, verify=verify
            )
        except Exception as exc:
            # 派发失败不该把主循环一起带走——它还能自己做。
            return ToolResult(
                ok=False,
                content=(
                    f"派发没能跑起来：{type(exc).__name__}: {exc}。"
                    "这一块你可以自己做，或者换个说法再派一次。"
                ),
            )
        return ToolResult(ok=True, content=_render(outcome), usage=outcome.usage)

    return ToolSpec(
        name="dispatch",
        description=(
            "把一块**有明确验收标准**的活交给子智能体去做：它在一个独立上下文里"
            "实现，再由另一个独立上下文审查，最后把结论交回给你。"
            "适合「要读很多文件、但你不想把这些细节留在自己上下文里」的改动；"
            "一次性小改动自己做更快。acceptance 必须写清怎么算做完——"
            "没有它不允许派发。注意：它会真的改文件（进待确认的 diff），"
            "所以目标要具体到能验收"
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "要它做什么，一句话说清"},
                "acceptance": {
                    "type": "string",
                    "description": "怎么算做完：跑什么命令、看什么结果",
                },
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "涉及的文件或符号",
                },
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "必须遵守的约束",
                },
                "out_of_scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "明确不要动的地方",
                },
            },
            "required": ["goal", "acceptance"],
            "additionalProperties": False,
        },
        handler=handler,
        brief="把一块活交给子智能体",
        group="派",
    )


def _render(outcome: object) -> str:
    """把一次派发的结果压成主循环能直接用的一段。

    主循环要从中得到三件事：**做没做**、**验没验过**、**改动在哪儿**。
    最后一条最重要——不说清楚它会把同样的活再做一遍。
    """
    lines = ["子智能体做过一轮了。"]
    lines.append(f"实现者说：{_flatten(getattr(outcome, 'implementer_final', ''), 400)}")

    review = getattr(outcome, "review", None)
    if review is None:
        lines.append("独立审查：没有进行。")
    elif review.inconclusive:
        lines.append(
            "独立审查：**没有得出结论**（审查者自己没跑完）——"
            "它的判断既不算通过也不算不通过，改动仍待确认。"
        )
    else:
        lines.append("独立审查：" + ("通过。" if review.passed else "**不通过**。"))
        if review.reasons:
            lines.append("理由：" + "；".join(review.reasons))
        if not review.passed and review.fix_goal:
            lines.append(f"它认为要修的是：{review.fix_goal}")

    lines.append(
        f"共 {getattr(outcome, 'rounds', 0)} 轮（实现→审查）。"
        "改动已经在待确认的 diff 里了——**不要自己再写一遍**；"
        "下一步要么根据审查意见再派一次，要么去做别的。"
    )
    return "\n".join(lines)


def _flatten(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    if not flat:
        return "（没说）"
    return flat[:limit] + "…" if len(flat) > limit else flat
