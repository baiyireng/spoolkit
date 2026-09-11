"""聊天记录。

这一层解决的是**给人看**的问题，不是给模型看的问题。

两件事必须分清楚：
- **上下文**不加载历史——这是我们整个设计的支点（状态外置，历史可弃）；
- **聊天区域**要能看到之前聊过什么——不然你重启之后面对一片空白，
  根本想不起来上次做到哪。

两者混为一谈会得出错误结论，比如「那就把历史塞回上下文吧」——
那会把短上下文的前提直接推翻。
"""

import sqlite3
import time
from datetime import datetime
from typing import Sequence

USER = "user"
ASSISTANT = "assistant"
NOTE = "note"

ROLE_LABELS = {USER: "你", ASSISTANT: "助手", NOTE: "系统"}


def record_message(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
    meta: str = "",
) -> int:
    """追加一条聊天记录。"""
    cursor = conn.execute(
        "INSERT INTO transcript(session_id, role, content, meta, created_at)"
        " VALUES (?,?,?,?,?)",
        (session_id, role, content.strip(), meta, time.time()),
    )
    conn.commit()
    return cursor.lastrowid


def recent_messages(
    conn: sqlite3.Connection, session_id: str, limit: int = 6
) -> list[sqlite3.Row]:
    """取最近若干条，返回时按时间正序，方便直接展示。"""
    rows = conn.execute(
        "SELECT * FROM transcript WHERE session_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return list(reversed(rows))


def _stamp(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%m-%d %H:%M")


def render_transcript(rows: Sequence[sqlite3.Row]) -> str:
    """渲染成聊天区域的样子。

    单条过长时截断：启动时刷出一大段旧回答，等于把聊天区域变成噪音。
    要看全文可以直接查 .agent/memory.db，那里是完整的。
    """
    if not rows:
        return "（这个会话还没有历史记录）"

    lines: list[str] = []
    for row in rows:
        label = ROLE_LABELS.get(row["role"], row["role"])
        lines.append(f"[{_stamp(row['created_at'])}] {label}：{_shorten(row['content'])}")
        if row["meta"]:
            lines.append(f"    {row['meta']}")
    return "\n".join(lines)


MAX_ENTRY_CHARS = 200


def _shorten(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= MAX_ENTRY_CHARS:
        return flat
    return flat[:MAX_ENTRY_CHARS] + "…（完整内容见 memory.db）"
