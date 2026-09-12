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
from agents_dev import diagnosis
from agents_dev.cli.options import (
    report_policy,
    resolve_policy,
    resolve_scope,
    resolve_window,
)
from agents_dev.cli.runtime import (
    LoopWiring,
    assemble_loop,
    build_approver,
    build_lessons,
    open_memory,
    provider_gateway,
    settle_lessons,
    show_history,
)
from agents_dev.cli.settle import settle
from agents_dev.cli.events import EventWriter, settle_with_events
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.memory.distill import distill
from agents_dev.memory.transcript import ASSISTANT, USER, record_message
from agents_dev.tools.edit import PendingChanges
from agents_dev.web.protocol import FINAL, START, USAGE


def run(args: argparse.Namespace) -> int:
    """执行一次任务。"""
    project_root = Path(args.root).resolve()
    read_roots, bad_roots = _resolve_read_roots(args, project_root)
    if bad_roots:
        for item in bad_roots:
            print(f"--allow-read 指向的目录不存在: {item}", file=sys.stderr)
        return 2

    # 走计划时目标来自计划文件，命令行不该再强制要求填一次。
    if not args.plan and not args.goal.strip():
        print("需要 --goal，或者用 --plan 推进已有计划。", file=sys.stderr)
        return 2

    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2

    window = resolve_window(gateway, args.window)
    # --events 模式下 stdout 上只能有 JSON 行。散文会被解析器忽略，
    # 但录下来的文件会变脏，调试时难读——而那正是我们的第一个观察手段。
    if not args.events:
        print(f"上下文窗口：{window} token")

    if args.plan:
        return advance_plan(args, project_root, gateway)
    if args.autonomous:
        return autonomous(args, project_root, gateway)

    if not args.events:
        report_policy(resolve_policy(args, project_root), resolve_scope(args))
    pending = PendingChanges(project_root)
    if args.events:
        return _events_mode(args, project_root, gateway, window, pending)
    if args.delegate:
        return _delegated(args, project_root, gateway, window, pending)
    return _standard(args, project_root, gateway, window, pending)


def _events_mode(args, project_root, gateway, window, pending) -> int:
    """`--events` 模式：stdout 上只有 JSON 行。

    与散文模式共用同一套装配与同一个主循环，只换了输入输出通道。
    两条路径的判定必须一致——同一个策略在终端和网页里表现不同，
    那种差异不会报错，只会让人困惑。
    """
    writer = EventWriter()
    before = len(diagnosis.load_requests(project_root))
    policy = resolve_policy(args, project_root)
    scope = resolve_scope(args)
    writer.emit(
        START, session=args.session, goal=args.goal, policy=policy, window=window
    )

    memory = (
        None
        if args.no_memory
        else open_memory(
            project_root,
            window,
            args.session,
            model=f"{args.provider}:{args.model or '默认'}",
        )
    )
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
            lessons=build_lessons(memory) if memory is not None else None,
            on_event=writer.handle,
            read_roots=_resolve_read_roots(args, project_root)[0],
        ),
    )
    result = loop.run(args.goal, resume=args.resume)
    _maybe_file_diagnosis(project_root, before, result, writer)

    if memory is not None:
        settle_lessons(memory, result.lessons_pushed, result.finished)

    writer.emit(
        USAGE,
        steps=result.steps,
        calls=result.model_calls,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    outcome = settle_with_events(
        pending, policy, scope, project_root / ".agent" / "last_change.json", writer
    )
    writer.emit(FINAL, ok=result.finished, text=result.final or "", settled=outcome)
    return 0 if result.finished else 1


def _standard(args, project_root, gateway, window, pending) -> int:
    """普通路径：主循环自己完成。"""
    approver, grants = build_approver(project_root)
    before = len(diagnosis.load_requests(project_root))
    read_roots, _ = _resolve_read_roots(args, project_root)
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
        config=Config(
            project_root=project_root,
            context_window=window,
            max_steps=args.max_steps,
            subagent_steps=args.subagent_steps,
        ),
        wiring=LoopWiring(
            memory=memory,
            distiller=distiller,
            pending=pending,
            approver=approver,
            grants=grants,
            lessons=build_lessons(memory) if memory is not None else None,
            read_roots=read_roots,
        ),
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

    read_roots, _ = _resolve_read_roots(args, project_root)
    registry = _plain_registry(project_root, pending, read_roots)
    # 子智能体和主循环共用同一套自动验证：审查者本来就有 run_command，
    # 但实测模型几乎从不主动跑测试——指望它凭自觉去验是不现实的。
    from agents_dev.tools.verify import make_verifier

    # 审查者要审的是「改动差异」，不是实现者的总结文字。不给 diff 的话，
    # 它只能靠反复读文件自己还原改了什么——实测它为此翻到步数上限。
    artifacts = "\n\n".join(
        f"--- {change.path} ---\n{change.diff}" for change in pending.items()
    )
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
        artifacts=artifacts,
        verify=make_verifier(project_root, pending),
    )
    for line in delegated.trace:
        print(line)
    print("--- 实现 ---")
    print(delegated.implementer_final)
    print("--- 独立审查 ---")
    print(delegated.reviewer_final)
    if delegated.rejected:
        print("--- 审查未通过 ---")
        print("改动还在待确认清单里，没有自动落盘。理由：")
        for reason in delegated.review.reasons:
            print(f"  - {reason}")
    elif delegated.review is not None and delegated.review.inconclusive:
        print("--- 审查没有结论 ---")
        print("审查者自己没跑完，所以这次改动既没被否定也没被确认：")
        for reason in delegated.review.reasons:
            print(f"  - {reason}")
    _settle_pending(args, project_root, pending)
    return 0


