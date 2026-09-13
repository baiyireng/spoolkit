"""工作区级的"额外可读目录"。

`--allow-read` 原本只能**启动时**给：一次运行一个参数。可真实用法是"我临时想
让你看看 D:\\我的小说 里有什么"——为这个去重启桥、重新配对，代价太高，于是用户
在聊天里说"我在这里授权你访问那个目录也不行吗"，而当时**确实不行**。

这个文件就是那个记下来的地方：`.agent/read-roots.json`。聊天里
`/allow-read <目录>` 写进去，之后每一轮都自动把 `--allow-read` 带上。

两条刻意的边界：

- **只读**。这里记的目录进的是 `--allow-read`（读得到、写不到），不是工作区——
  工作区身份（记忆、索引、检查点都挂在它上面）不该被一句聊天消息换掉。
- **只留还存在的目录**。盘符拔了、目录删了，记着也没用，读的时候直接过滤掉。
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
    """加一个可读目录。返回（全部记录, 给人看的一句话）。"""
    target = str(Path(directory).expanduser().resolve())
    path = Path(target)
    if not path.is_dir():
        return load(root), f"没有这个目录：{target}"
    roots = load(root)
    if target in roots:
        return roots, f"已经在列表里了：{target}"
    roots.append(target)
    _save(root, roots)
    return roots, f"已允许读取：{target}（只读；下一轮生效）"


def remove(root: Path | str, directory: str) -> tuple[list[str], str]:
    target = str(Path(directory).expanduser().resolve())
    roots = load(root)
    if target not in roots:
        return roots, f"列表里没有：{target}"
    roots.remove(target)
    _save(root, roots)
    return roots, f"已撤销读取：{target}"


def render(root: Path | str) -> str:
    roots = load(root)
    if not roots:
        return "当前没有额外可读目录。要放开：/allow-read <目录>"
    lines = ["额外可读目录（只读）："]
    for item in roots:
        mark = "" if Path(item).is_dir() else "（已不存在）"
        lines.append(f"  {item}{mark}")
    lines.append("撤销：/allow-read --remove <目录>")
    return "\n".join(lines)
