"""聊天通道里的斜杠命令。

为什么要有这一层：**聊天是唯一的交互面**时，有些事得能在聊天里做，而不是让
用户回去开终端。

规矩：

- **只有被放行的人能用**。命令处理放在准入判定**之后**，陌生人连 `/help` 都
  不该拿到——否则一个公网通道就有了一个免鉴权的操作面。
- **命令只做"管理"，不做"干活"**：看现状、说明边界，不替模型做决定。
- **命令只改"这条通道自己的状态"**（指向、会话），**不碰工作区里的内容**：
  授权、关联目录、回滚仍然是人的动作，聊天里不开放。
- **聊天里加不了关联目录。** 要碰工作区外的文件，只能由主人在服务器上
  `spool extra add`。小模型分不清危险操作，远程通道不开放授权口。
"""

from __future__ import annotations

from pathlib import Path

from spoolkit import readroots

HELP = """\
直接描述要做什么，例如：
  看看 calc.py 里 sum_to 是什么
  把它改成长度对半切

这条通道只能读写当前工作区。
其它盘、桌面、下载目录、未指明位置的文件都在范围外；
查资料、跑命令不受这条限制。
放开某个目录只能由主人在服务器上执行 spool extra add <目录>；聊天里不开放。

命令（每条单独一行）：
/status
  当前指向的工作区与会话
/ws
  列出登记过的工作区
/ws use 名字或序号
  切换工作区（无需重新配对）
/sessions
  列出该工作区的会话
/use 序号或id或latest
  切换到另一条会话
/new 标题
  新建一条会话
/allow-read
  说明文件边界与已关联目录
/help
  显示本页

agent 有改动待确认时：回 y 应用、n 丢弃。
配对请求在本机处理：任意目录执行 spool approve <码>，或 spool chat 里 /approve
（这条通道上不开放授权口）。
"""


def workspace_scope_notice(root: Path | str) -> str:
    """远程通道的文件边界。给人看的斜杠命令用；不进模型上下文，也不主动推给用户。"""
    here = Path(root).resolve()
    extras = readroots.existing(root)
    lines = [
        f"这是远程聊天通道：只能读写当前工作区（{here}）。"
    ]
    if extras:
        listed = "、".join(str(item) for item in extras)
        lines.append(
            f"主人在服务器上关联了：{listed}。"
            "这些目录可读；写入仍需确认。"
        )
    else:
        lines.append(
            "其它盘、桌面、下载目录与未指明位置的文件都在范围外。"
        )
    lines.append(
        "查资料、跑命令不受这条限制：有工具就调用，没有就如实说明，"
        "不要表述成一律禁止。"
    )
    lines.append(
        "聊天里不开放新的目录（远程通道上小模型分不清危险操作）；"
        "要关联目录用服务器上的 `spool extra add <目录>`。"
    )
    return "\n".join(lines)


def handle(
    text: str,
    target,
    *,
    client_key: str = "",
    sessions_mod=None,
    workspaces_mod=None,
) -> str | None:
    """是命令就返回答复；不是命令返回 None（交给 agent）。

    `target` 是内核解析出来的 (工作区, 会话)；`client_key` 是"这条聊天"的键
    （`chat:<通道>:<用户>`）——`/ws use`、`/use`、`/new` 要写指针，必须知道是谁。
    """
    if not text.startswith("/"):
        return None
    root = Path(target.root)
    parts = text.split()
    name = parts[0].lower()

    if name in ("/help", "/?"):
        return HELP
    if name == "/allow-read":
        return workspace_scope_notice(root)
    if name == "/status":
        return _status(target)

    sessions = sessions_mod if sessions_mod is not None else _sessions()
    workspaces = workspaces_mod if workspaces_mod is not None else _workspaces()

    if name == "/ws":
        if len(parts) == 1:
            return _ws_list(target, workspaces)
        if parts[1].lower() == "use":
            if len(parts) < 3:
                return "用法：/ws use <名字|序号|路径>"
            return _ws_use(target, " ".join(parts[2:]), client_key, workspaces)
        return "用法：/ws（看有哪些）或 /ws use <名字|序号|路径>"
    if name == "/sessions":
        return _sessions_listing(target, sessions)
    if name == "/use":
        if len(parts) < 2:
            return "用法：/use <序号|id|latest>"
        return _use(target, " ".join(parts[1:]), client_key, sessions)
    if name == "/new":
        return _new(target, " ".join(parts[1:]), client_key, sessions)
    return f"不认识的命令：{parts[0]}（/help 查看全部）"


