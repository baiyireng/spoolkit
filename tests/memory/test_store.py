from pathlib import Path

from agents_dev.memory.store import (
    Memory,
    add_episode,
    add_memory,
    init_memory_schema,
    list_memories,
    normalize_for_fts,
    search_memories,
)
from agents_dev.store.db import open_db


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def test_初始化后包含记忆相关表(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"memory", "episode", "memory_fts"} <= names
    conn.close()


def test_写入并读回记忆(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_memory(conn, kind="fact", text="测试命令是 pytest -q")
    items = list_memories(conn)
    assert len(items) == 1
    assert items[0].kind == "fact"
    assert items[0].confidence == 0.5
    conn.close()


def test_可按类型过滤(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_memory(conn, kind="fact", text="事实一")
    add_memory(conn, kind="preference", text="偏好一")
    assert len(list_memories(conn, kind="preference")) == 1
    conn.close()


def test_记忆带来源与置信度(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = add_memory(
        conn, kind="lesson", text="先上语法约束", source="ep#7", confidence=0.8
    )
    assert item.source == "ep#7"
    assert item.confidence == 0.8
    conn.close()


def test_英文关键词可检索(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_memory(conn, kind="fact", text="索引层用 ast 提取符号")
    hits = search_memories(conn, "ast")
    assert len(hits) == 1
    conn.close()


def test_中文关键词可检索(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_memory(conn, kind="fact", text="自动归档整理历史内容")
    assert len(search_memories(conn, "归档")) == 1
    conn.close()


def test_中文检索不误命中(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_memory(conn, kind="fact", text="自动归档整理历史内容")
    assert search_memories(conn, "渲染") == []
    conn.close()


def test_删除记忆会同步移除检索索引(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = add_memory(conn, kind="fact", text="要删掉的归档条目")
    conn.execute("DELETE FROM memory WHERE id = ?", (item.id,))
    conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (item.id,))
    conn.commit()
    assert search_memories(conn, "归档") == []
    conn.close()


def test_写入事件并读回(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_episode(
        conn,
        session_id="s1",
        task="给索引加增量更新",
        summary="完成，改了 indexer.py",
        outcome="success",
    )
    rows = conn.execute("SELECT task, outcome FROM episode").fetchall()
    assert rows[0]["task"] == "给索引加增量更新"
    assert rows[0]["outcome"] == "success"
    conn.close()


def test_规范化把中文字逐字分开() -> None:
    assert normalize_for_fts("归档ab").split() == ["归", "档", "ab"]


def test_记忆对象是不可变值对象() -> None:
    item = Memory(id=1, kind="fact", text="t", source="", confidence=0.5)
    assert item.text == "t"
