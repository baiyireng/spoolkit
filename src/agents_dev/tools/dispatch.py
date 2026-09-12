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

# 一次派发最多带几件。子智能体一轮的步数预算默认 20 步，而一件活平均要
# 2～3 步（读、改、验各一步）——按这个算，十件上下就是它的上限。
#
# 超过就**当场拦下并说明算法**，而不是让它硬做：实测一次派了 50 件进去，
# 实现者把预算烧光后撞上重复保护，回来只剩一句无从行动的话，然后还被送去
# 审查——审查者审的是个半成品。规模问题该在派之前拦。
MAX_TARGETS = 10


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

        if len(spec.targets) > MAX_TARGETS:
            return ToolResult(
                ok=False,
                content=(
                    f"不能派发：这次带了 {len(spec.targets)} 件，超出一次能派的上限"
                    f"（{MAX_TARGETS} 件）。子智能体的步数预算是一轮 "
                    f"{config.subagent_steps} 步，而一件活平均要 2～3 步——"
                    "派大了它只会烧光预算、交回一个半成品。"
                    "拆成几批再派，或者自己先做掉一部分。"
                ),
            )

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
        if outcome.too_big:
            # 「太大」是**任务规模**问题，不是「做砸了」：要让它能据此行动，
            # 就不能混在「审查未通过」里。
            return ToolResult(
                ok=False,
                content=(
                    "派出去的活没做完——子智能体说这块对它太大：\n"
                    f"{outcome.too_big}\n"
                    f"（一次能派的上限约 {MAX_TARGETS} 件，它的步数预算是"
                    f" {config.subagent_steps} 步。）"
                    "拆小之后再派；也可以自己先做掉一部分再派剩下的。"
                ),
                usage=outcome.usage,
            )
        return ToolResult(ok=True, content=_render(outcome), usage=outcome.usage)

    return ToolSpec(
        name="dispatch",
        description=(
            "把一块**有明确验收标准**的活交给子智能体去做：它在一个独立上下文里"
            "实现，再由另一个独立上下文审查，最后把结论交回给你。"
            "适合「要读很多文件、但你不想把这些细节留在自己上下文里」的改动。"
            "**一次派发可以带一批活**：一批同类的小活合成一次，摊薄固定的那几次"
            "调用之后才划算——单看一道小题，派发是亏的，所以别一道一道派。"
            "批次别太大：子智能体的上下文和你一样是有限的，一次交 3～5 件"
            "（或者一批彼此相像、验收方式相同的活）比较稳；上限见参数校验的报错。"
            "**它做得完做不完会如实告诉你**：如果这块对它太大，它会在结论里直接说"
            "「太大」并给出拆法，那时把任务拆小再派，不要硬塞——硬塞的代价是"
            "烧掉它的整轮预算，换回一个半成品。"
            "acceptance 必须写清怎么算做完——没有它不允许派发；带一批活时，"
            "验收方式要能覆盖整批（例如「这些目录里逐个跑 pytest 都通过」）。"
            "注意：它会真的改文件（进待确认的 diff），所以目标要具体到能验收"
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
