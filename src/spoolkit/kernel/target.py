"""唯一的解析入口：这次到底在哪个工作区、哪条会话里干活。

顺序（见设计文档 3.4 / 3.5）：

  工作区：显式 --root > 当前目录向上找 .agent/ > 客户端指向 > 登记表 > 当前目录
  会话：  显式 --session > 客户端指向（仅当指向的是同一个工作区）> 入口的默认名

"向上找"排在指针前面是有意的：在项目子目录里敲命令，指定性比"我上次在别处选过谁"
强得多。登记表里有多个又没人可问时**报错而不是猜**——猜错的症状是"这个命令的记忆
跟别的命令不是同一份"，那是最难查的一类问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from spoolkit.kernel import pointers, workspaces
from spoolkit.kernel.workspaces import WorkspaceError


@dataclass(frozen=True)
class Target:
    """这次要干活的地方。`*_source` 是给"开局那一眼"交代来源用的。"""

    root: Path
    session: str
    workspace_source: str  # explicit / cwd / pointer / registry / cwd-fallback
    session_source: str    # explicit / pointer / default


def resolve_target(
    *,
    workspace: str | Path | None = None,
    session: str | None = None,
    client: str = "cli",
    session_fallback: str = "cli",
    on_ambiguous: Callable[[list[Path]], Path] | None = None,
) -> Target:
    root, workspace_source = _resolve_workspace(workspace, client, on_ambiguous)
    name, session_source = _resolve_session(session, client, root, session_fallback)
    return Target(
        root=root,
        session=name,
        workspace_source=workspace_source,
        session_source=session_source,
    )


def _resolve_workspace(
    explicit: str | Path | None,
    client: str,
    on_ambiguous: Callable[[list[Path]], Path] | None,
) -> tuple[Path, str]:
    if explicit not in (None, ""):
        root = Path(str(explicit)).resolve()
        workspaces.register(root)  # 显式给的值不写指针，但登记表照旧更新
        return root, "explicit"

    found = workspaces.find_root()
    if found is not None:
        workspaces.register(found)
        return found, "cwd"

    pointer = pointers.get(client)
    if pointer is not None and (pointer.workspace / workspaces.MARK).is_dir():
        workspaces.register(pointer.workspace)
        return pointer.workspace, "pointer"

    candidates = workspaces.known()
    if len(candidates) == 1:
        return candidates[0], "registry"
    if len(candidates) > 1:
        if on_ambiguous is not None:
            picked = on_ambiguous(candidates)
            workspaces.register(picked)
            return picked, "registry"
        listed = "\n".join(f"  {item}" for item in candidates)
        raise WorkspaceError(
            "当前目录不是工作区，而登记表里有好几个，说不清是哪一个：\n"
            f"{listed}\n"
            "用 `--root <路径>` 指定一个。"
        )
    return Path.cwd().resolve(), "cwd-fallback"


def _resolve_session(
    explicit: str | None,
    client: str,
    root: Path,
    fallback: str,
) -> tuple[str, str]:
    if explicit not in (None, ""):
        return str(explicit), "explicit"
    pointer = pointers.get(client)
    # 会话属于工作区：指向的工作区跟这次要干的不是同一个时，那个会话名不能搬过来。
    if pointer is not None and pointer.session and pointer.workspace == root:
        return pointer.session, "pointer"
    return fallback, "default"
