"""结构版本与步进升级（设计稿 §9.3 第 1、3、5 步 / §4.3 第 3 块）。

两条纪律在这里被钉死：

1. **`PRAGMA user_version` 步进、幂等**：纯 `CREATE TABLE IF NOT EXISTS`
   给既有表加不了列，所以升级必须按版本号一步一步来，且同一步重复跑
   不许报 `duplicate column name`。
2. **`memory` 表一个字节不动**：老规矩是「`memory.db` 表结构一个字不改」
   （决议 7），`episode` 只加列加索引，`memory_fts` 的重建在版本 2。
"""

import sqlite3
from pathlib import Path

import pytest

from spoolkit.memory import store
from spoolkit.memory.store import (
    MEMORY_SCHEMA,
    SCHEMA_VERSION,
    MemorySchemaError,
    add_episode,
    add_memory,
    init_memory_schema,
    search_memories,
)
from spoolkit.store.db import open_db

# 改造前 `memory` 表的建表语句（逐字冻结）。SQLite 存进 `sqlite_master` 时会
# 去掉 `IF NOT EXISTS`、去掉结尾分号，所以这里按它**落库后**的样子写。
FROZEN_MEMORY_TABLE_SQL = "\n".join(
    (
        "CREATE TABLE memory (",
        "    id            INTEGER PRIMARY KEY,",
        "    kind          TEXT NOT NULL,",
        "    text          TEXT NOT NULL,",
        "    trigger       TEXT NOT NULL DEFAULT '',",
        "    source        TEXT NOT NULL DEFAULT '',",
        "    confidence    REAL NOT NULL DEFAULT 0.5,",
        "    created_at    REAL NOT NULL,",
        "    last_used_at  REAL,",
        "    use_count     INTEGER NOT NULL DEFAULT 0,",
        "    applied_count INTEGER NOT NULL DEFAULT 0,",
        "    success_count INTEGER NOT NULL DEFAULT 0",
        ")",
    )
)

FROZEN_MEMORY_COLUMNS = (
    "id",
    "kind",
    "text",
    "trigger",
    "source",
    "confidence",
    "created_at",
    "last_used_at",
    "use_count",
    "applied_count",
    "success_count",
)

EPISODE_NEW_COLUMNS = ("scope", "scope_id", "verified", "verify_detail", "archived")
EPISODE_NEW_INDEXES = ("idx_episode_session", "idx_episode_created")
FTS_COLUMNS = ("text", "kind", "trigger", "source")


def _old_db(tmp_path: Path) -> sqlite3.Connection:
    """用**改造前**的 `MEMORY_SCHEMA` 建一个 `user_version=0` 的旧库。"""
    conn = open_db(tmp_path / "memory.db")
    conn.executescript(MEMORY_SCHEMA)
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row["name"] for row in conn.execute(f"PRAGMA table_info({table})"))


def _schema_lines(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (row["type"], row["name"], row["sql"] or "")
        for row in conn.execute("SELECT type, name, sql FROM sqlite_master")
        if not row["name"].startswith("sqlite_")
    ]


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


# ---- 任务 4：版本步进 ---------------------------------------------------


def test_旧库升级后版本号与新增列就位(tmp_path: Path) -> None:
    conn = _old_db(tmp_path)
    # 先说清 fixture 真的是旧库：改造前的 episode 没有这 5 列。
    assert not set(EPISODE_NEW_COLUMNS) & set(_columns(conn, "episode"))
    add_episode(conn, "s1", "查天气", "查天气：已完成", "success")

    init_memory_schema(conn)

    assert _user_version(conn) == SCHEMA_VERSION
    assert set(EPISODE_NEW_COLUMNS) <= set(_columns(conn, "episode"))
    indexes = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert set(EPISODE_NEW_INDEXES) <= indexes
    row = conn.execute(
        "SELECT scope, scope_id, verified, verify_detail, archived FROM episode"
    ).fetchone()
    # 旧行取默认值：既有数据没有「验证」这个概念，默认即未验证。
    assert (row["scope"], row["scope_id"], row["verified"]) == ("session", "", 0)
    assert (row["verify_detail"], row["archived"]) == ("", 0)
    conn.close()


def test_幂等_连跑两次结构与行数都不变(tmp_path: Path) -> None:
    conn = _old_db(tmp_path)
    add_episode(conn, "s1", "任务", "摘要", "success")
    init_memory_schema(conn)
    first = (_columns(conn, "episode"), _columns(conn, "memory_fts"), _user_version(conn))
    episode_rows = conn.execute("SELECT count(*) FROM episode").fetchone()[0]

    init_memory_schema(conn)  # 再跑一次：不重复加列、不报错
    init_memory_schema(conn)

    assert (
        _columns(conn, "episode"),
        _columns(conn, "memory_fts"),
        _user_version(conn),
    ) == first
    assert conn.execute("SELECT count(*) FROM episode").fetchone()[0] == episode_rows
    conn.close()


def test_版本被人为退回时同一步骤也不重复加列(tmp_path: Path) -> None:
    """把 user_version 手工退回 0（模拟"步骤跑完但版本号没记上"）→ 不报错。"""
    conn = _old_db(tmp_path)
    init_memory_schema(conn)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()

    init_memory_schema(conn)

    assert _user_version(conn) == SCHEMA_VERSION
    assert set(EPISODE_NEW_COLUMNS) <= set(_columns(conn, "episode"))
    assert _columns(conn, "memory_fts") == FTS_COLUMNS
    conn.close()


