"""聊天记录。

三件事必须分清楚：

- **聊天区域**给人看完整往来（可截断展示）；
- **模型上下文**不加载工具往返，也不把整段历史塞回去——那会把短上下文
  推翻。通道/网页每条消息却是一次新 `run`，指代（「那天气呢」）需要
  最近几轮的**截断摘要**，由 `conversational_prompt` 拼进本轮提示。
- **按需检索**：历史不进上下文，但模型判断「这个问题是从哪来的」时，
  可以用 `search_messages` 主动把相关片段取回来（片段 + 出处）。默认
  不检索，是这里的核心口径——每轮都塞历史等于把短上下文这件事作废。
"""

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

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


# --- 按查询检索历史消息 ---------------------------------------------------
#
# 这里是「问题是从哪来的」的那条路：上下文不装历史，模型要联系过去时
# 自己调一次检索。**检索是纯读**——不写库、不建索引、不改任何一条消息。


@dataclass(frozen=True)
class MessageHit:
    """一条命中的历史消息。

    `snippet` 与 `offset` 一起给：片段说明「说了什么」，`offset` 说明
    「命中的是哪一处」——只给一段文字，模型分不清这条为什么被选中。
    `id` 是出处，人可以照着回 `memory.db` 核对原文。
    """

    id: int
    session_id: str
    role: str
    created_at: float
    snippet: str
    offset: int
    matched: tuple[str, ...] = ()


# 明显的密钥形状。检索结果会**回灌进模型上下文**，密钥跟着进去等于把
# `.env` 抄进了对话记录（还可能是别人的屏幕）。所以片段一律先过这一层：
# 只盖掉值、留下键名与前缀，人看得出"这里原本有个密钥"。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 常见供应商的密钥前缀：`sk-…` / `ghp_…` / `xoxb-…`。
    (re.compile(r"(?i)\b(sk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_-]{8,}"), r"\1-***"),
    # Google 的 `AIza…`：没有分隔符，只能按前缀加长度认。
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "AIza***"),
    # 任意 `名字里有 KEY/SECRET/TOKEN/PASSWORD 的键 = 值`：值一律盖掉。
    # 值的边界要认中文标点：`\S+` 会把「KEY=值，下一句话」整段吞进值里，
    # 那就不只是盖密钥了，而是把上下文也一起删掉。
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|APPID)"
            r"[A-Z0-9_]*)(\s*[=:]\s*)([^\s,;，；。、)）]+)"
        ),
        r"\1\2***",
    ),
    # `Authorization: Bearer …`
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{10,}"), "Bearer ***"),
)


def redact_secrets(text: str) -> str:
    """把片段里明显的密钥值盖掉，只留键名与前缀。"""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def parse_moment(raw: str, *, label: str) -> float:
    """把 `2026-09-27` 或 `2026-09-27 14:03` 解析成本地时间戳。

    只认 ISO 形状，认不出就报错、不猜：猜错的时间范围会让人以为
    「那几天没聊过」，那比报错难查得多。只给日期时按当天 00:00 算。
    """
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise ValueError(
            f"{label} 的时间格式认不出：{raw}。写法如 2026-09-27 或 2026-09-27 14:03。"
        ) from exc


def _escape_like(token: str) -> str:
    """转义 LIKE 的通配符：`_` 和 `%` 是查询里的普通字符，不是通配符。"""
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _find_ci(haystack: str, needle: str) -> int:
    """大小写不敏感地找位置（SQL 的 LIKE 对 ASCII 就是不敏感的）。"""
    return haystack.lower().find(needle.lower())


