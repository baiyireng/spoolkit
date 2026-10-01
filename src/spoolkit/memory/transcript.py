"""聊天记录。

三件事必须分清楚：

- **聊天区域**给人看完整往来（可截断展示）；
- **模型上下文**不加载工具往返，也不把整段历史塞回去——那会把短上下文
  推翻。通道/网页每条消息却是一次新 `run`，指代（「那天气呢」）需要
  最近几轮的**截断摘要**，由 `conversational_prompt` 拼进本轮提示：
  最近几轮给截断原文，更早那一段给**有界摘要**（摘要不进原文）；
  消息里出现指代词时再加**一行**提醒（`deictic_reminder`）——两者都只
  在通道/网页那条路上发生，`spool run --goal` 那种一次性的活没有"上一轮"。
- **按需检索**：历史不进上下文，但模型判断「这个问题是从哪来的」时，
  可以用 `search_messages` 主动把相关片段取回来（片段 + 出处）。默认
  不检索，是这里的核心口径——每轮都塞历史等于把短上下文这件事作废。

检索**默认只查当前会话**（`resolve_scope`）：跨会话是一次**显式**选择
（`session="*"`）。理由见 `docs/rationale.md` 的「索引与记忆的口径」。
"""

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, NamedTuple, Sequence

USER = "user"
ASSISTANT = "assistant"
NOTE = "note"

ROLE_LABELS = {USER: "你", ASSISTANT: "助手", NOTE: "系统"}

# 给人看的单条截断。给模型看的单条另有 max_chars，默认更短。
MAX_ENTRY_CHARS = 200
DEFAULT_TURNS = 6
DEFAULT_CHARS = 240

# 「全部会话」的显式说法。用 `*` 而不是空串：空串是「没给」，而没给的含义
# 是**当前会话**——这两个必须能分开，否则默认跨会话会从空参数里长回来。
ALL_SESSION_MARKS = ("*", "all")


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


# 两行抬头是发给模型的旁白，各自点明那一段的用途与边界：来源是过去的往来，
# 作用是理解指代与承接，都不是当前任务。抽成常量，改措辞只有一处，
# 测试也照着它切段，不必再抄一遍字面。
DIGEST_HEADER = "以下是更早往来的截断摘要，用于理解指代与承接，不是当前任务。"
PRIOR_HEADER = "以下是上一轮对话，用于理解指代，不是当前任务。"


def conversational_prompt(
    conn: sqlite3.Connection,
    session_id: str,
    goal: str,
    *,
    turns: int = DEFAULT_TURNS,
    max_chars: int = DEFAULT_CHARS,
    digest_turns: int = 0,
    digest_chars: int = 0,
) -> str:
    """把最近几轮 + 更早那一段的摘要拼进本轮提示；没有上一轮就只返回当前目标。

    当前这句话已经是 `goal`：它通常刚写入 transcript，这里要丢掉，避免付两遍。

    两段的分工（`turns`/`max_chars` 是**最近**那几轮的原文截断，
    `digest_turns`/`digest_chars` 是**更早**那一段的摘要）：

    - 最近那几轮给原文（截断），指代（「它」「那件事」）指的多半就在里面；
    - 再往前的给一段**有界摘要**，让模型天然知道"我们聊到哪儿了"，而不是
      只能靠它主动去检索。文字模型不会主动查（它不知道自己缺什么），
      这正是用户说的「意图识别能力太弱，似乎不会主动结合上下文分析」。
    - **摘要不是原文**：每条都按 `max_chars` 截断、总长还有字符预算，绝不把
      整段历史塞回窗口（那等于把「历史默认不进上下文」这条设计推翻）。
      最坏占用由调用方传入的标定值算得出来：正文总量 ≤ `digest_chars` 字符
      （中文大致 1 字 = 1 token 的量级，见 `limits.render` 的来源列）。
    - 摘要也过 `redact_secrets`：它会进模型上下文，密钥不能跟着进去。

    用户消息含指代词时，开头加**一行**提醒（`deictic_reminder`）——只加那一行，
    普通消息一个字的提示都不加。
    """
    goal = (goal or "").strip()
    # 多取几条：最后那条往往就是当前目标，前面要留给摘要那一段。
    span = max(turns * 2, turns + 1) + max(0, int(digest_turns))
    rows = recent_messages(conn, session_id, limit=span)
    items = _drop_current(rows, goal)
    prior = items[-turns:] if turns > 0 else []
    older = items[: -turns] if turns > 0 else items
    digest = _digest(older, turns=digest_turns, max_chars=digest_chars)

    if not prior and not digest:
        return _with_reminder(goal, goal)

    lines: list[str] = []
    if digest:
        lines.append(DIGEST_HEADER)
        lines.extend(digest)
    if prior:
        lines.append(PRIOR_HEADER)
        for row in prior:
            label = ROLE_LABELS.get(row["role"], row["role"])
            # 与摘要同一层脱敏：这条路也直通模型上下文，口径只能有一份。
            lines.append(f"{label}：{redact_secrets(_clip(row['content'], max_chars))}")
    lines.append("")
    lines.append(f"当前：{goal}")
    return _with_reminder("\n".join(lines), goal)


