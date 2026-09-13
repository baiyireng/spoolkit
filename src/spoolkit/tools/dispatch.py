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

import time
from dataclasses import dataclass
from pathlib import Path

from spoolkit.agents.dispatcher import DispatchPlan, run_delegated
from spoolkit.agents.runtime import TaskSpec
from spoolkit.config import Config
from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult, ToolSpec

# 派发战绩的行数与条数上限：它只在「正好要判断」的时候被取用（见下面
# dispatch_history），所以留着比丢掉划算——但也别无限长。
LOG_LIMIT = 20
HISTORY_LIMIT = 5


def _log_path(root: Path) -> Path:
    return root / ".agent" / "dispatch-log.md"


def record_dispatch(
    root: Path,
    *,
    kind: str,
    calls: int,
    rounds: int,
    targets: int,
    note: str = "",
    implementer_steps: int = 0,
    reviewer_steps: int = 0,
) -> None:
    """把一次派发的结果记在工作区里。

    为什么要有这份记录：主循环对子智能体的认知原本是**静态的两句话**——
    知道有个 dispatch 工具、知道什么活适合派，但**不知道在这个工作区里
    派出去是赚是亏**。而教训机制记的是任务级成败，不是派发级的；一次派发
    失败（审查没过、或者「太大」）不会回流到下次判断里。

    记录失败不影响任务本身：它是给人看的旁证。
    """
    path = _log_path(root)
    stamp = time.strftime("%m-%d %H:%M")
    # 两个角色各花多少步要记下来：「审查没给出结论」时，「它把预算花在验证
    # 动作上了」和「它判断不出来」是两件完全不同的事，只看一句结论分不清。
    steps = f"实现 {implementer_steps} 步 / 审查 {reviewer_steps} 步"
    # 目标件数是模型自己声明的；它常常不填，那写「0 件」不如说清是没声明。
    size = f"目标 {targets} 件" if targets else "目标未声明"
    line = f"- {stamp} [{kind}] {calls} 次调用 / {rounds} 轮 / {steps} / {size}"
    if note:
        line += f" — {' '.join(note.split())[:80]}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        body = [ln for ln in old if ln.strip()][-LOG_LIMIT:]
        body.append(line)
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
    except OSError:
        pass