def _plain_registry(project_root, pending, read_roots=()):
    """派发路径用的注册表：不带记忆与教训，子智能体只拿任务说明。"""
    from agents_dev.cli.runtime import attach_index
    from agents_dev.tools.edit import register_edit_tools
    from agents_dev.tools.diagnosis import (
        read_diagnosis_spec,
        request_diagnosis_spec,
    )
    from agents_dev.tools.exec import run_command_spec
    from agents_dev.tools.fs import list_dir_spec, read_file_spec
    from agents_dev.tools.registry import ToolRegistry
    from agents_dev.tools.search import search_code_spec
    from agents_dev.tools.stats import dir_stats_spec
    from agents_dev.tools.calc import calc_spec

    registry = ToolRegistry()
    # 与主循环一致：读工具要能看到待确认的改动，否则子智能体读到的
    # 是改之前的文件，而它跑测试看到的是改之后的——两套矛盾的世界。
    registry.register(read_file_spec(project_root, pending, read_roots))
    registry.register(list_dir_spec(project_root, pending, read_roots))
    registry.register(search_code_spec(project_root, pending, read_roots))
    registry.register(dir_stats_spec(project_root, read_roots))
    registry.register(calc_spec())
    registry.register(request_diagnosis_spec(project_root))
    registry.register(read_diagnosis_spec(project_root))
    # 必须把 pending 传进去：否则子智能体改完代码再跑测试，测到的是**旧代码**
    # （改动还没落盘），它会以为自己的修复没生效，然后去改一个已经改对的函数。
    # trial_workspace 这个模块存在的全部理由就是这个，别在这一条路径上漏掉。
    registry.register(run_command_spec(project_root, pending))
    # 与主循环共用同一套判定，否则会出现「同一个项目里子智能体有一把
    # 主循环没有的工具」这种分叉，而它只会在跑偏时才暴露。
    register_edit_tools(registry, project_root, pending)
    attach_index(project_root, registry, OfflineTokenCounter(), pending)
    return registry


def _resolve_read_roots(args, project_root: Path) -> tuple[tuple[Path, ...], list[str]]:
    """把 --allow-read 解析成可读根，并剔掉不存在、或就在工作区内的。

    授权来自命令行，也就是来自用户——模型自己不能加目录。工作区内的路径
    本来就读得到，重复授权只会让提示词变长。
    """
    roots: list[Path] = []
    bad: list[str] = []
    for raw in getattr(args, "allow_read", []) or []:
        target = Path(raw).expanduser()
        if not target.exists() or not target.is_dir():
            bad.append(raw)
            continue
        resolved = target.resolve()
        if resolved == project_root or project_root in resolved.parents:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots), bad


def _maybe_file_diagnosis(
    project_root, before: int, result, writer: EventWriter | None = None
) -> str:
    """任务做不下去、而且确认过是环境问题时，替它把诊断请求登记下来。

    触发条件刻意收得很紧，三条同时成立才登记：

    1. **任务没做成**——自己想办法绕过去了就不必查环境；
    2. **这一步确认过是环境问题**（验证器给出的判定，不是猜的）；
    3. **它自己没登记过**——它主动申请过就不重复。

    为什么要循环替它做：实测模型在自己被卡住时会去改配置绕过，
    根本想不起来用这个通道。文字提醒在这台模型上反复被验证为无效，
    能推得动的只有「循环替它做」。
    """
    if result.finished or not getattr(result, "environment_blocked", False):
        return ""
    if len(diagnosis.load_requests(project_root)) > before:
        return ""

    recent = [line for line in result.trace if "自动验证" in line][-3:]
    request = diagnosis.store_request(
        project_root,
        question="任务因环境问题无法继续，请确认是不是环境本身坏了",
        hypothesis="本机的测试环境存在异常，与本次改动无关，需要真实环境权限才能查证",
        evidence="\n".join(recent) or "（没有留下更多线索）",
    )
    line = (
        f"已替你登记诊断请求 {request.id}：任务因环境问题无法继续。"
        "它需要由具备真实环境权限的会话验证，你可以继续或等报告。"
    )
    if writer is not None:
        writer.emit("tool", name="诊断登记", ok=True, detail=line)
    else:
        print(line)
    return request.id


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