def _sessions():
    from spoolkit.kernel import sessions as module

    return module


def _workspaces():
    from spoolkit.kernel import workspaces as module

    return module


def _no_client() -> str:
    return "这条通道没有聊天身份，无法切换工作区/会话；请看桥的日志。"


def _status(target) -> str:
    from spoolkit import workspace

    root = Path(target.root).resolve()
    lines = [workspace_scope_notice(root), "", f"工作区：{root}"]
    session = str(getattr(target, "session", "") or "")
    if session:
        lines.append(f"会话：{session}")
    note = str(getattr(target, "note", "") or "")
    if note:
        lines.append(note)
    lines.append(readroots.render(root))
    lines.append(f"配对文件：{root / '.agent' / 'bridge-pairings.json'}")
    lines.append(f"工作区登记表：{workspace.registry_path()}")
    return "\n".join(lines)


def _ws_list(target, workspaces) -> str:
    items = workspaces.list_workspaces()
    if not items:
        return "主人尚未登记任何工作区。请在服务器上执行 `spool ws add <目录>`。"
    here = Path(target.root).resolve()
    lines = ["登记过的工作区："]
    for index, item in enumerate(items, start=1):
        mark = "  ← 现在" if item.root == here else ""
        alias = f"（别名 {item.alias}）" if item.alias else ""
        lines.append(f"  {index}) {item.name}{alias}{mark}")
        lines.append(f"      {item.root}")
    lines.append("用 `/ws use <名字|序号>` 切换（无需重新配对）。")
    return "\n".join(lines)


def _ws_use(target, name: str, client_key: str, workspaces) -> str:
    if not client_key:
        return _no_client()
    from spoolkit.kernel import pointers

    try:
        root = workspaces.resolve_workspace(name)
    except workspaces.WorkspaceError as exc:
        return str(exc)
    pointer = pointers.get(client_key)
    # 换了工作区就换一整套会话：会话属于工作区，旧会话名不能搬过去。
    session = ""
    if pointer is not None and pointer.workspace == root:
        session = pointer.session
    pointers.set(client_key, root, session)
    return (
        f"现在指向：{root}\n"
        "下一条消息在此工作区执行；会话 `/use` 选择，或直接描述任务。"
    )


def _sessions_listing(target, sessions) -> str:
    root = Path(target.root)
    items = sessions.list_sessions(root, include_archived=True)
    if not items:
        return "这个工作区还没有会话。用 `/new <标题>` 新建。"
    current = str(getattr(target, "session", "") or "")
    lines = [f"{root} 的会话："]
    for index, item in enumerate(items, start=1):
        marks = []
        if item.id == current:
            marks.append("现在")
        if item.archived:
            marks.append("已归档")
        tail = f"  ← {'、'.join(marks)}" if marks else ""
        lines.append(f"  {index}) {item.id}  {item.title or '（还没标题）'}{tail}")
    lines.append("用 `/use <序号|id|latest>` 切换，`/new [标题]` 新建。")
    return "\n".join(lines)


def _use(target, name: str, client_key: str, sessions) -> str:
    if not client_key:
        return _no_client()
    from spoolkit.kernel import pointers

    root = Path(target.root)
    try:
        picked = sessions.resolve_session(root, name)
    except sessions.SessionError as exc:
        return str(exc)
    pointers.set(client_key, root, picked)
    title = sessions.read_meta(root, picked).get("title") or "（还没标题）"
    return f"现在用会话 {picked}（{title}）"


def _new(target, title: str, client_key: str, sessions) -> str:
    if not client_key:
        return _no_client()
    from spoolkit.kernel import pointers

    root = Path(target.root)
    made = sessions.create_session(root, title=title)
    pointers.set(client_key, root, made)
    return f"新会话：{made}" + (f"（{title}）" if title else "（还没标题）")