def _window(text: str, pos: int, width: int) -> tuple[str, int]:
    """取命中处周围的一段，返回（片段, 命中在片段里的下标）。

    从命中处**往前**留三分之一：只往后留的话，命中的词前面那半句语境
    （「上一句在说哪个功能」）会被整段切掉，而语境正是判断来源要的东西。
    """
    width = max(1, int(width))
    if len(text) <= width:
        return text, pos
    start = min(max(0, pos - width // 3), len(text) - width)
    end = start + width
    head = "…" if start > 0 else ""
    tail = "…" if end < len(text) else ""
    return f"{head}{text[start:end]}{tail}", pos - start + len(head)


def search_messages(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    snippet_chars: int,
    session: str = "",
    since: float | None = None,
    until: float | None = None,
) -> list[MessageHit]:
    """按关键词检索聊天往来（**跨会话**是默认：新会话里问「这问题从哪来的」，
    答案往往就写在别的会话里）。

    为什么用 LIKE 而不是再养一张 FTS5 表：`recall` 那边的记忆是**蒸馏过的
    短条目**，值得一份索引；这里是**整段原文**，每写一条消息就多写一次索引，
    而它回答的问题本来就只偶尔问一次。代价是只做**子串匹配**：查询要给关键词
    （多个词用空格分开），长句子不会命中——没命中会明说没找到（`no_hits_text`），
    不给空串让模型猜。

    `limit` 与 `snippet_chars` **没有默认值**：它们直接决定回灌多少字，出处
    只能有一个——`limits.py` 的标定表，写死在核心函数里就等于绕过了它。
    """
    tokens = [token for token in str(query).split() if token]
    if not tokens:
        return []

    needles = [f"%{_escape_like(token)}%" for token in tokens]
    # 命中词多的优先，同分按新到旧：排前面的更可能是「那句话的来源」。
    score = " + ".join(
        "CASE WHEN content LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END" for _ in needles
    )
    where = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in needles)
    sql = (
        "SELECT id, session_id, role, content, created_at,"
        f" ({score}) AS matched FROM transcript"
        f" WHERE ({where})"
    )
    params: list[object] = list(needles) + list(needles)
    if session:
        sql += " AND session_id = ?"
        params.append(session)
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(float(since))
    if until is not None:
        sql += " AND created_at <= ?"
        params.append(float(until))
    sql += " ORDER BY matched DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit)))

    hits: list[MessageHit] = []
    for row in conn.execute(sql, params).fetchall():
        content = str(row["content"])
        matched = tuple(token for token in tokens if _find_ci(content, token) >= 0)
        # 片段按折叠空白后的正文取：聊天里换行很多，留着只会把预算花在空行上。
        flat = " ".join(content.split())
        spots = [pos for pos in (_find_ci(flat, token) for token in matched) if pos >= 0]
        snippet, offset = _window(flat, min(spots) if spots else 0, snippet_chars)
        hits.append(
            MessageHit(
                id=int(row["id"]),
                session_id=str(row["session_id"]),
                role=str(row["role"]),
                created_at=float(row["created_at"]),
                snippet=redact_secrets(snippet),
                offset=offset,
                matched=matched,
            )
        )
    return hits


def render_history_hits(
    hits: Sequence[MessageHit], titles: Mapping[str, str] | None = None
) -> str:
    """把命中渲染成「片段 + 出处」。

    出处要给齐（会话、时间、角色、消息 id、命中位置）：只给一句结论，
    模型没法判断「这个问题是从哪来的」，人也没法回库核对。
    列表按新到旧的方向排——时间戳在最前面。
    """
    if not hits:
        return ""
    lines = [f"找到 {len(hits)} 条历史消息（命中词多的在前，同分按新到旧）："]
    for hit in hits:
        title = (titles or {}).get(hit.session_id, "")
        label = ROLE_LABELS.get(hit.role, hit.role)
        where = f"会话 {hit.session_id}" + (f"「{title}」" if title else "")
        lines.append(
            f"- [{_stamp(hit.created_at)}] {where} {label} #{hit.id}"
            f"（命中第 {hit.offset} 字）：{hit.snippet}"
        )
    return "\n".join(lines)


def no_hits_text(query: str, *, session: str = "") -> str:
    """没找到时要**说清在哪个范围里没找到**，而不是给空串让模型猜。

    边界也要一起说：这里是关键词子串匹配，且历史按工作区分库——不说，
    模型会把「没搜到」读成「没发生过」。
    """
    scope = f"会话 {session}" if session else "这个工作区的全部会话"
    return (
        f"没有找到匹配的历史消息（查了{scope}，关键词：{query}）。"
        "这不等于没有：检索是关键词子串匹配，换个词、或者只给一个关键词再查一次；"
        "别的**工作区**的历史不在这里（历史按工作区分库存）。"
    )
