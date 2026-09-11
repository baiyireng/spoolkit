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

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT 'project',
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

CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind, scope);
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
    scope: str = "project",
) -> Memory:
    """写入一条记忆，并同步维护检索索引。"""
    cursor = conn.execute(
        "INSERT INTO memory"
        "(kind, scope, text, trigger, source, confidence, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (kind, scope, text, trigger, source, confidence, time.time()),
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
    """关键词检索。查询为空或全是停用符号时返回空列表。"""
    if not query.strip():
        return []
    try:
        rows = conn.execute(
            "SELECT m.* FROM memory_fts f JOIN memory m ON m.id = f.rowid"
            " WHERE memory_fts MATCH ? ORDER BY m.id",
            (_phrase_query(query),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_to_memory(row) for row in rows]


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

