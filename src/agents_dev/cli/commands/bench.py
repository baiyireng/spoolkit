"""回归任务集命令。"""

import argparse
import os
import sys
import time
from pathlib import Path

from agents_dev.bench import (
    TOGETHER_GOAL,
    load_tasks,
    prepare_together,
    render_report,
    render_together,
    run_task,
    verify_together,
)
from agents_dev.config import Config
from agents_dev.cli.runtime import LoopWiring, assemble_loop
from agents_dev.cli.settle import settle
from agents_dev.llm.providers import ProviderConfig, load_gateway
from agents_dev.net import system_proxy
from agents_dev.policy import AUTO
from agents_dev.tools.edit import PendingChanges


def bench(args: argparse.Namespace) -> int:
    """跑回归任务集，用客观验收结果衡量改动效果。"""
    project_root = Path(args.root).resolve()
    tasks = load_tasks(project_root / args.tasks)
    if args.filter:
        tasks = [task for task in tasks if args.filter in task.name]
    if not tasks:
        print("没有匹配的任务。", file=sys.stderr)
        return 2

    print(f"共 {len(tasks)} 个任务，供应商 {args.provider}")
    if args.together:
        return _together(args, project_root, tasks)

    results = []
    for task in tasks:
        # 用容器而不是默认参数传 pending：默认参数在函数定义时就绑定，
        # build 里重新赋值影响不到它，结算的就会是一个空集合——
        # 表现是「改动没落盘，验收全失败」，而原因跟模型毫无关系。
        holder: dict = {}

        def build(workspace: Path):
            pending = PendingChanges(workspace)
            holder["pending"] = pending
            # 凭据与供应商设置来自真实项目根，工作区只是临时的任务目录——
            # 拿临时目录去找 .env 当然找不到。
            gateway = load_gateway(
                args.provider,
                ProviderConfig(
                    project_root=project_root,
                    model=args.model,
                    base_url=args.base_url,
                    proxy=args.proxy if args.proxy else system_proxy(),
                    env=dict(os.environ),
                ),
            )
            return assemble_loop(
                workspace,
                gateway,
                config=Config(
                    project_root=workspace,
                    context_window=args.window or 8192,
                    max_steps=args.max_steps,
                ),
                wiring=LoopWiring(pending=pending),
            )

        def settle_now(task=task):
            # 无人工确认，改动直接落盘并记录基线，方便事后回看
            pending = holder.get("pending")
            if pending is None:
                return
            settle(pending, AUTO, task.scope or ("**",), non_interactive=True)

        print(f"  → {task.name}")
        results.append(
            run_task(task, build, settle_changes=settle_now, verbose=args.verbose)
        )

    print()
    print(render_report(results))
    return 0 if all(item.passed for item in results) else 1


def _together(args, project_root: Path, tasks) -> int:
    """把整批题铺进一个工作区，全部交给**一条会话**。

    与一题一会话的差别不是省事，是量的东西不一样：这里量的是任务发现、
    自我排序、长程记忆（上下文重置之后还记不记得做到哪儿）以及督导与派发
    在长任务里第一次真正被用上。
    """
    workspace = (
        project_root
        / ".agent"
        / "bench-together"
        / time.strftime("%Y%m%d-%H%M%S")
    )
    prepare_together(tasks, workspace)
    print(f"整批一条会话：{len(tasks)} 道题铺在 {workspace}")

    pending = PendingChanges(workspace)
    gateway = load_gateway(
        args.provider,
        ProviderConfig(
            project_root=project_root,
            model=args.model,
            base_url=args.base_url,
            proxy=args.proxy if args.proxy else system_proxy(),
            env=dict(os.environ),
        ),
    )
    loop = assemble_loop(
        workspace,
        gateway,
        config=Config(
            project_root=workspace,
            context_window=args.window or 8192,
            max_steps=args.max_steps,
            step_ceiling=args.step_ceiling,
        ),
        wiring=LoopWiring(pending=pending),
    )

    started = time.time()
    result = loop.run(TOGETHER_GOAL.format(count=len(tasks)))
    # 长任务里未落盘的改动会一直攒着，最后统一落盘——这是真实使用的形状。
    settle(pending, AUTO, ("**",), non_interactive=True)

    if args.verbose:
        print("轨迹：")
        for line in result.trace:
            print(f"  {line}")
        print(f"它自己的收尾报告：\n{result.final}")

    results = verify_together(tasks, workspace)
    print()
    print(
        render_together(
            results,
            {
                "steps": result.steps,
                "calls": result.model_calls,
                "prompt_tokens": result.prompt_tokens,
                "seconds": round(time.time() - started, 1),
                "workspace": workspace,
            },
        )
    )
    return 0 if all(item.passed for item in results) else 1
