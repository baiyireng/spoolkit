"""会话管理命令。

建了会话却管不了它，等于没有会话概念——你甚至不知道自己建过哪些。

这一版把会话做成**有生命周期**的：`list` / `new` / `use` / `rename` / `archive`。
列出来的那个序号就是 `use <序号>` 认的序号——两处都按同一份完整列表（含已归档）
编号，所以照着自己看到的数字敲回去一定落在同一条上。归档的条目在默认列表里
不显示，但仍然可以直接用 id 进去（设计文档 §3.3 / §4.1）。
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from spoolkit.kernel import pointers, sessions

CLIENT = "cli"


def _stamp(value: float | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value).strftime("%m-%d %H:%M")


def _current_session(root: Path) -> str:
    """指针里那条**属于这个工作区**的会话（会话不能跨工作区搬）。"""
    pointer = pointers.get(CLIENT)
    if pointer is None or pointer.workspace != root:
        return ""
    return pointer.session


def session_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    action = str(getattr(args, "action", "list") or "list")
    name = str(getattr(args, "name", "") or "").strip()
    title = str(getattr(args, "title", "") or "").strip()

    if action == "new":
        # `spool session new <标题>`：`new` 后面那一个位置参数**就是标题**（设计
        # §5.1 的 `new [标题]`）。argparse 的槽位名叫 `name`，于是这里取
        # `title or name`——两种写法都认，免得"建出来的会话没有标题"。
        made = sessions.create_session(root, title=title or name)
        pointers.set(CLIENT, root, made)
        heading = title or name
        print(f"新会话：{made}" + (f"（{heading}）" if heading else ""))
        return 0
    if action == "use":
        try:
            picked = sessions.resolve_session(root, name)
        except sessions.SessionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        pointers.set(CLIENT, root, picked)
        print(f"现在用会话：{picked}")
        return 0
    if action == "rename":
        try:
            sessions.rename_session(root, name, title)
        except sessions.SessionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"已改名：{name} → {title}")
        return 0
    if action == "archive":
        try:
            sessions.archive_session(root, name)
        except sessions.SessionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("已归档（数据还在，只是默认不列出；`spool session --all` 看得到）")
        return 0
    return _list(root, include_archived=bool(getattr(args, "all", False)))


def _list(root: Path, include_archived: bool = False) -> int:
    # 编号取**完整**列表（含已归档），与 `resolve_session` 是同一个空间：
    # 默认只打印没归档的那几行，于是序号会出现空档——那是在如实说明
    # "中间还有几条被藏起来了"，比"看得见的第 3 条却是另一条"要好。
    everything = sessions.list_sessions(root, include_archived=True)
    if not everything:
        if not (root / ".agent" / "memory.db").exists():
            print("这个工作区还没有任何会话。", file=sys.stderr)
            return 2
        print("还没有会话记录。", file=sys.stderr)
        return 2

    here = _current_session(root)
    print(f"{'会话':<18}{'标题':<24}{'模型':<20}{'窗口':>7}{'最近活跃':>14}")
    shown = 0
    for index, item in enumerate(everything, start=1):
        if item.archived and not include_archived:
            continue
        shown += 1
        marks = []
        if item.id == here:
            marks.append("当前")
        if item.archived:
            marks.append("已归档")
        tail = f"  ← {'、'.join(marks)}" if marks else ""
        print(
            f"{index:>3}) {item.id:<14}{(item.title or '—'):<24}"
            f"{(item.model or '—'):<20}{item.context_limit:>7}"
            f"{_stamp(item.last_active):>14}{tail}"
        )

    print()
    if shown == 0:
        print(f"共 {len(everything)} 个会话，都归档了。`spool session --all` 看得到。")
    elif include_archived:
        print(f"共 {len(everything)} 个会话（含已归档）。用 `spool session use <序号|id>` 切。")
    else:
        print(f"共 {shown} 个会话。用 `spool session use <序号|id|latest>` 切。")
    return 0
