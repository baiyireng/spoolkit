"""计划相关命令：生成、推进、自主执行。

三条路径共用同一套「执行一步并结算」的逻辑。共用是刻意的：
各写一遍的结果，是策略在其中一条路径上悄悄失效——那种 bug 不报错，
只让用户以为「我明明设了只读」。
"""

import argparse
import sys
from pathlib import Path
from typing import Sequence

from agents_dev.agents.plan import (
    DONE,
    FAILED,
    decompose,
    load_plan,
    narrow_scope,
    plan_path,
    render_step_prompt,
    save_plan,
)
from agents_dev.cli.options import (
    report_policy,
    resolve_policy,
    resolve_scope,
    resolve_window,
)
from agents_dev.cli.runtime import (
    assemble_loop,
    build_approver,
    build_lessons,
    open_memory,
    provider_gateway,
    settle_lessons,
)
from agents_dev.cli.settle import settle
from agents_dev.tools.edit import PendingChanges
from agents_dev.tools.grant import Grants


def make_plan(args: argparse.Namespace) -> int:
    """把一个较大目标拆成可验收的步骤序列并落盘。"""
    project_root = Path(args.root).resolve()
    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2

    plan = decompose(gateway, args.goal, limit=args.limit)
    if not plan.steps:
        print("没有拆出任何带验收标准的步骤。", file=sys.stderr)
        return 1

    save_plan(plan_path(project_root), plan)
    print(plan.render())
    print(f"\n计划已保存到 {plan_path(project_root)}")
    return 0


def execute_step(
    args: argparse.Namespace,
    project_root: Path,
    gateway,
    plan,
    step,
    scope: Sequence[str],
    non_interactive: bool,
    approver=None,
    grants=None,
):
    """执行一个计划步骤，返回（结果，待落盘改动）。"""
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
    loop = assemble_loop(
        project_root,
        gateway,
        window=window,
        max_steps=args.max_steps,
        subagent_steps=args.subagent_steps,
        memory=memory,
        pending=pending,
        approver=approver,
        grants=grants,
        lessons=build_lessons(memory) if memory is not None else None,
    )
    result = loop.run(render_step_prompt(plan, step))

    if memory is not None:
        settle_lessons(memory, result.lessons_pushed, result.finished)

    for line in result.trace:
        print(line)
    print(result.usage())
    return result, pending


def record_step(
    project_root: Path,
    plan,
    step,
    result,
    pending,
    scope: Sequence[str],
    policy: str,
    non_interactive: bool,
) -> None:
    """记录步骤结果并按策略处理待落盘改动。"""
    note = (result.final or "").strip().splitlines()[0][:80] if result.final else ""
    plan.mark(step.index, DONE if result.finished else FAILED, note=note)
    save_plan(plan_path(project_root), plan)

    if len(pending):
        settle(
            pending,
            policy,
            scope,
            baseline_path=project_root / ".agent" / "last_change.json",
            non_interactive=non_interactive,
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
    result, pending = execute_step(
        args,
        project_root,
        gateway,
        plan,
        step,
        scope,
        non_interactive=False,
        approver=approver,
        grants=grants,
    )
    record_step(
        project_root,
        plan,
        step,
        result,
        pending,
        scope,
        resolve_policy(args, project_root),
        non_interactive=False,
    )
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

    plan = decompose(gateway, args.goal, limit=args.limit)
    if not plan.steps:
        print("没能拆出任何带验收标准的步骤。", file=sys.stderr)
        return 1
    save_plan(plan_path(project_root), plan)
    print(f"授权范围：{'、'.join(granted)}")
    print(plan.render())

    policy = resolve_policy(args, project_root)
    completed = 0
    for step in plan.steps:
        blocked = plan.blocked_by()
        if blocked is not None:
            print(
                f"\n计划被第 {blocked.index} 步阻塞：{blocked.goal}"
                f"（{blocked.note or '未记录'}）",
                file=sys.stderr,
            )
            break

        effective = _effective_scope(granted, step)

        print(f"\n执行第 {step.index}/{len(plan.steps)} 步：{step.goal}")
        result, pending = execute_step(
            args,
            project_root,
            gateway,
            plan,
            step,
            effective,
            non_interactive=True,
            approver=None,
            grants=Grants(path=project_root / ".agent" / "grants.json"),
        )
        record_step(
            project_root,
            plan,
            step,
            result,
            pending,
            effective,
            policy,
            non_interactive=True,
        )
        if not result.finished:
            print(plan.render())
            return 1
        completed += 1

    print(plan.render())
    print(f"\n自主运行结束：完成 {completed}/{len(plan.steps)} 步。")
    return 0 if completed == len(plan.steps) else 1


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
