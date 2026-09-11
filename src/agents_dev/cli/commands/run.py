"""默认命令：跑一次任务。

三条分支——派发、计划、自主——在装配网关之后分流。共同的部分
（窗口解析、网关装载、授权询问器）放在这里，避免各分支重复。
"""

import argparse
import sys
from pathlib import Path

from agents_dev.agents.dispatcher import plan_dispatch, run_delegated
from agents_dev.config import Config
from agents_dev.cli.commands.plan import advance_plan, autonomous
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
    show_history,
)
from agents_dev.cli.settle import settle
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.memory.distill import distill
from agents_dev.memory.transcript import ASSISTANT, USER, record_message
from agents_dev.tools.edit import PendingChanges


def run(args: argparse.Namespace) -> int:
    """执行一次任务。"""
    project_root = Path(args.root).resolve()

    # 走计划时目标来自计划文件，命令行不该再强制要求填一次。
    if not args.plan and not args.goal.strip():
        print("需要 --goal，或者用 --plan 推进已有计划。", file=sys.stderr)
        return 2

    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2

    window = resolve_window(gateway, args.window)
    print(f"上下文窗口：{window} token")

    if args.plan:
        return advance_plan(args, project_root, gateway)
    if args.autonomous:
        return autonomous(args, project_root, gateway)

    report_policy(resolve_policy(args, project_root), resolve_scope(args))
    pending = PendingChanges(project_root)
    if args.delegate:
        return _delegated(args, project_root, gateway, window, pending)
    return _standard(args, project_root, gateway, window, pending)


def _standard(args, project_root, gateway, window, pending) -> int:
    """普通路径：主循环自己完成。"""
    approver, grants = build_approver(project_root)
    memory = None
    distiller = None
    if not args.no_memory:
        memory = open_memory(
            project_root,
            window,
            args.session,
            model=f"{args.provider}:{args.model or '默认'}",
        )
        # 假模型没有多余脚本条目可分给归纳调用，因此只在真实供应商下启用。
        if args.provider != "fake":
            distiller = lambda state, final: distill(gateway, state, final=final)

    loop = assemble_loop(
        project_root,
        gateway,
        window=window,
        max_steps=args.max_steps,
        subagent_steps=args.subagent_steps,
        memory=memory,
        distiller=distiller,
        pending=pending,
        approver=approver,
        grants=grants,
        lessons=build_lessons(memory) if memory is not None else None,
    )
    checkpoint = loop.config.task_path("task")
    if memory is not None:
        if not args.no_history:
            show_history(memory, args.session, args.history)
        record_message(memory._conn, args.session, USER, args.goal)
    if checkpoint.exists() and not args.resume:
        print(
            f"发现未完成的检查点（{checkpoint}）。"
            "加 --resume 可以接着做，不加则从零开始。"
        )

    result = loop.run(args.goal, resume=args.resume)
    if memory is not None:
        settle_lessons(memory, result.lessons_pushed, result.finished)
        record_message(
            memory._conn,
            args.session,
            ASSISTANT,
            result.final or "（没有产出）",
            meta=f"{result.usage()}，{'完成' if result.finished else '未完成'}",
        )

    for line in result.trace:
        print(line)
    print("---")
    print(result.usage())
    print("---")
    print(result.final)

    _settle_pending(args, project_root, pending)
    return 0 if result.finished else 1


def _delegated(args, project_root, gateway, window, pending) -> int:
    """派发路径：先判断值不值得派发，值得就交实现者做、审查者独立验。"""
    plan = plan_dispatch(gateway, args.goal)
    print(f"分派判断：{'派发' if plan.delegate else '自己完成'} —— {plan.reason}")
    if not plan.delegate:
        print("未派发，请去掉 --delegate 让主循环自己完成。")
        return 0

    registry = _plain_registry(project_root, pending)
    delegated = run_delegated(
        plan,
        gateway,
        OfflineTokenCounter(),
        registry,
        Config(
            project_root=project_root,
            context_window=window,
            max_steps=args.max_steps,
            subagent_steps=args.subagent_steps,
        ),
    )
    for line in delegated.trace:
        print(line)
    print("--- 实现 ---")
    print(delegated.implementer_final)
    print("--- 独立审查 ---")
    print(delegated.reviewer_final)
    _settle_pending(args, project_root, pending)
    return 0


def _plain_registry(project_root, pending):
    """派发路径用的注册表：不带记忆与教训，子智能体只拿任务说明。"""
    from agents_dev.cli.runtime import attach_index
    from agents_dev.tools.edit import replace_lines_spec, write_file_spec
    from agents_dev.tools.exec import run_command_spec
    from agents_dev.tools.fs import list_dir_spec, read_file_spec
    from agents_dev.tools.registry import ToolRegistry
    from agents_dev.tools.search import search_code_spec

    registry = ToolRegistry()
    registry.register(read_file_spec(project_root))
    registry.register(list_dir_spec(project_root))
    registry.register(search_code_spec(project_root))
    registry.register(run_command_spec(project_root))
    registry.register(write_file_spec(project_root, pending))
    registry.register(replace_lines_spec(project_root, pending))
    attach_index(project_root, registry, OfflineTokenCounter())
    return registry


def _settle_pending(args, project_root, pending) -> None:
    if not len(pending):
        return
    print("---")
    settle(
        pending,
        resolve_policy(args, project_root),
        resolve_scope(args),
        baseline_path=project_root / ".agent" / "last_change.json",
    )