def _drop_current(rows: Sequence[sqlite3.Row], goal: str) -> list:
    """丢掉当前目标对应的那条用户消息：它已经是 `goal`，留着等于付两遍。"""
    items = list(rows)
    if items and items[-1]["role"] == USER and items[-1]["content"].strip() == goal:
        items = items[:-1]
    return items


def _digest(older: Sequence[sqlite3.Row], *, turns: int, max_chars: int) -> list[str]:
    """把「更早那一段」压成摘要行。越界或坏标定值都退化成「没有摘要」。

    预算的算法写清楚（它就是"最坏占多少"的出处）：

    1. 最多取 `turns` 条（更早的丢掉——最近的往来对理解指代更要紧）；
    2. 每条正文截到 `max_chars`（助手那侧取一半：它通常更长，同样几条能多覆盖几轮）；
    3. 再按**总字符预算** `max_chars` 从最老的开始丢，直到装得下。

    于是无论历史多长、消息多长，这段摘要的正文总量都不超过 `max_chars` 字符
    （字符数 ≠ token 数，中文大致 1 字 = 1 token 的量级；行首的角色标签是
    `ROLE_LABELS` 里那两三个字，也要算进去）。
    """
    turns = max(0, int(turns))
    width = max(0, int(max_chars))
    if not turns or not width:
        return []
    lines: list[str] = []
    for row in list(older)[-turns:]:
        label = ROLE_LABELS.get(row["role"], row["role"])
        # 助手那侧通常更长：给它一半宽度，同样几条摘要能覆盖更多轮。
        clip = width // 2 if row["role"] == ASSISTANT else width
        # 摘要会进模型上下文，所以和检索片段一样先过脱敏层。
        lines.append(f"{label}：{redact_secrets(_clip(row['content'], clip))}")
    # 从最老的开始丢，直到装进预算。
    while len(lines) > 1 and sum(len(line) for line in lines) > width:
        lines.pop(0)
    return lines


# 指代 / 承接的说法。命中就加一行提醒——**只加一行**，不是一段提示，
# 也不是对所有消息都加：普通消息带上它，每一轮都在付一笔没用的税。
_DEICTIC_MARKS = (
    "那个", "那件", "那次", "那种", "那几条", "该文件", "此事",
    "上次", "上回", "前面提到", "前面说", "刚才", "刚刚", "之前", "先前",
    "继续", "接着", "还是按", "照旧", "老样子", "它", "这个", "这些", "这条",
)

DEICTIC_REMINDER = (
    "这条消息含指代或承接，不是完整表述：先确认它指的是什么；"
    "需要时先调用 search_history，再作答。"
    "检索默认只查当前会话，跨会话须显式声明。"
    "按检索结果作答，未检索到时说明未检索到，不得推测。"
)


def has_deictic(text: str) -> bool:
    """这条用户消息是不是含指代 / 承接。

    刻意保持朴素：一个子串表，不引分词、不做语义判断。判据是**宁窄勿宽**
    ——多提醒一次只是多一行字，漏提醒一次才是真的丢上下文。
    """
    said = str(text or "")
    return any(mark in said for mark in _DEICTIC_MARKS)


def deictic_reminder(text: str) -> str:
    """命中就返回那一行提醒，没命中返回空串（调用方直接拼，不用再判一次）。"""
    return DEICTIC_REMINDER if has_deictic(text) else ""


def _with_reminder(body: str, user_text: str) -> str:
    reminder = deictic_reminder(user_text)
    return f"{reminder}\n\n{body}" if reminder else body


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


class Scope(NamedTuple):
    """一次检索的作用域：进 SQL 的 `session` + 给人看的 `description`。

    用 NamedTuple 而不是普通元组：调用方既可能 `session, said = resolve_scope(...)`，
    也可能只想要其中一半（`resolve_scope(...).description`），两种都支持。
    """

    session: str
    description: str


