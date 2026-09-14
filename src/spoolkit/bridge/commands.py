"""聊天通道里的斜杠命令。

为什么要有这一层：**聊天是唯一的交互面**时，有些事得能在聊天里做，而不是让
用户回去开终端。

规矩：

- **只有被放行的人能用**。命令处理放在准入判定**之后**，陌生人连 `/help` 都
  不该拿到——否则一个公网通道就有了一个免鉴权的操作面。
- **命令只做"管理"，不做"干活"**：看现状、说明边界，不替模型做决定。
- **聊天里加不了关联目录。** 要碰工作区外的目录，只能由主人在服务器上
  `spool extra add`。小模型分不清危险操作，远程通道不开放授权口。
"""

from __future__ import annotations

from pathlib import Path

from spoolkit import readroots


def workspace_scope_notice(root: Path | str) -> str:
    """远程通道给人看的工作区边界。不进模型上下文。"""
    here = Path(root).resolve()
    extras = readroots.existing(root)
    lines = [
        f"这条是远程聊天通道：默认只能读写当前工作区（{here}）里的文件。"
    ]
    if extras:
        listed = "、".join(str(item) for item in extras)
        lines.append(
            f"主人在服务器上还关联了：{listed}。"
            "这些目录可以读，写要等人确认。"
        )
    else:
        lines.append(
            "别的盘、桌面、下载目录、没说在哪的文件，这边够不到。"
        )
    lines.append(
        "不会在聊天里开放新的目录——远程通道上小模型分不清危险操作。"
        "要关联目录，在服务器上用 `spool extra add <目录>`。"
    )
    return "\n".join(lines)


HELP = """\
可以直接说要做的事。

这条通道默认只能读写**当前工作区**里的文件，聊天里不开放授权口
（别的盘 / 桌面 / 下载 / 没说在哪的文件，除非主人已在服务器上
`spool extra add` 关联过）。

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
        return workspace_scope_notice(root)
    if name == "/status":
        return _status(root)
    return f"不认识的命令：{parts[0]}（/help 看有哪些）"


def _status(root: Path) -> str:
    from spoolkit import workspace

    lines = [workspace_scope_notice(root), "", f"工作区：{Path(root).resolve()}"]
    lines.append(readroots.render(root))
    pairing = Path(root) / ".agent" / "bridge-pairings.json"
    lines.append(f"配对文件：{pairing}")
    registry = workspace.registry_path()
    lines.append(f"工作区登记表：{registry}")
    return "\n".join(lines)
