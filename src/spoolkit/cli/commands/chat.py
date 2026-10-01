"""对话式使用：一行一句，接着上一句说。

为什么要有它：`run --goal …` 是**一次性任务**式的——每次都要把话说全。
但真实使用是逐轮推进的：先让它查看，再让它改一处，再让它跑个测试。
这条命令把一个进程里的多次运行串起来，**共用同一个会话**（记忆、进度、
授权策略都延续）。每轮 `loop.run()` 仍重置工具往返；最近几轮往来（截断）
由 `prompt_for_turn` 拼进本轮提示，用来理解指代，不是把整段历史塞回去。
"""

import argparse
from pathlib import Path

from spoolkit.cli.commands.run import (
    _overrides,
    _report_numbers,
    _resolve_read_roots,
    _settle_pending,
)
from spoolkit.cli.options import resolve_policy, resolve_scope, resolve_window
from spoolkit.cli.runtime import (
    LoopWiring,
    assemble_loop,
    build_approver,
    build_lessons,
    distiller_for,
    open_memory,
    prompt_for_turn,
    show_history,
    settle_lessons,
    web_access,
    web_search_backend,
)
from spoolkit.config import Config
from spoolkit.kernel import pointers, sessions, workspaces
from spoolkit.kernel.target import resolve_target
from spoolkit.kernel.workspaces import WorkspaceError
from spoolkit.memory.transcript import ASSISTANT, USER, record_message
from spoolkit.tools.edit import PendingChanges

CLIENT = "cli"

EXIT_WORDS = ("/exit", "/quit", "/q")
HELP = """\
直接输入要做的事，例如：
  看看 calc.py 里 sum_to 是什么
  把它改成返回 1 到 n 的和

/history   查看本会话最近几轮（仅供人阅读，不入模型上下文）
/ws        列出登记过的工作区，`/ws use <名字>` 切换（下次进入 chat 生效）
/sessions  列出该工作区的会话，`/use <序号|id|latest>` 切换
/new [标题] 新建一条会话，本次对话写入其中
/config    查看当前供应商/模型/地址及其来源
/policy    查看授权策略（写操作如何放行）
/approve   处理聊天通道的配对请求：/approve <码>、--list、--revoke <用户>
/help      显示本页
/exit      退出
"""


def _status_line(
    args, project_root: Path, window: int, policy: str, scope, target=None
) -> str:
    """开局那一眼：现在到底连的是哪儿、拿什么策略在跑。

    带上内核给的**来源**（`target.workspace_source` / `session_source`）：光说
    "在哪个工作区"不够，人还要知道"为什么是它"——是显式给的、是向上找到的、
    还是上次指向的。指向的工作区不存在时也要说清楚，不能静默换地方（设计 §7）。
    """
    from spoolkit.cli.runtime import resolve_provider_args

    values = resolve_provider_args(args)
    # 地址只对本地服务有意义：假模型/Gemini 上显示一个 127.0.0.1:8080 是噪音。
    address = (
        f"，地址 {values['base_url']}"
        if values["provider"] == "llamacpp" and values["base_url"]
        else ""
    )
    model = values["model"] or "服务端默认"
    scope_text = "、".join(scope) if scope else "（无）"
    if target is not None:
        from spoolkit.kernel import sessions as ksession

        title = ksession.read_meta(project_root, target.session).get("title") or "（还没标题）"
        note = f"{target.note}\n" if getattr(target, "note", "") else ""
        head = (
            f"工作区：{project_root}（{target.workspace_source}）\n"
            f"会话：{target.session} ｜ {title}（{target.session_source}）\n"
            f"{note}"
        )
    else:
        head = f"工作区：{project_root}\n"
    return (
        f"{head}"
        f"供应商：{values['provider']}（{values['provider_source']}）{address}\n"
        f"模型：{model} ｜ 上下文窗口：{window}\n"
        f"授权：{policy} ｜ 自动落盘范围：{scope_text}"
    )


def _show_config() -> None:
    """复用 config 命令那张表：值 + 来源。"""
    from spoolkit import settings as settings_module
    from spoolkit.cli.commands.config import _render

    effective: dict[str, str] = {}
    for key in settings_module.KEYS:
        value, source = settings_module.resolve(key)
        effective[key] = value
        effective[f"{key}_source"] = source
    print(_render(effective, settings_module.config_path()))


