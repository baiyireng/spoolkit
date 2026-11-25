from pathlib import Path

from spoolkit.memory.store import (
    Memory,
    add_episode,
    add_memory,
    init_memory_schema,
    list_memories,
    normalize_for_fts,
    search_episodes,
    search_memories,
)
from spoolkit.store.db import open_db


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


def test_中文部分词也能召回(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_memory(conn, kind="fact", text="厦门近两天约 28°C")
    hits = search_memories(conn, "刚才厦门多少度")
    assert any("28" in item.text for item in hits)
    conn.close()


def test_事件摘要能按结论数字搜到(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_episode(conn, "s1", "查天气", "查天气：已完成 无记录\n结论：厦门近两天约 28°C。", "success")
    rows = search_episodes(conn, "厦门 温度")
    assert rows
    assert "28" in rows[0]["summary"]
    conn.close()


def test_按类型可检索(tmp_path: Path) -> None:
    """`kind` 进索引（设计稿 §2.2 / 缺陷 4）：查 `fact` 能捞到事实类条目。"""
    conn = _conn(tmp_path)
    add_memory(conn, kind="fact", text="索引层用 ast 提取符号")
    add_memory(conn, kind="preference", text="提交信息用中文写")
    hits = search_memories(conn, "fact")
    assert [item.text for item in hits] == ["索引层用 ast 提取符号"]
    conn.close()


def test_按触发词可检索(tmp_path: Path) -> None:
    """`trigger` 进索引：教训的触发词查得到（除非它能被检索，否则永远推不出来）。"""
    conn = _conn(tmp_path)
    add_memory(
        conn, kind="lesson", text="先上语法约束", trigger="解析,正则,格式", source="ep#7"
    )
    hits = search_memories(conn, "正则")
    assert [item.text for item in hits] == ["先上语法约束"]
    conn.close()


def test_按来源可检索(tmp_path: Path) -> None:
    """`source` 进索引：`ep#12` 能回溯到那一条。"""
    conn = _conn(tmp_path)
    add_memory(conn, kind="fact", text="渲染层不碰语义", source="ep#12")
    hits = search_memories(conn, "ep#12")
    assert [item.text for item in hits] == ["渲染层不碰语义"]
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
