"""回归任务集命令。"""

import argparse
import os
import sys
from pathlib import Path

from agents_dev.bench import load_tasks, render_report, run_task
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