def _show_pairings(project_root: Path, argument: str) -> None:
    """`/approve …`：把聊天通道的配对**在你人在的这个会话里**处理掉。

    原先配对码只出现在桥自己的终端上；你在另一个窗口跑 chat，什么都看不到。
    """
    from spoolkit.cli.commands.bridge import (
        approve_command,
        forget_command,
        list_pairings,
        revoke_command,
    )

    parts = argument.split()
    if not parts:
        print(
            "用法：/approve <码>（批准）、/approve --list（看全部）、"
            "/approve --revoke <用户>（解绑）、/approve --forget <码>（丢掉）"
        )
        return
    first = parts[0]
    if first == "--list":
        list_pairings(project_root)
    elif first == "--revoke" and len(parts) > 1:
        revoke_command(project_root, parts[1])
    elif first == "--forget" and len(parts) > 1:
        forget_command(project_root, parts[1])
    else:
        approve_command(project_root, first)


def _turn(args, project_root, gateway, window, memory) -> int:
    """跑一轮：和 `run` 的普通路径同构，只是不退出进程。"""
    overrides = _overrides(args, project_root)
    approver, grants = build_approver(project_root)
    read_roots, _ = _resolve_read_roots(args, project_root)
    pending = PendingChanges(project_root, extra_roots=read_roots)
    if memory is not None:
        record_message(memory._conn, args.session, USER, args.goal)
    prompt = prompt_for_turn(memory, args.session, args.goal, overrides)

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
            session=args.session,
        ),
        wiring=LoopWiring(
            memory=memory,
            distiller=distiller_for(args, gateway, memory),
            pending=pending,
            approver=approver,
            grants=grants,
            lessons=build_lessons(memory) if memory is not None else None,
            read_roots=read_roots,
            web=web_access(args, project_root),
            search=web_search_backend(args, project_root),
            mcp=not getattr(args, "no_mcp", False),
        ),
    )

    result = loop.run(args.goal, prompt=prompt)
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
    # 会话与工作区由内核定一次（显式 > 向上找 > 指针 > 登记表 > 当前目录）。
    # `chat` 的会话兜底名仍然是 `chat`：老会话、老进度都接得上。
    target = resolve_target(
        workspace=args.root, session=args.session, client="cli", session_fallback="chat"
    )
    args.root, args.session = str(target.root), target.session
    project_root = Path(args.root).resolve()
    from spoolkit.cli.runtime import bind_model_wait, provider_gateway

    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2
    bind_model_wait(gateway, args)
    window = resolve_window(gateway, args.window)
    model_name = f"{args.provider}:{args.model or '默认'}"
    memory = None
    if not args.no_memory:
        memory = open_memory(project_root, window, args.session, model=model_name)
        show_history(memory, args.session, args.history)
    policy = resolve_policy(args, project_root)
    scope = resolve_scope(args)
    print(_status_line(args, project_root, window, policy, scope, target))
    print("进入对话模式（/help 查看帮助，/exit 退出）。最近几轮截断后并入本轮提示；工具往返不重复计入。")

    # 正开着桥时，"有人要配对"原先只写在桥自己的终端上。这里补一条，
    # 让消息出现在你人正在的这个会话里（并且能就地批准）。
    from spoolkit.cli.commands.bridge import pending_notice

    notice = pending_notice(project_root)
    if notice:
        print(notice)

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
                print("（本次运行已关闭记忆，无历史可查）")
            continue
        if text == "/config":
            _show_config()
            continue
        if text == "/policy":
            print(f"当前授权策略：{policy}（范围 {('、'.join(scope) or '（无）')}）")
            print("修改本次会话：重启 chat 时加 --policy ask|auto|deny 与 --scope；")
            print("修改长期默认：spool policy --set auto")
            continue
        if text == "/approve" or text.startswith("/approve "):
            _show_pairings(project_root, text[len("/approve") :])
            continue
        if text in ("/ws", "/workspace"):
            print(_ws_listing(project_root))
            continue
        if text.startswith("/ws use "):
            print(_ws_switch(text[len("/ws use ") :].strip()))
            continue
        if text in ("/sessions", "/session"):
            print(_sessions_listing(project_root, args.session))
            continue
        if text.startswith("/use "):
            memory, shown = _switch_session(
                args, project_root, window, model_name, memory, text[len("/use ") :].strip()
            )
            print(shown)
            continue
        if text == "/new" or text.startswith("/new "):
            title = text[len("/new") :].strip()
            memory, shown = _switch_session(
                args,
                project_root,
                window,
                model_name,
                memory,
                None,
                new_title=title,
            )
            print(shown)
            continue
        args.goal = text
        _turn(args, project_root, gateway, window, memory)
    return 0


