"""`spool ws`：工作区管理。列、切、加、忘、起别名。

为什么要有它：工作区原先只能在启动参数里给（`--root <长路径>`），而且在别处就
只能靠"登记表里只有一个"碰运气。列出来 + 起个短名字之后，切换是一句话的事。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from spoolkit.cli.commands.init import init_command
from spoolkit.kernel import pointers, workspaces
from spoolkit.kernel.workspaces import WorkspaceError

CLIENT = "cli"


def ws_command(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "list") or "list")
    name = str(getattr(args, "name", "") or "").strip()
    try:
        if action == "list":
            return _list()
        if action == "use":
            return _use(name)
        if action == "add":
            # 建工作区这件事只能由人做（`.agent/` 里装着授权策略），
            # 这里直接复用 `spool init`，不另写一套。
            return init_command(argparse.Namespace(path=name or "."))
        if action == "forget":
            root = workspaces.forget(name)
            print(f"已从登记表摘掉：{root}（目录和 .agent/ 都没动）")
            return 0
        if action == "alias":
            alias = str(getattr(args, "value", "") or "").strip()
            root = workspaces.resolve_workspace(name)
            workspaces.set_alias(root, alias)
            print(f"{root} 现在也叫「{alias}」")
            return 0
    except WorkspaceError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"写状态文件失败：{exc}", file=sys.stderr)
        return 2
    print(f"不认识的用法：ws {action}", file=sys.stderr)
    return 2


def _current() -> Path | None:
    pointer = pointers.get(CLIENT)
    if pointer is not None:
        return pointer.workspace
    return workspaces.find_root()


def _list() -> int:
    items = workspaces.list_workspaces()
    if not items:
        print("还没有登记过任何工作区。在这儿敲 `spool ws add` 把当前目录做成工作区。")
        return 0
    here = _current()
    for index, item in enumerate(items, start=1):
        marks = []
        if here is not None and item.root == here:
            marks.append("当前")
        if item.alias:
            marks.append(f"别名 {item.alias}")
        tail = f"  ← {'、'.join(marks)}" if marks else ""
        print(f"  {index}) {item.name}  {item.root}{tail}")
    print("\n用 `spool ws use <名字|序号|路径>` 切；`spool ws alias <名字> <别名>` 起短名。")
    return 0


def _use(name: str) -> int:
    if not name:
        print("要切到哪个？例如 `spool ws use 2` 或 `spool ws use 我的项目`", file=sys.stderr)
        return 2
    root = workspaces.resolve_workspace(name)  # 找不到会抛 WorkspaceError
    session = ""
    existing = pointers.get(CLIENT)
    if existing is not None and existing.workspace == root:
        session = existing.session
    pointers.set(CLIENT, root, session)
    print(f"现在指向：{root}")
    if not session:
        print("（还没选会话；第一句话会开一条新的，或用 `spool session use` 挑一条）")
    return 0