def dispatch_history(root: Path, limit: int = HISTORY_LIMIT) -> str:
    """派发战绩的一小段，给「要不要派」当依据。

    只在两个时刻取用：模型问 `tool_help("dispatch")` 时（那正是它准备派的
    时刻），以及拆解前收集环境时（那正是它决定「谁来做」的时刻）。
    常驻提示词里一个字都不放——那是每个请求的税。
    """
    path = _log_path(root)
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return ""
    if not lines:
        return ""
    recent = lines[-limit:]
    counts: dict[str, int] = {}
    for line in lines:
        for kind in ("通过", "未通过", "没结论", "太大", "失败"):
            if f"[{kind}]" in line:
                counts[kind] = counts.get(kind, 0) + 1
                break
    tally = "、".join(f"{k} {v} 次" for k, v in counts.items()) or "（没法归类）"
    return (
        f"本工作区记过的派发（共 {len(lines)} 次：{tally}）。最近几次：\n"
        + "\n".join(recent)
        # 「没结论」很容易被读成「派发不靠谱」——实测有一次它记的是「没结论」，
        # 而那 8 道题客观验收 8/8 全过（审查者没跑完，不是活没做对）。
        # 教训必须教对东西，否则这条回路会把它推向「干脆别派」。
        + "\n（注意：「没结论」只说明审查没跑完，不代表改动没做对；"
        "实测有一次记「没结论」而客观验收是 8/8。要判断值不值得派，"
        "看「通过/未通过」，别把「没结论」当失败。）"
    )


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

    # 这两个数是**能力标定**，从登记表 + 本次运行的覆盖来，而不是写死在
    # 工具里：本地 20 步预算的小模型和远程强模型不是一回事。
    max_targets = int(config.limit("max_targets"))
    review_limit = int(config.limit("review_limit"))

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

        if len(spec.targets) > max_targets:
            return ToolResult(
                ok=False,
                content=(
                    f"不能派发：这次带了 {len(spec.targets)} 件，超出一次能派的上限"
                    f"（{max_targets} 件）。子智能体的步数预算是一轮 "
                    f"{config.subagent_steps} 步，而一件活平均要 2～3 步——"
                    "派大了它只会烧光预算、交回一个半成品。"
                    "拆成几批再派，或者自己先做掉一部分。"
                    "（这个数是能力标定值，`limits --set max_targets=20` 可以调大。）"
                ),
            )

        plan = DispatchPlan(delegate=True, reason="主循环在会话中途派发", spec=spec)
        try:
            outcome = run_delegated(
                plan, gateway, tokenizer, registry, config, verify=verify
            )
        except Exception as exc:
            record_dispatch(
                config.project_root,
                kind="失败",
                calls=0,
                rounds=0,
                targets=len(spec.targets),
                note=f"{type(exc).__name__}: {exc}",
            )
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
                    f"（一次能派的上限约 {max_targets} 件，它的步数预算是"
                    f" {config.subagent_steps} 步。）"
                    "拆小之后再派；也可以自己先做掉一部分再派剩下的。"
                ),
                usage=outcome.usage,
            )
        content = _render(outcome)
        if len(spec.targets) > review_limit:
            # 结果里说，而不是只在描述里说：这一刻它刚看到「这批能不能被审出
            # 结论」，而这是它决定下一批派多少的唯一时机。
            #
            # 注意这条**只是提醒**，不是上限。曾经把它改成硬拦，理由是「审查在
            # 大批次上结不出结论」——那条证据后来被推翻了：真正的原因是督导
            # 缺少「它已经做完了」这种结论（见 supervisor.FINISH），同一个 9 件
            # 派发在修好之后连过两次。**混杂观察不能当结论用。**
            content += (
                f"\n\n⚠ 这批带了 {len(spec.targets)} 件，超过 {review_limit} 件："
                "审查者要在一个上下文里核对这么多处，容易给不出结论。"
                f"下一批可以考虑拆到 {review_limit} 件以内。"
            )
        return ToolResult(ok=True, content=content, usage=outcome.usage)

    # 战绩附录进「怎么用这个工具」的说明里：模型调 tool_help("dispatch")
    # 的那一刻，正是它准备做派发决定的那一刻——钱花在这里最值。
    history = dispatch_history(config.project_root)
    notes = ("\n\n" + history) if history else ""
    return ToolSpec(
        name="dispatch",
        description=(
            "把一块**有明确验收标准**的活交给子智能体去做：它在一个独立上下文里"
            "实现，再由另一个独立上下文审查，最后把结论交回给你。"
            "适合「要读很多文件、但你不想把这些细节留在自己上下文里」的改动。"
            "**一次派发可以带一批活**：一批同类的小活合成一次，摊薄固定的那几次"
            "调用之后才划算——单看一道小题，派发是亏的，所以别一道一道派。"
            f"批次别太大：子智能体的上下文和你一样有限，一次交 3～5 件比较稳。"
            f"超过 {review_limit} 件时**审查那一步容易给不出结论**（实测 8 件那次："
            "活全做对了，审查没结论）——审查者要在一个上下文里核对所有改动。"
            f"绝对上限 {max_targets} 件，硬拦。"
            "**它做得完做不完会如实告诉你**：如果这块对它太大，它会在结论里直接说"
            "「太大」并给出拆法，那时把任务拆小再派，不要硬塞——硬塞的代价是"
            "烧掉它的整轮预算，换回一个半成品。"
            "acceptance 必须写清怎么算做完——没有它不允许派发；带一批活时，"
            "验收方式要能覆盖整批（例如「这些目录里逐个跑 pytest 都通过」）。"
            "注意：它会真的改文件（进待确认的 diff），所以目标要具体到能验收"
            + notes
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
            # targets 必填：它是「这次要动哪些东西」的声明。没有它，
            # 批次大小就无从判断（实测那次 8 件的派发没声明目标，
            # 于是所有跟件数有关的约束都落不了地）。
            "required": ["goal", "acceptance", "targets"],
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
        # 审查没结论时，把它最后几步交出来：那是唯一能看出「它卡在哪」的东西。
        # 没有这一段，主循环（和人）只能看到一句「没结论」，连猜都没处猜。
        tail = [
            line for line in getattr(outcome, "trace", []) if line.startswith("[审查")
        ][-3:]
        if tail:
            lines.append("审查最后几步：" + " ｜ ".join(tail))
    else:
        lines.append("独立审查：" + ("通过。" if review.passed else "**不通过**。"))
        if review.reasons:
            lines.append("理由：" + "；".join(review.reasons))
        if not review.passed and review.fix_goal:
            lines.append(f"它认为要修的是：{review.fix_goal}")

    lines.append(
        f"共 {getattr(outcome, 'rounds', 0)} 轮（实现→审查）。"
        f"（实现者 {getattr(outcome, 'implementer_steps', 0)} 步 / "
        f"审查者 {getattr(outcome, 'reviewer_steps', 0)} 步 / "
        f"合计 {getattr(outcome, 'model_calls', 0)} 次调用）"
        "改动已经在待确认的 diff 里了——**不要自己再写一遍**；"
        "下一步要么根据审查意见再派一次，要么去做别的。"
    )
    return "\n".join(lines)


def _flatten(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    if not flat:
        return "（没说）"
    return flat[:limit] + "…" if len(flat) > limit else flat
