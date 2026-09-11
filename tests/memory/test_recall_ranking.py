from pathlib import Path

from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.memory.session import MemorySession
from agents_dev.memory.store import (
    add_memory,
    init_memory_schema,
    search_memories,
    touch_memory,
)
from agents_dev.store.db import open_db


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def _session(tmp_path: Path, conn) -> MemorySession:
    return MemorySession(
        conn=conn,
        hot_path=tmp_path / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=8192,
    )


def test_新记忆默认使用次数为零(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = add_memory(conn, kind="fact", text="测试命令是 pytest")
    assert item.use_count == 0
    conn.close()


def test_记录使用会累加次数并更新时间(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = add_memory(conn, kind="fact", text="规则")
    touch_memory(conn, item.id)
    touch_memory(conn, item.id)
    row = conn.execute(
        "SELECT use_count, last_used_at FROM memory WHERE id = ?", (item.id,)
    ).fetchone()
    assert row["use_count"] == 2
    assert row["last_used_at"] is not None
    conn.close()


def test_用过更多的排在前面(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rarely = add_memory(conn, kind="fact", text="解析器要加超时")
    often = add_memory(conn, kind="fact", text="解析器要加超时保护")
    for _ in range(5):
        touch_memory(conn, often.id)
    order = [item.id for item in search_memories(conn, "解析器")]
    assert order[0] == often.id
    assert rarely.id in order
    conn.close()


def test_召回会记录使用(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = add_memory(conn, kind="fact", text="解析器要加超时")
    session = _session(tmp_path, conn)
    session.recall("解析器")
    session.recall("解析器")
    row = conn.execute(
        "SELECT use_count FROM memory WHERE id = ?", (item.id,)
    ).fetchone()
    assert row["use_count"] == 2
    conn.close()


def test_没召回到的记忆不被计数(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = add_memory(conn, kind="fact", text="完全无关的内容")
    _session(tmp_path, conn).recall("解析器")
    row = conn.execute(
        "SELECT use_count FROM memory WHERE id = ?", (item.id,)
    ).fetchone()
    assert row["use_count"] == 0
    conn.close()


def test_打分权重可解释() -> None:
    from agents_dev.memory.store import FREQUENCY_WEIGHT, RECENCY_WEIGHT

    assert FREQUENCY_WEIGHT + RECENCY_WEIGHT == 1.0

