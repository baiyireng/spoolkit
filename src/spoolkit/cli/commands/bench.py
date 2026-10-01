"""回归任务集命令。"""

import argparse
import os
import sys
import time
from pathlib import Path

from spoolkit.bench import (
    EXIT_ENVIRONMENT_BLOCKED,
    TOGETHER_GOAL,
    WorkspaceUnavailableError,
    check_acceptance_environment,
    describe_environment_problem,
    load_tasks,
    prepare_together,
    render_report,
    render_together,
    run_task,
    verify_together,
)
from spoolkit.config import Config
from spoolkit.cli.runtime import LoopWiring, assemble_loop
from spoolkit.cli.settle import settle
from spoolkit.llm.providers import ProviderConfig, load_gateway
from spoolkit.net import system_proxy
from spoolkit.policy import AUTO
from spoolkit.tools.edit import PendingChanges


def bench(args: argparse.Namespace) -> int:
    """跑回归任务集，用客观验收结果衡量改动效果。"""
    project_root = Path(args.root).resolve()
    tasks = load_tasks(project_root / args.tasks)
    if args.filter:
        tasks = [task for task in tasks if args.filter in task.name]
    if args.limit:
        # 长任务那条仪器动辄几十分钟。先跑前 N 道，形状照样看得见，
        # 而且和整批跑的前 N 道结果可以直接比（题集顺序是稳定的）。
        tasks = tasks[: args.limit]
    if not tasks:
        print("没有匹配的任务。", file=sys.stderr)
        return 2

    print(f"共 {len(tasks)} 个任务，供应商 {args.provider}")

    # 开跑前先确认验收环境跑得起来。这一步是**省钱**的：模型压根没启动，
    # 报告里也不会出现「验收 0/N」这种会被读成「模型不行」的东西。
    environment = check_acceptance_environment(tasks)
    if environment.blocked:
        print(describe_environment_problem(environment), file=sys.stderr)
        return EXIT_ENVIRONMENT_BLOCKED
    if environment.missing_pytest:
        # 这批任务的验收只用到 python，所以不拦；但要说一声——否则以后有人往
        # 题集里加一道 pytest 题，会以为是模型突然变差了。
        print(
            "注意：选中的解释器里没有 pytest"
            f"（{environment.interpreter}）；这批任务的验收命令用不到它，照跑。"
            "要跑 pytest 类任务就得换解释器（见 docs/benchmarking.md）。",
            file=sys.stderr,
        )

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
        try:
            results.append(
                run_task(task, build, settle_changes=settle_now, verbose=args.verbose)
            )
        except WorkspaceUnavailableError as exc:
            # 环境把目录权限锁死了。这是环境的问题，不是题目也不是模型的问题——
            # 所以给一句人话（含路径与原因），别把原始 traceback 扔出来。
            print(str(exc), file=sys.stderr)
            return 2

    print()
    print(render_report(results))
    if any(item.blocked for item in results):
        # 有任务的验收根本没跑起来（环境问题）。这时候「整体失败」是错的结论，
        # 所以用一个与「有任务没过」不同的退出码，脚本也能分辨。
        return EXIT_ENVIRONMENT_BLOCKED
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
    try:
        prepare_together(tasks, workspace)
    except WorkspaceUnavailableError as exc:
        # 同上：环境限制目录权限时给人话，不抛裸 traceback。
        print(str(exc), file=sys.stderr)
        return 2
    print(f"整批单会话：{len(tasks)} 道题布置在 {workspace}")

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
        print(f"其收尾报告：\n{result.final}")

    results = verify_together(tasks, workspace)
    print()
    print(
        render_together(
            results,
            {
                "steps": result.steps,
                "calls": result.model_calls,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "seconds": round(time.time() - started, 1),
                "workspace": workspace,
            },
        )
    )
    if any(item.blocked for item in results):
        return EXIT_ENVIRONMENT_BLOCKED
    return 0 if all(item.passed for item in results) else 1
