"""SQLite 存储层。

索引数据放在独立的 .agent/index.db 中，可随时删除重建——
它是派生数据，不是事实来源。
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS file (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    lang         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mtime        REAL NOT NULL,
    indexed_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS symbol (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    parent_id  INTEGER REFERENCES symbol(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    signature  TEXT NOT NULL,
    doc        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_symbol_file ON symbol(file_id);
CREATE INDEX IF NOT EXISTS idx_symbol_name ON symbol(name);
CREATE INDEX IF NOT EXISTS idx_symbol_parent ON symbol(parent_id);

-- 引用边。dst_symbol_id 为空表示目标无法唯一确定——
-- 这比随便指一个符号安全得多：错误的「只有一个调用方」会让人放心地改下去。
CREATE TABLE IF NOT EXISTS ref (
    id            INTEGER PRIMARY KEY,
    src_symbol_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    dst_name      TEXT NOT NULL,
    dst_symbol_id INTEGER REFERENCES symbol(id) ON DELETE SET NULL,
    kind          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ref_src ON ref(src_symbol_id);
CREATE INDEX IF NOT EXISTS idx_ref_dst ON ref(dst_symbol_id);
CREATE INDEX IF NOT EXISTS idx_ref_name ON ref(dst_name);
"""


def open_db(path: Path) -> sqlite3.Connection:
    """打开（必要时创建）索引数据库。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """建表。可重复调用，不会破坏已有数据。"""
    conn.executescript(SCHEMA)
    conn.commit()
