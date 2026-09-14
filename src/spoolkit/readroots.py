"""工作区级的关联目录。

记在 `.agent/read-roots.json`。**只能由本机指令写入**（`spool extra add` /
`spool extra remove`），远程聊天里的 `/allow-read` 加不进去——那边小模型
分不清危险操作。

两条刻意的边界：

- **工作区身份不变。** 记忆、索引、检查点仍挂在主工作区上；关联目录不是换绑。
- **只留还存在的目录。** 盘符拔了、目录删了，记着也没用，读的时候直接过滤掉。
- **写走待确认。** 读可以直接做；写进 PendingChanges，确认前不落盘，
  即使 `--policy auto` 也不自动写关联目录。
"""

from __future__ import annotations

import json
from pathlib import Path

FILE = "read-roots.json"


def path_for(root: Path | str) -> Path:
    return Path(root) / ".agent" / FILE


def load(root: Path | str) -> list[str]:
    """记下来的目录（原样，不判断存不存在——列表要看得见"记了什么"）。"""
    path = path_for(root)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = payload.get("roots") if isinstance(payload, dict) else payload
    return [str(item) for item in items or []]


def existing(root: Path | str) -> list[Path]:
    """真正会被传下去的：存在、且是目录。"""
    found: list[Path] = []
    for item in load(root):
        path = Path(item)
        if path.is_dir() and path not in found:
            found.append(path)
    return found


def _save(root: Path | str, roots: list[str]) -> None:
    path = path_for(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"roots": roots}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add(root: Path | str, directory: str) -> tuple[list[str], str]:
    """加一个关联目录。返回（全部记录, 给人看的一句话）。"""
    target = str(Path(directory).expanduser().resolve())
    path = Path(target)
    if not path.is_dir():
        return load(root), f"没有这个目录：{target}"
    roots = load(root)
    if target in roots:
        return roots, f"已经在列表里了：{target}"
    roots.append(target)
    _save(root, roots)
    return roots, (
        f"已关联：{target}（读放开，写要确认后才落盘；下一轮生效）"
    )


def remove(root: Path | str, directory: str) -> tuple[list[str], str]:
    target = str(Path(directory).expanduser().resolve())
    roots = load(root)
    if target not in roots:
        return roots, f"列表里没有：{target}"
    roots.remove(target)
    _save(root, roots)
    return roots, f"已取消关联：{target}"


def render(root: Path | str) -> str:
    roots = load(root)
    if not roots:
        return (
            "当前没有关联目录。要放开：在服务器上 `spool extra add <目录>`"
            "（聊天里加不了）。"
        )
    lines = ["关联目录（读放开，写要确认后才落盘）："]
    for item in roots:
        mark = "" if Path(item).is_dir() else "（已不存在）"
        lines.append(f"  {item}{mark}")
    lines.append("撤销：spool extra remove <目录>")
    return "\n".join(lines)
