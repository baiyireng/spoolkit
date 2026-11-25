"""工作区：从哪来、怎么找、记在哪。

**这个文件现在是一层壳。** 实现在 `spoolkit.kernel.workspaces`：那里多了命名
（目录名、别名、序号）与客户端指向，四个入口都要用。这里保留同名导出，是因为
外部与测试一直按 `workspace.resolve_root` / `workspace.MARK` 这些路径导入——
改名那次已经吃过一次"旧导入路径断掉"的亏，不再吃第二次。

`resolve_root` 仍然住在这里，因为它的签名（`choose` / `interactive`）是给"人在
终端前、登记表里有好几个"那个交互场景的，纯内核不该知道终端怎么问人。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Iterable

from spoolkit.kernel.workspaces import (
    ALIASES,
    MARK,
    REGISTRY,
    STATE_ENV,
    Workspace,
    WorkspaceError,
    aliases_path,
    find_root,
    forget,
    known,
    list_workspaces,
    load_aliases,
    register,
    registry_path,
    resolve_workspace,
    set_alias,
    state_dir,
)

__all__ = [
    "ALIASES",
    "MARK",
    "REGISTRY",
    "STATE_ENV",
    "Workspace",
    "WorkspaceError",
    "aliases_path",
    "find_root",
    "forget",
    "known",
    "list_workspaces",
    "load_aliases",
    "register",
    "registry_path",
    "resolve_root",
    "resolve_root_interactive",
    "resolve_workspace",
    "set_alias",
    "state_dir",
]


def resolve_root(
    explicit: str | Path | None = None,
    *,
    choose: Callable[[list[Path]], Path] | None = None,
    interactive: Callable[[], bool] | None = None,
) -> Path:
    """老签名、老行为。多出来的只是"指针"这一层兜底。"""
    from spoolkit.kernel.target import resolve_target

    on_ambiguous: Callable[[list[Path]], Path] | None = None
    if choose is not None or interactive is not None:

        def on_ambiguous(candidates: list[Path]) -> Path:
            asks = interactive or _interactive
            if not asks():
                return _ambiguous_error(candidates)
            return (choose or _ask_which)(candidates)

    return resolve_target(
        workspace=explicit, client="cli", on_ambiguous=on_ambiguous
    ).root


def resolve_root_interactive(explicit: str | Path | None = None) -> Path:
    """**CLI 专用**：说不清是哪一个时，人在终端前就问，问不了就照老文案报错。

    为什么要单独一个函数：内核只认"调用方注入的 chooser"，它不认识终端
    （设计 3.x：交互属于入口面）。而 CLI 是唯一确定"人就在终端前"的入口，
    所以由它把 chooser 注入进来。少了这一步，登记表里有两个工作区时，
    终端就从"弹选单"变成"报错退 2"——那是一次真回归。
    """
    return resolve_root(explicit, choose=_ask_which, interactive=_interactive)


def _ambiguous_error(candidates: list[Path]) -> Path:
    """说不清是哪一个。文案一字不改：tests/test_workspace_root.py 钉着它。"""
    listed = "\n".join(f"  {item}" for item in candidates)
    raise WorkspaceError(
        "当前目录不是工作区，而登记表里有好几个，说不清是哪一个：\n"
        f"{listed}\n"
        "用 `--root <路径>` 指定一个。"
    )


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ValueError, AttributeError):  # pragma: no cover - 少数被包装过的流
        return False


def _ask_which(candidates: Iterable[Path]) -> Path:
    items = list(candidates)
    print("当前目录不是工作区，登记表里有这几个：")
    for index, item in enumerate(items, start=1):
        print(f"  {index}) {item}")
    try:
        raw = input("选哪个 [1]：").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        # 交给 CLI `main` 统一安静退 130；这里只保证提示行后换行。
        print()
        raise
    try:
        return items[int(raw) - 1]
    except (ValueError, IndexError):
        return items[0]
