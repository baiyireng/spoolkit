"""计划相关命令：生成、推进、自主执行。

三条路径共用同一套「执行一步并结算」的逻辑。共用是刻意的：
各写一遍的结果，是策略在其中一条路径上悄悄失效——那种 bug 不报错，
只让用户以为「我明明设了只读」。
"""

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agents_dev.agents.plan import (
    BATCH_LIMIT_NOTE,
    DONE,
    FAILED,
    SUBAGENT,
    decompose,
    load_plan,
    narrow_scope,
    plan_path,
    render_step_prompt,
    save_plan,
    extend_plan,
    reflect_progress,
    uncovered,
)
from agents_dev.llm.gateway import CountingGateway
from agents_dev import limits
from agents_dev.cli.options import (
    report_policy,
    resolve_policy,
    resolve_scope,
    resolve_window,
)
from agents_dev.config import Config
from agents_dev.cli.runtime import (
    LoopWiring,
    assemble_loop,
    build_approver,
    build_lessons,
    counter_for,
    open_memory,
    provider_gateway,
    settle_lessons,
)
from agents_dev.cli.settle import settle
from agents_dev.tools.edit import PendingChanges
from agents_dev.tools.grant import Grants
from agents_dev.memory.lessons import record_lesson


def _survey(project_root: Path, goal: str, tokenizer) -> str:
    """拆解**之前**先看一眼环境。

    这一步不是可选的。实测：不看的拆解产出的是**形状完美、内容全错**的计划
    ——20 步、每步都有验收标准和范围，而范围写的是 `task_1/`…`task_20/`
    （真实目录是 `01_off_by_one`…）。那些目录根本不存在，于是每一步的
    范围闸门都会把改动挡在外面，整份计划等于没有。

    收集什么由目标决定：先给目录结构（名字对了，范围才可能对），
    再给检索到的相关代码片段——都不多，够它对齐名字即可。
    """
    lines: list[str] = []
    # 任务材料：说「要做什么」的那些文件（TASK/README/*.md）与验收测试。
    #
    # 这一段是**效率的关键**：拆解时读不到它们，步骤就只能写成
    # 「修复 02_empty_input 中的空输入处理缺陷」这种笼统的话（实测就是这样），
    # 于是每个执行者都得自己去翻一遍——每题多一轮 survey，而每轮还要把后续
    # 提示词一起撑大。契约在这里读一次，比在每个子任务里读一遍便宜得多。
    material_budget = int(limits.resolve("decompose_material_budget", {})[0])
    material: list[str] = []
    used = 0
    exhausted = False
    for home in sorted(p for p in project_root.iterdir() if p.is_dir()):
        if exhausted:
            break
        if home.name == ".agent":
            continue
        for item in sorted(home.iterdir()):
            if not item.is_file():
                continue
            name = item.name.lower()
            is_material = name.endswith(".md") or name.startswith("test_")
            if not is_material:
                continue
            try:
                body = item.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            block = f"### {home.name}/{item.name}\n{body}"
            cost = tokenizer.count(block)
            if used + cost > material_budget:
                # 预算用完了：说一句就够。原先每个目录报一行，50 个目录就是
                # 五十行噪音——而每一批拆解都要把这段材料重发一遍。
                exhausted = True
                break
            material.append(block)
            used += cost
    if exhausted:
        material.append(
            f"（材料预算 {material_budget} token 用完，只展开了 {used} token 的部分，"
            "其余条目没有展开——需要时直接看工作区里的文件）"
        )
    if material:
        lines.append("各题目的材料（题目说明与验收测试）：\n" + "\n\n".join(material))
    try:
        entries = sorted(
            item.name + ("/" if item.is_dir() else "")
            for item in project_root.iterdir()
            if item.name != ".agent"
        )
        if entries:
            lines.append("工作区根目录下的条目：" + "、".join(entries[:60]))
    except OSError:
        pass
    from agents_dev.index.rank import prefetch as prefetch_text
    from agents_dev.tools.dispatch import dispatch_history

    try:
        snippet = prefetch_text(project_root, goal, tokenizer)
    except Exception:
        snippet = ""
    if snippet:
        lines.append("检索到的相关片段：\n" + snippet)
    # 拆解那一步正是决定「谁来做」的时刻，把本工作区的派发战绩摆出来
    history = dispatch_history(project_root)
    if history:
        lines.append(history)
    return "\n\n".join(lines)


