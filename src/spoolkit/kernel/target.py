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
    note: str = ""         # 需要交代给用户的一句话（例如"上次指向的 X 不在了"）


def resolve_target(
    *,
    workspace: str | Path | None = None,
    session: str | None = None,
    client: str = "cli",
    session_fallback: str = "cli",
    on_ambiguous: Callable[[list[Path]], Path] | None = None,
    use_cwd: bool = True,
) -> Target:
    """`use_cwd=False`：这条入口**不是"在某个目录里干活"**那种（聊天通道就是），
    于是"当前目录向上找"这一层不参与——桥是长驻进程，它的 cwd 只是启动方式的
    副产品，跟"手机那边这条聊天想在哪干活"毫无关系。此时工作区只能来自
    显式参数、这条聊天的指针，或登记表；都没有就报错，不猜。
    """
    root, workspace_source, note = _resolve_workspace(
        workspace, client, on_ambiguous, use_cwd
    )
    name, session_source = _resolve_session(session, client, root, session_fallback)
    return Target(
        root=root,
        session=name,
        workspace_source=workspace_source,
        session_source=session_source,
        note=note,
    )


def _resolve_workspace(
    explicit: str | Path | None,
    client: str,
    on_ambiguous: Callable[[list[Path]], Path] | None,
    use_cwd: bool = True,
) -> tuple[Path, str, str]:
    if explicit not in (None, ""):
        root = Path(str(explicit)).resolve()
        workspaces.register(root)  # 显式给的值不写指针，但登记表照旧更新
        return root, "explicit", ""

    if use_cwd:
        found = workspaces.find_root()
        if found is not None:
            workspaces.register(found)
            return found, "cwd", ""

    pointer = pointers.get(client)
    note = ""
    if pointer is not None:
        if (pointer.workspace / workspaces.MARK).is_dir():
            workspaces.register(pointer.workspace)
            return pointer.workspace, "pointer", ""
        # 指针指向的工作区不在了：**跳过它、继续往下退，但要说出来**。
        # 静默换地方的症状是"我明明指着 A，它却在 B 里干活"，属于最难查的那类。
        note = f"上次指向的 {pointer.workspace} 不在了（没有 .agent/），已跳过。"

    candidates = workspaces.known()
    if len(candidates) == 1:
        return candidates[0], "registry", note
    if len(candidates) > 1:
        if on_ambiguous is not None:
            picked = on_ambiguous(candidates)
            workspaces.register(picked)
            return picked, "registry", note
        listed = "\n".join(f"  {item}" for item in candidates)
        raise WorkspaceError(
            "当前目录不是工作区，而登记表里有好几个，说不清是哪一个：\n"
            f"{listed}\n"
            "用 `--root <路径>` 指定一个。"
        )
    if not use_cwd:
        # 不看 cwd 的入口：没有可指的工作区时**报错**，别拿启动目录凑数。
        raise WorkspaceError(
            "还没有工作区指向这里。让你的主人登记一个（`spool ws add <目录>`），"
            "或发 `/ws use <名字>` 挑一个。"
        )
    return Path.cwd().resolve(), "cwd-fallback", note


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
