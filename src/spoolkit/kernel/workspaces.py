"""工作区：在哪、叫什么、怎么找到它。

搬过来的是原来 `spoolkit/workspace.py` 的规则（向上找 `.agent/`、显式就是显式、
登记表是最近使用序），新增的是**命名**：目录名、别名、序号。

别名单独放一个文件（`workspace-aliases.json`），**不动登记表的格式**——别的工具
可能按 `{"workspaces": ["D:\\..."]}` 这个老格式读它，往里塞对象会把它们弄瞎。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from spoolkit.kernel import jsonstore

MARK = ".agent"
REGISTRY = "workspaces.json"
ALIASES = "workspace-aliases.json"
STATE_ENV = "SPOOLKIT_STATE"


class WorkspaceError(RuntimeError):
    """工作区找不到、或者有好几个说不清是哪个。"""


@dataclass(frozen=True)
class Workspace:
    """登记表里的一个工作区。`name` 是显示名（重名时带上一级）。"""

    root: Path
    name: str
    alias: str = ""


def state_dir() -> Path:
    """这台机器上放用户级状态的地方（登记表、指向、放行）。"""
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


def aliases_path() -> Path:
    return state_dir() / ALIASES


def find_root(start: Path | None = None) -> Path | None:
    """从 `start`（默认当前目录）往上找带 `.agent/` 的那一层。"""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / MARK).is_dir():
            return candidate
    return None


def known() -> list[Path]:
    """登记表里的工作区（只留现在还在的，顺序是最近使用在前）。"""
    payload = jsonstore.read_json(registry_path(), {})
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
    """把工作区登记下来（幂等，最近用的排最前）。写不进去不挡干活。"""
    target = Path(root).resolve()
    order = [item for item in known() if item != target]
    order.insert(0, target)
    try:
        jsonstore.write_json(
            registry_path(), {"workspaces": [str(item) for item in order]}
        )
    except OSError:
        # 登记不上不该挡住干活：最坏是下次还得写 --root。
        pass


def load_aliases() -> dict[str, Path]:
    """别名 → 工作区。坏了、指向不存在的工作区，都当没设过。"""
    payload = jsonstore.read_json(aliases_path(), {})
    raw = payload.get("aliases") if isinstance(payload, dict) else None
    found: dict[str, Path] = {}
    for alias, target in (raw or {}).items():
        try:
            path = Path(str(target)).resolve()
        except (OSError, ValueError):
            continue
        if path.is_dir():
            found[str(alias)] = path
    return found


def set_alias(root: Path | str, alias: str) -> None:
    """给工作区起个别名。同一个工作区的旧别名会被新别名顶掉。"""
    target = Path(root).resolve()
    name = str(alias).strip()
    if not name:
        raise WorkspaceError("别名不能是空的")
    if name.isdigit():
        # 序号是解析顺序里的一个位置，被别名抢走之后那人就再也点不动第 3 个了。
        raise WorkspaceError("别名不能用纯数字（那是按序号选的写法）")
    current = {key: value for key, value in load_aliases().items() if value != target}
    current[name] = target
    jsonstore.write_json(
        aliases_path(), {"aliases": {k: str(v) for k, v in current.items()}}
    )


def _display_names(roots: list[Path]) -> dict[Path, str]:
    """重名时带上父目录名：两个 `app` 在列表里要分得开。"""
    counts: dict[str, int] = {}
    for root in roots:
        counts[root.name] = counts.get(root.name, 0) + 1
    return {
        root: (root.name if counts[root.name] == 1 else f"{root.parent.name}/{root.name}")
        for root in roots
    }


def list_workspaces() -> list[Workspace]:
    """登记表 + 别名 + 显示名，最近使用在前。"""
    roots = known()
    names = _display_names(roots)
    by_root = {value: key for key, value in load_aliases().items()}
    return [
        Workspace(root=root, name=names[root], alias=by_root.get(root, ""))
        for root in roots
    ]


def forget(name_or_path: str | Path) -> Path:
    """从登记表摘掉（**不删目录、不删 `.agent/`**）。"""
    root = resolve_workspace(name_or_path)
    order = [item for item in known() if item != root]
    jsonstore.write_json(registry_path(), {"workspaces": [str(item) for item in order]})
    rest = {key: value for key, value in load_aliases().items() if value != root}
    jsonstore.write_json(
        aliases_path(), {"aliases": {k: str(v) for k, v in rest.items()}}
    )
    return root


def _as_workspace(root: Path, raw: str) -> Path:
    """点名到的东西得**真的能干活**：没有 `.agent/` 就按"按路径点名"的语义往上找一层，
    再找不到就报清楚。

    为什么必须有（全分支评审的必修项）：登记表里可以有**还不是工作区**的目录
    （`register()` 被显式参数、`ws use <路径>` 等路径调用时会登记，`known()` 只校验
    目录存在）。于是同一个目录会出现两种结果——**按名字点名拿到它自己**（没有
    `.agent/`，`ready=false`），**按路径点名沿祖先找到真正那个工作区**。用哪个写法
    决定你落到哪份记忆/计划上，那是不能接受的。现在两种写法**口径一致**。
    """
    if (root / MARK).is_dir():
        return root
    found = find_root(root)
    if found is not None:
        return found
    raise WorkspaceError(
        f"{root} 还不是工作区（没有 {MARK}/）——它在登记表里不等于能干活。"
        f"要在那儿干活先 `spool ws add {root}`。"
    )


def _looks_like_path(raw: str) -> bool:
    """这个字符串是不是**路径写法**（而不是一个名字）。

    判据只认写法：绝对路径（含盘符）、带分隔符、或以 `./` `../` `~/` 开头。
    **不拿"它恰好存在"当判据**——那会让同一个名字在不同 cwd 下解析成不同的工作区，
    而 MCP 那条入口的 cwd 跟调用方想在哪干活毫无关系（见 `kernel/target.py`）。
    """
    if raw.startswith(("./", "../", "~/", ".\\", "..\\", "~\\")):
        return True
    if os.path.isabs(raw) or os.path.splitdrive(raw)[0]:
        return True
    return "/" in raw or "\\" in raw


def resolve_workspace(name_or_path: str | Path, *, use_cwd: bool = True) -> Path:
    """名字、别名、序号（1 起）或路径。找不到就抛 `WorkspaceError`，**不猜**。

    `use_cwd=False`：这条入口**不看进程 cwd**。MCP 服务是被客户端按配置拉起来的
    （`spool mcp` 常常从任意目录启动），它的启动目录跟调用方想在哪干活没有关系；
    这时**纯名字**只认登记表，路径写法（绝对路径 / 带分隔符 / `./`）照旧可用——
    那是调用方显式写的，不是从 cwd 猜的。
    """
    raw = str(name_or_path).strip()
    if not raw:
        raise WorkspaceError("没有给工作区名字或路径")

    aliases = load_aliases()
    if raw in aliases:
        return _as_workspace(aliases[raw], raw)

    path_like = _looks_like_path(raw)
    items = list_workspaces()

    # 纯名字/序号**必须先查登记表**，再考虑"它恰好存在"：
    # 反过来的话，cwd 里只要碰巧有个同名目录，点名就会被劫持到那个目录所属的工作区，
    # 登记表里真正叫这个名字的工作区永远轮不到。实测过：向 MCP 点名 `snake-vue`，
    # 服务进程 cwd 在 `agents_dev` 下、那里恰好有个 `snake-vue\` 子目录，
    # 于是"向上找 .agent/"把它解析成了 `agents_dev` —— 点名 b 却在 a 里干活。
    if not path_like:
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(items):
                return _as_workspace(items[index - 1].root, raw)
            raise WorkspaceError(f"只有 {len(items)} 个登记过的工作区，没有第 {index} 个。")

        exact = [item for item in items if item.root.name == raw or item.alias == raw]
        if len(exact) == 1:
            return _as_workspace(exact[0].root, raw)
        if len(exact) > 1:
            listed = "\n".join(f"  {item.root}" for item in exact)
            raise WorkspaceError(
                f"有多个工作区都叫 {raw}：\n{listed}\n"
                f"用完整路径，或起个别名：`spool ws alias <路径> {raw}`"
            )

    candidate = Path(raw)
    if candidate.exists() and (path_like or use_cwd):
        found = find_root(candidate)
        if found is not None:
            return found
        raise WorkspaceError(
            f"{candidate.resolve()} 不是工作区（没有 {MARK}/）。"
            f"要在那里干活先 `spool ws add {candidate.resolve()}`。"
        )

    if path_like:
        raise WorkspaceError(
            f"{candidate.resolve()} 不是一个存在的工作区（没有 {MARK}/）。"
            f"要用它先建好目录并 `spool ws add {candidate.resolve()}`。"
        )

    if not items:
        raise WorkspaceError(
            f"没有叫 {raw} 的工作区，登记表也是空的。先 `spool ws add <路径>`。"
        )
    listed = "\n".join(
        f"  {index}) {item.root}" for index, item in enumerate(items, start=1)
    )
    raise WorkspaceError(f"没有叫 {raw} 的工作区。现在登记的有：\n{listed}")