def _plan_phase(
    gateway,
    project_root: Path,
    args,
    limit: int | None = None,
    extra: str = "",
    context: str | None = None,
) -> object:
    """拆解那一段：排完、对一遍覆盖、把这笔账说出来。

    账要说出来：拆解在实测里占墙钟的四分之一（50 题 3 次调用、约 5.5 分钟），
    而它原先只有总时间能看，「为什么慢」只能猜。

    覆盖检查也是这一步的活：目标说「把 50 道题都做对」，计划少一道，
    原先没有任何机制会发现。
    """
    counting = CountingGateway(gateway)
    # 覆盖名单由调用方给：什么算「必须覆盖」是关于目标的判断，harness 猜不准。
    # 早先这里自动拿「工作区顶层目录」当名单——那是为「N 道题摆成 N 个目录」
    # 这一种形状写的，放到真实项目上只会误报（src/ 覆盖了，docs/ 没覆盖，
    # 然后去补一堆没人要的步骤）。
    cover = tuple(
        part.strip()
        for part in str(getattr(args, "cover", "") or "").split(",")
        if part.strip()
    )
    started = time.time()
    steps_log: list[str] = []
    # 材料只读一次：分批推进时续排也要用它（不给就会退化成笼统的步骤）。
    surveyed = (
        context
        if context is not None
        else _survey(project_root, args.goal, counter_for(gateway))
    )
    plan = decompose(
        counting,
        args.goal,
        context=surveyed,
        # 分批推进时，第一批只排 batch 步——一次排满就没有"回头看一眼"的机会了。
        limit=int(limit if limit is not None else args.limit),
        # `plan` 子命令没有 --window，`run --autonomous` 有：两边都要能用。
        window=resolve_window(gateway, getattr(args, "window", 0)),
        must_cover=cover,
        trace=steps_log,
        extra=extra,
    )
    elapsed = time.time() - started
    for line in steps_log:
        print("  " + line)
    print(
        f"拆解：{counting.calls} 次调用 / {counting.prompt_tokens} 输入 + "
        f"{counting.completion_tokens} 输出 token / {elapsed:.0f}s"
    )
    missing = uncovered(plan, cover)
    if missing:
        # 补了两轮还缺就报出来——报，不猜：它可能是有意不做的。
        print(
            f"⚠ 计划没有覆盖这些（{len(missing)} 个）："
            + "、".join(missing[:10])
            + ("…" if len(missing) > 10 else ""),
            file=sys.stderr,
        )
    return plan


def make_plan(args: argparse.Namespace) -> int:
    """把一个较大目标拆成可验收的步骤序列并落盘。"""
    project_root = Path(args.root).resolve()
    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2

    plan = _plan_phase(gateway, project_root, args)
    if not plan.steps:
        print("没有拆出任何带验收标准的步骤。", file=sys.stderr)
        return 1

    save_plan(plan_path(project_root), plan)
    print(plan.render())
    print(f"\n计划已保存到 {plan_path(project_root)}")
    return 0


@dataclass
class StepRun:
    """一次步骤运行的上下文。

    这些参数在一次运行里全程不变，打包在一起的理由和 LoopWiring 一样：
    散成位置参数时，调用方看不出自己漏传了什么，而漏传的表现是
    「策略静默失效」，不是报错。
    """

    args: argparse.Namespace
    project_root: Path
    gateway: object
    plan: object
    non_interactive: bool


def execute_step(ctx: StepRun, step, scope: Sequence[str], approver=None, grants=None):
    """执行一个计划步骤，返回（结果，待落盘改动）。"""
    args, project_root, gateway = ctx.args, ctx.project_root, ctx.gateway
    plan = ctx.plan
    report_policy(resolve_policy(args, project_root), scope)
    window = resolve_window(gateway, args.window)
    pending = PendingChanges(project_root)
    memory = (
        None
        if args.no_memory
        else open_memory(
            project_root,
            window,
            getattr(args, "session", "cli"),
            model=f"{args.provider}:{args.model or '默认'}",
        )
    )
    _wiring_started = time.time()
    loop = assemble_loop(
        project_root,
        gateway,
        config=Config(
            project_root=project_root,
            context_window=window,
            max_steps=args.max_steps,
            subagent_steps=args.subagent_steps,
        ),
        wiring=LoopWiring(
            memory=memory,
            pending=pending,
            approver=approver,
            grants=grants,
            lessons=build_lessons(memory) if memory is not None else None,
            # 这一步要动哪儿由计划声明，执行时把它交给预取——
            # 否则预取只能靠整段提示词里的关键词猜，实测会猜偏到测试文件上。
            prefetch_anchors=tuple(step.scope or scope),
        ),
    )
    # 必须在**装配完那一刻**就读走：晚一步（比如等 run 回来再读）量到的
    # 就是整步墙钟，而它会顶着"装配循环"这个名字，把账搅成 0 其它时间。
    _wiring_seconds = time.time() - _wiring_started
    if step.executor == SUBAGENT:
        result = _delegate_step(ctx, step, loop, pending)
    else:
        # goal 用**短目标**，整段步骤提示词只作为提示词发一次：
        # 状态块里再重复一份，等于每次调用多付一遍。
        result = loop.run(step.goal, prompt=render_step_prompt(plan, step))
    result.wiring_seconds = _wiring_seconds

    if memory is not None:
        settle_lessons(memory, result.lessons_pushed, result.finished)

    for line in result.trace:
        print(line)
    print(result.usage())
    return result, pending


