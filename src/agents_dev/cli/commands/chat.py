"""对话式使用：一行一句，接着上一句说。

为什么要有它：`run --goal …` 是**一次性任务**式的——每次都要把话说全。
但真实使用更像聊天：先让它看一眼，再让它改一处，再让它跑个测试。
这条命令把一个进程里的多次运行串起来，**共用同一个会话**（记忆、进度、
授权策略都延续），而上下文仍然按原设计：每轮独立，不把历史塞回模型。

也就是说：**聊天区域有历史（给人看），模型上下文没有历史（给短上下文让路）**。
这两件事混为一谈会得出"把历史塞回去"的结论，而那正是这个项目要避免的。
"""

import argparse
from pathlib import Path

from agents_dev.cli.commands.run import (
    _overrides,
    _report_numbers,
    _resolve_read_roots,
    _settle_pending,
)
from agents_dev.cli.options import resolve_policy, resolve_scope, resolve_window
from agents_dev.cli.runtime import (
    LoopWiring,
    assemble_loop,
    build_approver,
    build_lessons,
    open_memory,
    show_history,
    settle_lessons,
)
from agents_dev.config import Config
from agents_dev.memory.distill import distill
from agents_dev.memory.transcript import ASSISTANT, USER, record_message
from agents_dev.tools.edit import PendingChanges

EXIT_WORDS = ("/exit", "/quit", "/q")
HELP = """\
直接输入要做的事就行，例如：
  看看 calc.py 里 sum_to 是什么
  把它改成返回 1 到 n 的和

/history   看这个会话最近几轮（给人看的，不进入模型上下文）
/config    看当前用的供应商/模型/地址，以及它们从哪来
/policy    看授权策略（写操作怎么放行）
/help      这一页
/exit      退出
"""


def _status_line(args, project_root: Path, window: int, policy: str, scope) -> str:
    """开局那一眼：现在到底连的是哪儿、拿什么策略在跑。"""
    from agents_dev.cli.runtime import resolve_provider_args

    values = resolve_provider_args(args)
    # 地址只对本地服务有意义：假模型/Gemini 上显示一个 127.0.0.1:8080 是噪音。
    address = (
        f"，地址 {values['base_url']}"
        if values["provider"] == "llamacpp" and values["base_url"]
        else ""
    )
    model = values["model"] or "服务端默认"
    scope_text = "、".join(scope) if scope else "（无）"
    return (
        f"工作区：{project_root}\n"
        f"供应商：{values['provider']}（{values['provider_source']}）{address}\n"
        f"模型：{model} ｜ 上下文窗口：{window}\n"
        f"授权：{policy} ｜ 自动落盘范围：{scope_text}"
    )


def _show_config() -> None:
    """复用 config 命令那张表：值 + 来源。"""
    from agents_dev import settings as settings_module
    from agents_dev.cli.commands.config import _render

    effective: dict[str, str] = {}
    for key in settings_module.KEYS:
        value, source = settings_module.resolve(key)
        effective[key] = value
        effective[f"{key}_source"] = source
    print(_render(effective, settings_module.config_path()))


def _turn(args, project_root, gateway, window, memory) -> int:
    """跑一轮：和 `run` 的普通路径同构，只是不退出进程。"""
    overrides = _overrides(args, project_root)
    approver, grants = build_approver(project_root)
    pending = PendingChanges(project_root)
    read_roots, _ = _resolve_read_roots(args, project_root)
    distiller = None
    if memory is not None and args.provider != "fake":
        distiller = lambda state, final: distill(gateway, state, final=final)

    loop = assemble_loop(
        project_root,
        gateway,
        config=Config(
            project_root=project_root,
            context_window=window,
            max_steps=args.max_steps,
            subagent_steps=args.subagent_steps,
            supervise=not args.no_supervise,
            step_ceiling=args.step_ceiling,
            overrides=overrides,
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
    if memory is not None:
        record_message(memory._conn, args.session, USER, args.goal)

    result = loop.run(args.goal)
    _report_numbers(loop, result)
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
    print(result.final)
    _settle_pending(args, project_root, pending)
    return 0 if result.finished else 1


def chat_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).resolve()
    from agents_dev.cli.runtime import provider_gateway

    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2
    window = resolve_window(gateway, args.window)
    memory = None
    if not args.no_memory:
        memory = open_memory(
            project_root,
            window,
            args.session,
            model=f"{args.provider}:{args.model or '默认'}",
        )
        show_history(memory, args.session, args.history)
    policy = resolve_policy(args, project_root)
    scope = resolve_scope(args)
    print(_status_line(args, project_root, window, policy, scope))
    print("进入对话模式（/help 看提示，/exit 退出）。每轮独立：历史不塞回模型。")

    # reader 只在测试里给：直接喂几行进去，测"多轮 + /exit"这条链。
    reader = getattr(args, "reader", None)
    while True:
        try:
            line = reader() if reader is not None else input("› ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        text = line.strip()
        if not text:
            continue
        if text in EXIT_WORDS:
            break
        if text == "/help":
            print(HELP)
            continue
        if text == "/history":
            if memory is not None:
                show_history(memory, args.session, args.history)
            else:
                print("（这次运行关了记忆，没有历史可看）")
            continue
        if text == "/config":
            _show_config()
            continue
        if text == "/policy":
            print(f"当前授权策略：{policy}（范围 {('、'.join(scope) or '（无）')}）")
            print("改当前会话：重启 chat 时加 --policy ask|auto|deny 与 --scope；")
            print("改长期默认：agents-dev policy --set auto")
            continue
        args.goal = text
        _turn(args, project_root, gateway, window, memory)
    return 0
