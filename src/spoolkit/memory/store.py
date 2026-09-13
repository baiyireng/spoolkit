"""记忆的 SQLite 存储与关键词检索。

检索用 SQLite 自带的 FTS5，不引入向量库：代码与项目语境里的讨论对象
多是独特标识符，关键词命中率本来就高，先把这个便宜的好处拿满。

中文处理是个坑：FTS5 默认分词器会把一整段连续中文当成一个 token，
「自动归档整理」和查询「归档」对不上。因此写入与查询都要先把
中文字符逐字用空格分开，让默认分词器能把它们切成独立的可检索单元。
"""

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    text          TEXT NOT NULL,
    trigger       TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    confidence    REAL NOT NULL DEFAULT 0.5,
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    applied_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS episode (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    task       TEXT NOT NULL,
    summary    TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(text);

CREATE TABLE IF NOT EXISTS session (
    id            TEXT PRIMARY KEY,
    project_root  TEXT NOT NULL,
    model         TEXT NOT NULL DEFAULT '',
    context_limit INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_active   REAL NOT NULL
);

-- 给人看的聊天记录。和上下文是两件事：
-- 上下文不加载历史（状态外置），但你要能回头看之前聊过什么。
CREATE TABLE IF NOT EXISTS transcript (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind);
"""

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


@dataclass(frozen=True)
class Memory:
    """一条记忆。"""

    id: int
    kind: str
    text: str
    source: str = ""
    confidence: float = 0.5
    trigger: str = ""
    use_count: int = 0


def normalize_for_fts(text: str) -> str:
    """把中文字符逐字分开，让 FTS5 默认分词器能切出可检索的中文词。"""
    return _CJK.sub(lambda match: f" {match.group(0)} ", text)


def _phrase_query(query: str) -> str:
    """把查询规范化并转成 FTS5 短语查询，避免多词被当成 OR。"""
    tokens = normalize_for_fts(query).split()
    return '"' + " ".join(tokens) + '"'


def init_memory_schema(conn: sqlite3.Connection) -> None:
    """建表。可重复调用。"""
    conn.executescript(MEMORY_SCHEMA)
    conn.commit()


def add_memory(
    conn: sqlite3.Connection,
    kind: str,
    text: str,
    source: str = "",
    confidence: float = 0.5,
    trigger: str = "",
) -> Memory:
    """写入一条记忆，并同步维护检索索引。"""
    cursor = conn.execute(
        "INSERT INTO memory"
        "(kind, text, trigger, source, confidence, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (kind, text, trigger, source, confidence, time.time()),
    )
    memory_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO memory_fts(rowid, text) VALUES (?, ?)",
        (memory_id, normalize_for_fts(text)),
    )
    conn.commit()
    return Memory(
        id=memory_id,
        kind=kind,
        text=text,
        source=source,
        confidence=confidence,
        trigger=trigger,
    )


def _to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=row["id"],
        kind=row["kind"],
        text=row["text"],
        source=row["source"],
        confidence=row["confidence"],
        trigger=row["trigger"],
        use_count=row["use_count"],
    )


# 公开别名：别的模块不该为了拿这个转换函数去碰私有名字。
to_memory = _to_memory


def list_memories(conn: sqlite3.Connection, kind: str | None = None) -> list[Memory]:
    """按写入顺序列出记忆，可按类型过滤。"""
    if kind is None:
        rows = conn.execute("SELECT * FROM memory ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memory WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
    return [_to_memory(row) for row in rows]


def search_memories(conn: sqlite3.Connection, query: str) -> list[Memory]:
    """关键词检索，按「用过多少次 + 多久没用」重排。

    FTS5 只给出「匹配上了」，给不出「哪条更有用」。这里叠加两个维度：
    用过次数多的更可能有用；很久没被翻出来的逐渐沉底。

    规则很土，但胜在可解释——排序不合预期时，一眼能看出是频率还是
    时间把某条顶上来的。
    """
    if not query.strip():
        return []
    try:
        rows = conn.execute(
            "SELECT m.* FROM memory_fts f JOIN memory m ON m.id = f.rowid"
            " WHERE memory_fts MATCH ?",
            (_phrase_query(query),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_to_memory(row) for row in _rank(rows)]


FREQUENCY_WEIGHT = 0.6
RECENCY_WEIGHT = 0.4


def _rank(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """按使用频率与新鲜度重排。"""
    now = time.time()

    def score(row: sqlite3.Row) -> float:
        last = row["last_used_at"] or row["created_at"]
        age_days = max(0.0, (now - last) / 86400)
        recency = 1.0 / (1.0 + age_days)
        return row["use_count"] * FREQUENCY_WEIGHT + recency * RECENCY_WEIGHT

    return sorted(rows, key=score, reverse=True)


def touch_memory(conn: sqlite3.Connection, memory_id: int) -> None:
    """记录一次使用，为后续按使用频率排序提供依据。"""
    conn.execute(
        "UPDATE memory SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
        (time.time(), memory_id),
    )
    conn.commit()


def add_episode(
    conn: sqlite3.Connection,
    session_id: str,
    task: str,
    summary: str,
    outcome: str,
) -> int:
    """写入一条历史事件。"""
    cursor = conn.execute(
        "INSERT INTO episode(session_id, task, summary, outcome, created_at)"
        " VALUES (?,?,?,?,?)",
        (session_id, task, summary, outcome, time.time()),
    )
    conn.commit()
    return cursor.lastrowid


def search_episodes(
    conn: sqlite3.Connection, query: str, limit: int = 5
) -> list[sqlite3.Row]:
    """按关键词检索历史事件。

    这里用 LIKE 而不是 FTS5：事件表规模小，而且 LIKE 对中文天然是子串匹配，
    不需要像 FTS5 那样先把中文逐字拆开。简单方案在这个规模上更可靠。
    """
    if not query.strip():
        return []
    needle = f"%{query.strip()}%"
    return conn.execute(
        "SELECT task, summary, outcome, created_at FROM episode"
        " WHERE task LIKE ? OR summary LIKE ?"
        " ORDER BY id DESC LIMIT ?",
        (needle, needle, limit),
    ).fetchall()


def record_session(
    conn: sqlite3.Connection,
    session_id: str,
    project_root: str,
    model: str = "",
    context_limit: int = 0,
) -> None:
    """登记会话，并在首次创建时记下它绑定到哪个工作区。

    绑定要显式记录，不能只靠「数据碰巧放在这个目录下」。一旦以后把记忆
    挪到共享存储，路径约定就失效了，而那时才发现没有任何东西知道
    某个会话属于哪个工作区。
    """
    now = time.time()
    row = conn.execute(
        "SELECT project_root FROM session WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO session"
            "(id, project_root, model, context_limit, created_at, last_active)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, project_root, model, context_limit, now, now),
        )
    else:
        conn.execute(
            "UPDATE session SET model = ?, context_limit = ?, last_active = ?"
            " WHERE id = ?",
            (model, context_limit, now, session_id),
        )
    conn.commit()


def get_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    """读取会话记录。"""
    return conn.execute(
        "SELECT * FROM session WHERE id = ?", (session_id,)
    ).fetchone()


def list_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """列出全部会话，最近活跃的在前。"""
    return conn.execute(
        "SELECT * FROM session ORDER BY last_active DESC"
    ).fetchall()


def check_binding(
    conn: sqlite3.Connection, session_id: str, project_root: str
) -> str | None:
    """会话绑定的工作区与当前工作区不一致时给出警告说明。

    这不是安全问题（记忆本来就按工作区分开存），而是防止搞混：
    同一个会话名在两个工作区里跑，事件会混进同一条 session 记录，
    「上次做到哪」就不可信了。
    """
    row = get_session(conn, session_id)
    if row is None:
        return None
    if Path(row["project_root"]) == Path(project_root):
        return None
    return (
        f"会话 {session_id} 之前绑定在 {row['project_root']}，"
        f"当前工作区是 {project_root}。建议换个会话名，否则事件会混在一起。"
    )