def _delegate_step(ctx: StepRun, step, loop, pending) -> object:
    """把这一步交给子智能体：独立上下文实现 + 另一个独立上下文审查。

    为什么要有这一支：`render_step_prompt` 那条路**也是**按「派发契约」的形状
    在喂——goal + 验收标准 + 范围——但由 harness 直接跑，于是派发的决定权与
    **独立审查**都不见了（实测那条路里 `dispatch` 用了 0 次，而验收只剩
    「跑测试」这一种机械判断，它看不出「这处改得是不是偷懒了」）。

    成了哪一步由**审查结论**定，不是由「测试过了」定；改动仍然落在同一份
    待确认里，用户只确认一次。
    """
    from agents_dev.agent.loop import LoopResult
    from agents_dev.agent.state import TaskState
    from agents_dev.agents.dispatcher import DispatchPlan, run_delegated
    from agents_dev.agents.runtime import TaskSpec

    spec = TaskSpec(
        goal=step.goal,
        targets=step.scope,
        acceptance=step.acceptance,
        constraints=("只做这一步，不要顺手做后面步骤的事",),
    )
    plan = ctx.plan
    outcome = run_delegated(
        DispatchPlan(True, "计划里声明了由子智能体执行", spec=spec),
        ctx.gateway,
        loop.tokenizer,
        loop.registry,
        loop.config,
        verify=loop.verify,
    )

    review = outcome.review
    if outcome.too_big:
        finished = False
        final = f"【太大】{outcome.too_big}"
    elif review is None:
        finished = False
        final = outcome.implementer_final
    else:
        finished = review.passed
        verdict = "通过" if review.passed else ("没有结论" if review.inconclusive else "未通过")
        reasons = "；".join(review.reasons)
        final = f"独立审查{verdict}：{reasons}" if reasons else f"独立审查{verdict}"

    trace = [f"[派发] {line}" for line in outcome.trace]
    if len(pending):
        trace.append(f"[派发] 这次改动涉及 {len(pending)} 个文件，仍在待确认里")
    return LoopResult(
        finished=finished,
        final=final,
        state=TaskState(task_id=f"step-{step.index}", goal=step.goal),
        steps=0,
        resets=0,
        trace=trace,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        model_calls=outcome.model_calls,
    )


def record_step(ctx: StepRun, step, result, pending, scope: Sequence[str]) -> None:
    """记录步骤结果并按策略处理待落盘改动。"""
    project_root, plan = ctx.project_root, ctx.plan
    note = (result.final or "").strip().splitlines()[0][:80] if result.final else ""
    plan.mark(step.index, DONE if result.finished else FAILED, note=note)
    save_plan(plan_path(project_root), plan)

    if len(pending):
        settle(
            pending,
            resolve_policy(ctx.args, project_root),
            scope,
            baseline_path=project_root / ".agent" / "last_change.json",
            non_interactive=ctx.non_interactive,
        )


