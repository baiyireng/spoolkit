from pathlib import Path

from agents_dev.memory.store import init_memory_schema
from agents_dev.memory.transcript import (
    ASSISTANT,
    USER,
    recent_messages,
    record_message,
    render_transcript,
)
from agents_dev.store.db import open_db


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def test_记录并按时间取回(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "问一句")
    record_message(conn, "s", ASSISTANT, "答一句")
    rows = recent_messages(conn, "s")
    assert [row["content"] for row in rows] == ["问一句", "答一句"]
    conn.close()


def test_会话之间互相隔离(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "a", USER, "属于 a")
    record_message(conn, "b", USER, "属于 b")
    rows = recent_messages(conn, "a")
    assert [row["content"] for row in rows] == ["属于 a"]
    conn.close()


def test_只取最近若干条且按正序返回(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for i in range(10):
        record_message(conn, "s", USER, f"第{i}条")
    rows = recent_messages(conn, "s", limit=3)
    assert [row["content"] for row in rows] == ["第7条", "第8条", "第9条"]
    conn.close()


def test_元信息随消息一起保存(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", ASSISTANT, "做完了", meta="3 步，输入 1200 token")
    rows = recent_messages(conn, "s")
    assert "3 步" in rows[0]["meta"]
    conn.close()


def test_渲染包含角色与内容(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "给项目加个功能")
    record_message(conn, "s", ASSISTANT, "已完成")
    text = render_transcript(recent_messages(conn, "s"))
    assert "你：" in text
    assert "给项目加个功能" in text
    assert "助手：" in text
    conn.close()


def test_空记录有明确提示(tmp_path: Path) -> None:
    assert "还没有历史" in render_transcript([])


def test_内容首尾空白被去掉(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "  有空白  ")
    assert recent_messages(conn, "s")[0]["content"] == "有空白"
    conn.close()

