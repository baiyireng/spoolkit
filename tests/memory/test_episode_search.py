from pathlib import Path

from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.memory.session import MemorySession
from agents_dev.memory.store import add_episode, init_memory_schema, search_episodes
from agents_dev.store.db import open_db


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def test_按任务名检索事件(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_episode(conn, "s", "给索引加增量更新", "完成，改了 indexer.py", "success")
    hits = search_episodes(conn, "增量更新")
    assert len(hits) == 1
    assert hits[0]["outcome"] == "success"
    conn.close()


def test_按摘要检索事件(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_episode(conn, "s", "修一个 bug", "问题出在编码上", "success")
    assert len(search_episodes(conn, "编码")) == 1
    conn.close()


def test_没有命中时返回空(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_episode(conn, "s", "任务", "摘要", "success")
    assert search_episodes(conn, "完全无关") == []
    conn.close()


def test_空查询返回空(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_episode(conn, "s", "任务", "摘要", "success")
    assert search_episodes(conn, "  ") == []
    conn.close()


def test_结果按时间倒序(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_episode(conn, "s", "第一次归档", "旧的", "success")
    add_episode(conn, "s", "第二次归档", "新的", "success")
    hits = search_episodes(conn, "归档")
    assert hits[0]["task"] == "第二次归档"
    conn.close()


def test_数量受上限约束(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for i in range(8):
        add_episode(conn, "s", f"归档任务{i}", "摘要", "success")
    assert len(search_episodes(conn, "归档", limit=3)) == 3
    conn.close()


def test_召回合并记忆与事件(tmp_path: Path) -> None:
    from agents_dev.memory.store import add_memory

    conn = _conn(tmp_path)
    add_memory(conn, kind="failure", text="整文件重解析太慢", source="ep#3")
    add_episode(conn, "s", "给 parser 加增量更新", "完成", "success")

    session = MemorySession(
        conn=conn,
        hot_path=tmp_path / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=8192,
    )
    text = session.recall("重解析")
    assert "[failure]" in text

    merged = session.recall("增量更新")
    assert "[事件]" in merged
    conn.close()