def advance_plan(args: argparse.Namespace, project_root: Path, gateway) -> int:
    """执行计划中下一个待办步骤，并记录结果。"""
    plan = load_plan(plan_path(project_root))
    if plan is None:
        print("还没有计划，请先用 `plan --goal` 生成。", file=sys.stderr)
        return 2

    step = plan.next_pending()
    if step is None:
        print(plan.render())
        print("\n计划已全部完成。")
        return 0

    blocked = plan.blocked_by()
    if blocked is not None:
        print(plan.render())
        print(
            f"\n计划被第 {blocked.index} 步阻塞：{blocked.goal}\n"
            f"原因：{blocked.note or '未记录'}\n"
            "先处理它，或者用 `plan --goal` 重新生成计划。",
            file=sys.stderr,
        )
        return 1

    print(f"执行第 {step.index}/{len(plan.steps)} 步：{step.goal}")
    # 打印的是**这一步实际生效**的范围，不是运行级默认值：
    # 安全边界的输出报错比不输出更糟——看到「**」会以为整个项目都放行了。
    scope = resolve_scope(args, step.scope, default=())
    approver, grants = build_approver(project_root)
    ctx = StepRun(
        args=args,
        project_root=project_root,
        gateway=gateway,
        plan=plan,
        non_interactive=False,
    )
    result, pending = execute_step(
        ctx,
        step,
        scope,
        approver=approver,
        grants=grants,
    )
    record_step(ctx, step, result, pending, scope)
    print(plan.render())
    return 0 if result.finished else 1


def autonomous(args: argparse.Namespace, project_root: Path, gateway) -> int:
    """自己拆解目标并逐步做完。

    唯一不能交给模型的是**授权范围**：如果它既能提范围又能批范围，
    就等于自己给自己发许可证。所以范围必须由 --scope 给出，
    模型只能在里面收窄，不能扩大。
    """
    granted = tuple(part.strip() for part in args.scope.split(",") if part.strip())
    if not granted:
        print(
            "自主模式必须用 --scope 指定授权范围。\n"
            "授权必须来自你——让模型自己定范围，等于它自己给自己发许可证。\n"
            "例如：--scope scratch_lab/",
            file=sys.stderr,
        )
        return 2

    batch = int(getattr(args, "batch", 0) or 0)
    rolling = batch > 0 and not getattr(args, "no_roll", False)
    # 分批推进时第一批只排 batch 步：一次排满就没有"回头看一眼"的机会了。
    # 材料读一次，第一批与后面的续排共用——不给续排材料，它排出来的
    # 步骤会退化成"修复 04 目录下的编程题"这种话（实测）。
    context = _survey(project_root, args.goal, counter_for(gateway))
    plan = _plan_phase(
        gateway,
        project_root,
        args,
        limit=min(int(args.limit), batch) if rolling else int(args.limit),
        # 分批推进时第一批必须补这一句：只说"最多 N 步"会让模型把多件事
        # 并成一条步骤（实测那一步跑了 191 秒）。
        extra=BATCH_LIMIT_NOTE.format(limit=batch) if rolling else "",
        context=context,
    )
    if not plan.steps:
        print("没能拆出任何带验收标准的步骤。", file=sys.stderr)
        return 1
    save_plan(plan_path(project_root), plan)
    print(f"授权范围：{'、'.join(granted)}")
    print(plan.render())

    completed = 0
    ctx = StepRun(
        args=args,
        project_root=project_root,
        gateway=gateway,
        plan=plan,
        non_interactive=True,
    )
    # 教训要写进记忆库：分批推进的"回头看"只有存下来，才能在后面的同类任务里
    # 被主动推送（那才是"在任务中成长"）。--no-memory 时不建，也不留状态。
    lesson_memory = (
        None
        if getattr(args, "no_memory", False)
        else open_memory(
            project_root,
            resolve_window(gateway, getattr(args, "window", 0)),
            getattr(args, "session", "cli"),
            model=f"{getattr(args, 'provider', '')}:{getattr(args, 'model', '') or '默认'}",
        )
    )
    # 分批推进：先做一批 → 回头看一眼 → 再排下一批。
    #
    # 这样每一批都能吃到前一批的**实际结果**（某个接口不是那样、某个约束
    # 计划里没料到），而不是把五十步一次排死。要看完整目标时才用 --no-roll。
    cursor = 0
    while True:
        steps = plan.steps[cursor:]
        if not steps:
            break
        for step in steps:
            cursor = plan.steps.index(step) + 1
            if not _run_one_step(ctx, granted, step, plan):
                print(plan.render())
                return 1
            completed += 1
        if not rolling or completed >= int(getattr(args, "limit", 0) or 0):
            break
        # 这一批做完了：回头看一眼，再排下一批。
        reflection, lessons = reflect_progress(gateway, plan.goal, plan.steps)
        if reflection:
            print(f"\n回头看这一批：{reflection}")
        stored = _keep_lessons(lessons, lesson_memory)
        if stored:
            print(f"（记下 {stored} 条教训——后面同类任务会主动推给它）")
        fresh = extend_plan(
            gateway,
            plan,
            reflection,
            limit=min(batch, max(0, int(getattr(args, "limit", 0) or 0) - len(plan.steps))),
            context=context,
            window=resolve_window(gateway, getattr(args, "window", 0)),
            trace=[],
        )
        if not fresh:
            # 它说没有更多步骤了。这是**它的判断**，如实报出来。
            print("\n续排：模型认为目标已经排完（没有更多步骤）。")
            break
        for step in fresh:
            step.index = len(plan.steps) + 1
            plan.steps.append(step)
        save_plan(plan_path(project_root), plan)
        print(plan.render())

    print(plan.render())
    print(f"\n自主运行结束：完成 {completed}/{len(plan.steps)} 步。")
    return 0 if completed == len(plan.steps) else 1


