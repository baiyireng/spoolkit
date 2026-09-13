"""聊天通道里的斜杠命令。

为什么要有这一层：**聊天是唯一的交互面**时，有些事得能在聊天里做，而不是让
用户回去开终端。最早缺的就是`授权读目录`——用户在 QQ 里说"我在这里授权你访问
那个目录也不行吗"，而当时的答案是"不行"（只能重启桥加 `--allow-read`）。

规矩：

- **只有被放行的人能用**。命令处理放在准入判定**之后**，陌生人连 `/help` 都
  不该拿到——否则一个公网通道就有了一个免鉴权的操作面。
- **命令只做"管理"，不做"干活"**：改的是桥自己的状态（可读目录、看现状），
  不碰工作区里的东西，也不替模型做决定。
"""

from __future__ import annotations

from pathlib import Path

from spoolkit import readroots

HELP = """\
可以直接说要做的事；这些是桥自己处理的命令：

/allow-read <目录>            放开读取某个目录（只读，写不到；下一轮生效）
/allow-read                   看当前放开了哪些
/allow-read --remove <目录>   撤销
/status                       看这个工作区现在什么状态
/help                         这一页
（另：agent 有改动等你确认时，回 y 应用、n 丢弃）
"""


def handle(text: str, root: Path) -> str | None:
    """是命令就返回答复；不是命令返回 None（交给 agent）。"""
    if not text.startswith("/"):
        return None
    parts = text.split()
    name = parts[0].lower()

    if name in ("/help", "/?"):
        return HELP
    if name == "/allow-read":
        return _allow_read(parts[1:], root)
    if name == "/status":
        return _status(root)
    return f"不认识的命令：{parts[0]}（/help 看有哪些）"


def _allow_read(parts: list[str], root: Path) -> str:
    if not parts:
        return readroots.render(root)
    if parts[0] in ("--remove", "-r", "--forget"):
        if len(parts) < 2:
            return "用法：/allow-read --remove <目录>"
        _, message = readroots.remove(root, parts[1])
        return f"{message}\n{readroots.render(root)}"
    _, message = readroots.add(root, parts[0])
    return f"{message}\n{readroots.render(root)}"


def _status(root: Path) -> str:
    from spoolkit import workspace

    lines = [f"工作区：{Path(root).resolve()}"]
    roots = readroots.load(root)
    lines.append(f"额外可读目录：{len(roots)} 个")
    for item in roots:
        mark = "" if Path(item).is_dir() else "（已不存在）"
        lines.append(f"  {item}{mark}")
    registry = workspace.registry_path()
    lines.append(f"工作区登记表：{registry}")
    pairing = Path(root) / ".agent" / "bridge-pairings.json"
    lines.append(f"配对文件：{pairing}")
    return "\n".join(lines)
