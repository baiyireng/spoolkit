"""聊天记录。

两件事必须分清楚：

- **聊天区域**给人看完整往来（可截断展示）；
- **模型上下文**不加载工具往返，也不把整段历史塞回去——那会把短上下文
  推翻。通道/网页每条消息却是一次新 `run`，指代（「那天气呢」）需要
  最近几轮的**截断摘要**，由 `conversational_prompt` 拼进本轮提示。
"""

import sqlite3
import time
from datetime import datetime
from typing import Sequence

USER = "user"
ASSISTANT = "assistant"
NOTE = "note"

ROLE_LABELS = {USER: "你", ASSISTANT: "助手", NOTE: "系统"}

# 给人看的单条截断。给模型看的单条另有 max_chars，默认更短。
MAX_ENTRY_CHARS = 200
DEFAULT_TURNS = 6
DEFAULT_CHARS = 240


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


def conversational_prompt(
    conn: sqlite3.Connection,
    session_id: str,
    goal: str,
    *,
    turns: int = DEFAULT_TURNS,
    max_chars: int = DEFAULT_CHARS,
) -> str:
    """把最近几轮拼进本轮提示。没有上一轮就只返回当前目标。

    当前这句话已经是 `goal`：它通常刚写入 transcript，这里要丢掉，避免付两遍。
    """
    goal = (goal or "").strip()
    # 多取一条：最后那条往往就是当前目标。
    rows = recent_messages(conn, session_id, limit=max(turns * 2, turns + 1))
    prior = _prior_turns(rows, goal, turns=turns)
    if not prior:
        return goal
    lines = ["上一轮对话（用来理解指代，不是当前任务）："]
    for row in prior:
        label = ROLE_LABELS.get(row["role"], row["role"])
        lines.append(f"{label}：{_clip(row['content'], max_chars)}")
    lines.append("")
    lines.append(f"当前：{goal}")
    return "\n".join(lines)


def _prior_turns(rows: Sequence[sqlite3.Row], goal: str, *, turns: int) -> list:
    """丢掉当前目标对应的那条用户消息，再取最近 `turns` 条。"""
    items = list(rows)
    if items and items[-1]["role"] == USER and items[-1]["content"].strip() == goal:
        items = items[:-1]
    if not items:
        return []
    return items[-turns:]


def _clip(text: str, max_chars: int) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars] + "…"


def _shorten(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= MAX_ENTRY_CHARS:
        return flat
    return flat[:MAX_ENTRY_CHARS] + "…（完整内容见 memory.db）"