# --- 对话里的工作区与会话切换（Task 5）-------------------------------------------
#
# 这几条只动**指针与本次对话的会话**：`/ws use` 不热切工作区（记忆库、索引、
# 授权范围、关联目录全挂在工作区上，一次对话中途换根会让下半程读到另一份状态），
# 它只把指针挪过去，下一次进 chat 就在新工作区。


def _ws_listing(project_root: Path) -> str:
    here = pointers.get(CLIENT)
    current = here.workspace if here is not None else project_root
    lines = ["登记过的工作区："]
    for index, item in enumerate(workspaces.list_workspaces(), start=1):
        mark = "  ← 当前" if item.root == current else ""
        alias = f"（别名 {item.alias}）" if item.alias else ""
        lines.append(f"  {index}) {item.name}{alias}  {item.root}{mark}")
    lines.append("用 `/ws use <名字|序号|路径>` 切换（下次进入 chat 时生效）。")
    return "\n".join(lines)


def _ws_switch(name: str) -> str:
    if not name:
        return "需要指定目标，例如 `/ws use 2` 或 `/ws use 我的项目`"
    try:
        root = workspaces.resolve_workspace(name)
    except WorkspaceError as exc:
        return str(exc)
    session = ""
    existing = pointers.get(CLIENT)
    if existing is not None and existing.workspace == root:
        session = existing.session
    pointers.set(CLIENT, root, session)
    return (
        f"指针现在指向：{root}\n"
        "本次对话仍在原工作区内运行；下次进入 chat 时使用新工作区，"
        "会话用 `spool session use` 指定或由第一条消息新建。"
    )


def _sessions_listing(project_root: Path, current: str) -> str:
    everything = sessions.list_sessions(project_root, include_archived=True)
    if not everything:
        return "这个工作区还没有任何会话。用 `/new <标题>` 新建。"
    lines = ["这个工作区的会话（序号与 `/use <序号>` 一致）："]
    for index, item in enumerate(everything, start=1):
        marks = []
        if item.id == current:
            marks.append("当前")
        if item.archived:
            marks.append("已归档")
        tail = f"  ← {'、'.join(marks)}" if marks else ""
        lines.append(f"  {index}) {item.id}  {item.title or '（还没标题）'}{tail}")
    lines.append("用 `/use <序号|id|latest>` 切换，`/new [标题]` 新建。")
    return "\n".join(lines)


def _switch_session(
    args,
    project_root: Path,
    window: int,
    model_name: str,
    memory,
    name: str | None,
    new_title: str = "",
):
    """切（或新开）一条会话，并让**这次对话**接着往它写。

    `MemorySession` 的会话 id 是构造时定下的，所以切换必须重建一份——只改
    `args.session` 会让记忆还写进老会话，症状是"切了，但历史里没有"。
    """
    if name is None:
        picked = sessions.create_session(project_root, title=new_title)
    else:
        try:
            picked = sessions.resolve_session(project_root, name)
        except sessions.SessionError as exc:
            return memory, str(exc)
    pointers.set(CLIENT, project_root, picked)
    args.session = picked
    if not args.no_memory:
        if memory is not None:
            conn = getattr(memory, "_conn", None)
            if conn is not None:
                conn.close()
        memory = open_memory(project_root, window, picked, model=model_name)
    title = sessions.read_meta(project_root, picked).get("title") or "（还没标题）"
    return memory, f"现在用会话 {picked}（{title}）"
