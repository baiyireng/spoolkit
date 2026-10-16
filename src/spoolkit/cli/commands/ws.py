"""`spool ws`: list / use / add / forget / alias workspaces."""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.markup import escape

from spoolkit.cli.commands.init import init_command
from spoolkit.cli.term import print_err, print_out
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
            return init_command(argparse.Namespace(path=name or "."))
        if action == "forget":
            root = workspaces.forget(name)
            print_out(
                f"removed from registry: {root} (directory unchanged)",
                markup=False,
            )
            return 0
        if action == "alias":
            alias = str(getattr(args, "value", "") or "").strip()
            root = workspaces.resolve_workspace(name)
            workspaces.set_alias(root, alias)
            print_out(f"{root}  alias = {alias}", markup=False, style="ok")
            return 0
    except WorkspaceError as exc:
        print_err(str(exc), markup=False)
        return 2
    except OSError as exc:
        print_err(f"failed to write state: {exc}", markup=False)
        return 2
    print_err(f"unknown: ws {action}", markup=False)
    return 2


def _current() -> Path | None:
    pointer = pointers.get(CLIENT)
    if pointer is not None:
        return pointer.workspace
    return workspaces.find_root()


def _list() -> int:
    items = workspaces.list_workspaces()
    if not items:
        print_out("No workspaces registered.")
        print_out("  [muted]spool ws add .[/]")
        return 0
    here = _current()
    name_width = max(len(item.name) for item in items)
    print_out("[muted]workspaces[/]")
    print_out("")
    for index, item in enumerate(items, start=1):
        marks: list[str] = []
        if here is not None and item.root == here:
            marks.append("[accent]current[/]")
        if item.alias:
            marks.append(f"alias:{escape(item.alias)}")
        tail = f"  ({', '.join(marks)})" if marks else ""
        name_col = f"{item.name:<{name_width}}"
        print_out(
            f"  {index:>2}  {escape(name_col)}  "
            f"{escape(str(item.root))}{tail}"
        )
    print_out("")
    print_out("[muted]  spool ws use <name|index|path>[/]")
    print_out("[muted]  spool ws alias <name> <alias>[/]")
    return 0


def _use(name: str) -> int:
    if not name:
        print_err("usage: spool ws use <name|index|path>", markup=False)
        return 2
    root = workspaces.resolve_workspace(name)
    session = ""
    existing = pointers.get(CLIENT)
    if existing is not None and existing.workspace == root:
        session = existing.session
    pointers.set(CLIENT, root, session)
    print_out(f"pointer → {root}", markup=False, style="ok")
    if not session:
        print_out(
            "(no session yet; first message creates one, or: spool session use …)",
            markup=False,
            style="muted",
        )
    return 0