def _keep_lessons(lessons, memory) -> int:
    """把回头看得来的教训写进教训库，返回写了几条。

    没有记忆会话（`--no-memory`）时就丢掉——这是刻意的：不写记忆的运行
    不该偷偷留下状态。

    教训的**触发词**由模型给（"什么时候该把这条推出来"）。没有它这条教训
    永远不会被推送，所以解析时已经把没触发词的丢掉了。
    """
    if not lessons or memory is None:
        return 0
    for rule, trigger in lessons:
        record_lesson(
            memory._conn,
            rule,
            trigger,
            source="分批推进的回头看",
        )
    return len(lessons)


def _run_one_step(ctx, granted: tuple[str, ...], step, plan) -> bool:
    """跑一步并记账。返回它是否做完了。"""
    blocked = plan.blocked_by()
    if blocked is not None:
        print(
            f"\n计划被第 {blocked.index} 步阻塞：{blocked.goal}"
            f"（{blocked.note or '未记录'}）",
            file=sys.stderr,
        )
        return False

    effective = _effective_scope(granted, step)
    print(f"\n执行第 {step.index}/{len(plan.steps)} 步：{step.goal}")
    _step_started = time.time()
    result, pending = execute_step(
        ctx,
        step,
        effective,
        approver=None,
        grants=Grants(path=ctx.project_root / ".agent" / "grants.json"),
    )
    _step_seconds = time.time() - _step_started
    record_step(ctx, step, result, pending, effective)
    _settle_seconds = time.time() - _step_started - _step_seconds
    _report_step_cost(result, _step_seconds, _settle_seconds)
    return bool(result.finished)


def _report_step_cost(result, step_seconds: float, settle_seconds: float) -> None:
    """把这个**步**的墙钟拆开。

    前面那笔"4.6 秒/步没人认领"是从残差推出来的，而残差推不出结论——
    所以这里把每一步的墙钟当场拆成几笔打出来。谁占大头一眼就看得到，
    不必再去猜。
    """
    known = (
        result.wiring_seconds
        + result.model_seconds
        + result.tool_seconds
        + result.assemble_seconds
        + result.verify_seconds
        + result.state_seconds
        + result.supervisor_seconds
        + settle_seconds
    )
    other = max(0.0, step_seconds + settle_seconds - known)
    print(
        f"本步墙钟 {step_seconds + settle_seconds:.1f}s ｜ "
        f"模型 {result.model_seconds:.1f} 装配循环 {result.wiring_seconds:.1f} "
        f"装配上下文 {result.assemble_seconds:.1f} 工具 {result.tool_seconds:.1f} "
        f"验证 {result.verify_seconds:.1f} 落盘 {result.state_seconds:.1f} "
        f"收尾 {settle_seconds:.1f} 其它 {other:.1f}"
    )


def _effective_scope(granted: tuple[str, ...], step) -> tuple[str, ...]:
    """把这一步的范围收窄到授权之内。

    提议被全部驳回时回退到你授予的范围，而不是回退成「什么都不许」。
    授权来自你；步骤提议只是模型想进一步收窄的意愿，它不该有
    把整步变成只读的能力。
    """
    effective, rejected = narrow_scope(granted, step.scope or granted)
    if rejected:
        print(
            f"第 {step.index} 步提出的范围超出授权，已收窄："
            + "、".join(rejected)
        )
    return effective or granted


def enter_autonomous(args: argparse.Namespace, project_root: Path) -> int:
    """装配网关后进入自主模式。"""
    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2
    return autonomous(args, project_root, gateway)


def enter_advance(args: argparse.Namespace, project_root: Path) -> int:
    """装配网关后推进计划。"""
    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2
    return advance_plan(args, project_root, gateway)