def resolve_scope(raw: str, current: str) -> Scope:
    """把「模型/人给的作用域」解成（进 SQL 的 session, 给人看的说法）。

    三个输入各有各的说法，这里只认三种：

    - 没给（`""`）或 `"current"` / `"this"` —— **当前会话**。这是默认值：
      历史是分会话的活，跨会话去翻别人的往来必须是一次**显式**动作
      （用户原话：历史消息的查看权限似乎没有限制到会话一级）。
    - `"*"` / `"all"` —— 本工作区的**全部**会话（工作区边界不动：跨工作区
      依旧不做，历史按工作区分库存）。
    - 别的字符串 —— 就是那条会话的名字（保留「指定某一条会话」的能力）。

    返回的 `session` 是喂给 `search_messages` 的值（空串 = 不过滤会话），
    `description` 只用于文案，别拿它去查库。
    """
    text = str(raw or "").strip()
    if not text or text.lower() in ("current", "this"):
        return Scope(current, f"当前会话 {current}" if current else "当前会话")
    if text in ALL_SESSION_MARKS:
        return Scope("", "本工作区的全部会话")
    return Scope(text, f"会话 {text}")


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
    """按关键词检索聊天往来。`session=""` = 不过滤会话（库里能看到的都查）。

    **默认作用域由调用方给**（`resolve_scope`）：工具与命令行都默认
    「当前会话」，跨会话要显式声明。这一层保留 `""`=全部，是为了让「全部」
    有个明确的名字，而不是让空参数顺手变成跨会话——上一条的默认是跨工作区
    全部会话，用户提的正是这一点（历史消息的查看权限似乎没有限制到会话一级）。

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
    hits: Sequence[MessageHit],
    titles: Mapping[str, str] | None = None,
    *,
    cross_session: bool = False,
    scope_said: str = "",
) -> str:
    """把命中渲染成「片段 + 出处」。

    出处要给齐（会话、时间、角色、消息 id、命中位置）：只给一句结论，
    模型没法判断「这个问题是从哪来的」，人也没法回库核对。
    列表按新到旧的方向排——时间戳在最前面。

    **每一行都带会话名**（同一份格式，不按作用域分叉），**跨会话时抬头还要
    额外点名**（`cross_session`）：默认只查当前会话时那句会话名是冗余的，
    可一旦跨了会话，来源会话就是判断「这句话是谁说的」的第一信息——不点出来，
    模型会把别的会话里的话当成这条会话的上下文。
    """
    if not hits:
        return ""
    head = f"找到 {len(hits)} 条历史消息"
    if scope_said:
        head += f"（范围：{scope_said}）"
    if cross_session:
        head += "；**来自多条会话**，每条都标了来源会话"
    lines = [head + "（命中词多的在前，同分按新到旧）："]
    for hit in hits:
        title = (titles or {}).get(hit.session_id, "")
        label = ROLE_LABELS.get(hit.role, hit.role)
        where = f"会话 {hit.session_id}" + (f"「{title}」" if title else "")
        lines.append(
            f"- [{_stamp(hit.created_at)}] {where} {label} #{hit.id}"
            f"（命中第 {hit.offset} 字）：{hit.snippet}"
        )
    return "\n".join(lines)


def no_hits_text(
    query: str, *, session: str = "", scope_said: str = "", cross_session: bool = False
) -> str:
    """没找到时要**说清在哪个范围里没找到**，而不是给空串让模型猜。

    边界也要一起说：这里是关键词子串匹配，且历史按工作区分库——不说，
    模型会把「没搜到」读成「没发生过」。

    默认作用域是**当前会话**，所以文案必须把两件事都讲到：这次只查了
    当前会话，以及**跨会话该怎么显式要**。只写「没找到」，模型（和人）
    都会以为答案不存在，而它可能就写在隔壁那条会话里。
    """
    said = str(scope_said or "").strip()
    if not said:
        said = f"会话 {session}" if session else "这个工作区的全部会话"
    lines = [
        f"没有找到匹配的历史消息（范围：{said}，关键词：{query}）。",
        "这不是「没发生过」：检索是关键词子串匹配，换个词、或者只给一个关键词再查一次。",
    ]
    if not cross_session:
        lines.append(
            "注意：本次**只查了当前会话**。要跨会话查，显式声明一次——"
            '传 session="*"（本工作区的全部会话），命令行上写 `--all-sessions`；'
            "也可以点名某一条会话。"
        )
    lines.append("别的**工作区**的历史不在这里（历史按工作区分库存）。")
    return "\n".join(lines)
