"""客户端指向：某个入口"现在指着哪儿"，即一对 (工作区, 会话)。

为什么需要它：四个入口要么每次让人敲 `--root`（记不住），要么在启动时钉死
（换不了）。指向把"上次选的那一对"记下来，作为解析的**一层兜底**——它永远排在
"当前目录向上找"后面（在项目子目录里敲命令，指定性明显强于"我上次在别处选过谁"）。

聊天通道按**每个聊天**分键（`chat:<通道>:<用户>`），所以两个群可以各自指着不同的
工作区，互不影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from spoolkit.kernel import jsonstore
from spoolkit.kernel.workspaces import state_dir

POINTERS = "clients.json"


@dataclass(frozen=True)
class Pointer:
    workspace: Path
    session: str


def pointers_path() -> Path:
    return state_dir() / POINTERS


def all_pointers() -> dict[str, Pointer]:
    """坏条目直接跳过：指向是便利，不是数据。"""
    payload = jsonstore.read_json(pointers_path(), {})
    raw = payload.get("clients") if isinstance(payload, dict) else None
    found: dict[str, Pointer] = {}
    for key, value in (raw or {}).items():
        if not isinstance(value, dict):
            continue
        text = str(value.get("workspace") or "").strip()
        if not text:
            continue
        try:
            workspace = Path(text).resolve()
        except (OSError, ValueError):
            continue
        found[str(key)] = Pointer(
            workspace=workspace, session=str(value.get("session") or "")
        )
    return found


def get(client_key: str) -> Pointer | None:
    return all_pointers().get(str(client_key))


def set(client_key: str, root: Path | str, session: str) -> None:
    """写指针。工作区存不存在由调用方先判（这里不做工作区解析）。"""
    root_text = str(Path(root).resolve())
    session_text = str(session)

    def mutate(payload: object) -> dict:
        data = payload if isinstance(payload, dict) else {}
        clients = dict(data.get("clients") or {})
        clients[str(client_key)] = {"workspace": root_text, "session": session_text}
        return {**data, "clients": clients}

    jsonstore.update_json(pointers_path(), mutate, {})


def clear(client_key: str) -> None:
    def mutate(payload: object) -> dict:
        data = payload if isinstance(payload, dict) else {}
        clients = dict(data.get("clients") or {})
        clients.pop(str(client_key), None)
        return {**data, "clients": clients}

    jsonstore.update_json(pointers_path(), mutate, {})