def test_新库一步到位得到同一结构(tmp_path: Path) -> None:
    upgraded = _old_db(tmp_path / "old")
    init_memory_schema(upgraded)
    fresh = open_db(tmp_path / "fresh" / "memory.db")
    init_memory_schema(fresh)

    assert sorted(_schema_lines(upgraded)) == sorted(_schema_lines(fresh))
    assert _user_version(fresh) == SCHEMA_VERSION
    assert set(EPISODE_NEW_COLUMNS) <= set(_columns(fresh, "episode"))
    upgraded.close()
    fresh.close()


def test_memory_表的DDL逐字不变(tmp_path: Path) -> None:
    conn = _old_db(tmp_path / "old")
    init_memory_schema(conn)
    add_memory(conn, kind="fact", text="测试命令是 pytest -q")

    # ① 库里落下去的建表语句与改造前逐字一致。
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory'"
    ).fetchone()[0]
    assert sql == FROZEN_MEMORY_TABLE_SQL
    # ② 列清单与顺序也一致（加列、改名、删列都会在这里红）。
    assert _columns(conn, "memory") == FROZEN_MEMORY_COLUMNS
    # ③ 常量本身也没被改：MEMORY_SCHEMA 里那段与冻结文本只差 IF NOT EXISTS。
    block = MEMORY_SCHEMA.split("CREATE TABLE IF NOT EXISTS memory", 1)[1]
    block = "CREATE TABLE memory" + block.split("\n);", 1)[0] + "\n)"
    assert block == FROZEN_MEMORY_TABLE_SQL
    # ④ 新库里也一样。
    fresh = open_db(tmp_path / "fresh" / "memory.db")
    init_memory_schema(fresh)
    assert (
        fresh.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory'"
        ).fetchone()[0]
        == FROZEN_MEMORY_TABLE_SQL
    )
    conn.close()
    fresh.close()


def test_前向兼容_库比代码新时不动库也不报错(tmp_path: Path, capsys) -> None:
    conn = _old_db(tmp_path)
    init_memory_schema(conn)
    # 造一个"库比代码新"的现场：删掉一个索引 + 把版本调到更高。
    conn.execute("DROP INDEX idx_episode_created")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()

    init_memory_schema(conn)

    indexes = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_episode_created" not in indexes  # 一步都没跑
    assert _user_version(conn) == SCHEMA_VERSION + 1  # 版本号没被写回低值
    out = capsys.readouterr().out
    assert "高于" in out and "跳过升级" in out
    conn.close()


def test_升级失败给的是人话而不是裸异常(tmp_path: Path, monkeypatch) -> None:
    conn = open_db(tmp_path / "memory.db")

    def boom(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("模拟的磁盘错误")

    monkeypatch.setattr(
        store, "_MIGRATIONS", store._MIGRATIONS + ((3, "坏步骤", boom),)
    )
    monkeypatch.setattr(store, "SCHEMA_VERSION", 3)

    with pytest.raises(MemorySchemaError) as excinfo:
        init_memory_schema(conn)

    message = str(excinfo.value)
    assert "记忆库结构升级失败" in message
    assert "第 3 步" in message and "坏步骤" in message
    assert "模拟的磁盘错误" in message
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    # 失败的那一步不落版本号；前面的步骤已经记上，重跑不会重复加列。
    assert _user_version(conn) == 2
    # 库还在、还能读。
    assert _columns(conn, "episode")
    conn.close()


# ---- 任务 5：FTS 四列与回填 ---------------------------------------------


def test_memory_fts_是四列且不用触发器(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    assert _columns(conn, "memory_fts") == FTS_COLUMNS
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
    ).fetchone()[0]
    assert "fts5(text, kind, trigger, source)" in sql
    assert "content=" not in sql  # 保持非 external content
    triggers = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'")
    assert triggers.fetchone()[0] == 0  # 应用层显式索引，不用触发器
    conn.close()


def test_旧库回填后既有行可按新字段检索(tmp_path: Path) -> None:
    conn = _old_db(tmp_path)
    # 模拟改造前的写入：教训只进主表、完全不进 FTS（缺陷 4 的现场）。
    conn.execute(
        "INSERT INTO memory(kind, text, trigger, source, confidence, created_at)"
        " VALUES ('lesson','解析自定义格式先上语法约束','解析,正则,格式','ep#12',0.5,1.0)"
    )
    lesson_id = conn.execute("SELECT id FROM memory").fetchone()[0]
    # 事实走改造前的 add_memory 路径：主表 + 只按 text 建索引。
    conn.execute(
        "INSERT INTO memory(kind, text, trigger, source, confidence, created_at)"
        " VALUES ('fact','索引层用 ast 提取符号','','',0.5,1.0)"
    )
    fact_id = conn.execute("SELECT max(id) FROM memory").fetchone()[0]
    conn.execute(
        "INSERT INTO memory_fts(rowid, text) VALUES (?, ?)",
        (fact_id, "索引层用 ast 提取符号"),
    )
    conn.commit()

    # 升级前：新字段一律查不到（这就是缺陷 4）。
    assert search_memories(conn, "正则") == []
    assert search_memories(conn, "ep#12") == []
    assert search_memories(conn, "fact") == []

    init_memory_schema(conn)

    assert [item.id for item in search_memories(conn, "正则")] == [lesson_id]
    assert [item.id for item in search_memories(conn, "ep#12")] == [lesson_id]
    assert [item.id for item in search_memories(conn, "lesson")] == [lesson_id]
    assert [item.id for item in search_memories(conn, "fact")] == [fact_id]
    assert [item.id for item in search_memories(conn, "ast")] == [fact_id]
    # 回填完整：索引行数与主表行数一致。
    assert conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 2

    # 回填也幂等。
    init_memory_schema(conn)
    assert conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 2
    conn.close()
