"""工作区：从哪来、怎么找、记在哪。

为什么要有它：`.agent/` 里装着这个项目的记忆、索引、检查点与策略，所有命令都得
先知道"说的是哪个工作区"。原先每条子命令各自把 `--root` 默认成 `"."`，于是

- 在工作区的**子目录**里敲命令，它就当成另一个工作区（记忆、索引全对不上）；
- 在别的地方敲 `spool approve <码>`，它去翻那个目录的配对文件，报"没有这个配对码"
  ——**不是码错了，是找错了工作区**，而这句话完全指不到病根。

所以规则收在一处（本模块），`main()` 只调用一次：

1. `--root` 显式给了 → 用它（指定就是指定，不去改它）；
2. 否则从当前目录**往上找** `.agent/` → 找到就用那一层；
3. 否则查全局登记表：只有一个就用它；多个且人在终端前，列出来让他选；
4. 都没有 → 当前目录（跟今天一样，交给具体命令去报它自己的错）。

登记表落在用户级状态目录（`SPOOLKIT_STATE` 可覆盖，测试与多套状态用），
每次"在工作区里干活"都会把它挪到最前面，所以它同时是**最近使用**的顺序。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable, Iterable

MARK = ".agent"
REGISTRY = "workspaces.json"
STATE_ENV = "SPOOLKIT_STATE"


class WorkspaceError(RuntimeError):
    """工作区找不到、或者有好几个说不清是哪个。"""


def state_dir() -> Path:
    """这台机器上放用户级状态的地方（登记表、诊断密钥的邻居）。"""
    override = os.environ.get(STATE_ENV)
    if override:
        return Path(override)
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "spoolkit"


def registry_path() -> Path:
    return state_dir() / REGISTRY


def find_root(start: Path | None = None) -> Path | None:
    """从 `start`（默认当前目录）往上找带 `.agent/` 的那一层。"""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / MARK).is_dir():
            return candidate
    return None


def known() -> list[Path]:
    """登记表里的工作区（只留**现在还在**的，顺序是最近使用在前）。"""
    path = registry_path()
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = payload.get("workspaces") if isinstance(payload, dict) else payload
    found: list[Path] = []
    for item in items or []:
        try:
            candidate = Path(str(item)).resolve()
        except (OSError, ValueError):
            continue
        if candidate.is_dir() and candidate not in found:
            found.append(candidate)
    return found


def register(root: Path | str) -> None:
    """把工作区登记下来（幂等）。登记表坏了就当空的——它是便利，不是数据。"""
    target = Path(root).resolve()
    order = [item for item in known() if item != target]
    order.insert(0, target)
    path = registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"workspaces": [str(item) for item in order]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        # 登记不上不该挡住干活：最坏是下次还得写 --root。
        pass


def resolve_root(
    explicit: str | Path | None = None,
    *,
    choose: Callable[[list[Path]], Path] | None = None,
    interactive: Callable[[], bool] | None = None,
) -> Path:
    """按模块开头那四条规则定出工作区。"""
    if explicit not in (None, ""):
        return Path(str(explicit)).resolve()

    found = find_root()
    if found is not None:
        # 顺手登记：登记表因此天然是"最近用过哪些工作区"的顺序。
        register(found)
        return found

    candidates = known()
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        chooser = choose or _ask_which
        is_interactive = interactive or _interactive
        if is_interactive():
            picked = chooser(candidates)
            register(picked)
            return picked
        listed = "\n".join(f"  {item}" for item in candidates)
        raise WorkspaceError(
            "当前目录不是工作区，而登记表里有好几个，说不清是哪一个：\n"
            f"{listed}\n"
            "用 `--root <路径>` 指定一个。"
        )
    # 一个都没有：保持老行为（当前目录），让具体命令去报它自己的错。
    return Path.cwd().resolve()


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ValueError, AttributeError):  # pragma: no cover - 少数被包装过的流
        return False


def _ask_which(candidates: Iterable[Path]) -> Path:
    """列出来让人选。默认第一个（最近用过的那个）。"""
    items = list(candidates)
    print("当前目录不是工作区，登记表里有这几个：")
    for index, item in enumerate(items, start=1):
        print(f"  {index}) {item}")
    raw = input("选哪个 [1]：").strip() or "1"
    try:
        return items[int(raw) - 1]
    except (ValueError, IndexError):
        return items[0]
