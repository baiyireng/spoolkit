from pathlib import Path

from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.memory.session import MemorySession
from agents_dev.memory.store import add_memory, init_memory_schema
from agents_dev.memory.tools import recall_spec
from agents_dev.store.db import open_db


def _session(tmp_path: Path) -> MemorySession:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return MemorySession(
        conn=conn,
        hot_path=tmp_path / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=8192,
    )


def test_检索到历史记忆(tmp_path: Path) -> None:
    session = _session(tmp_path)
    add_memory(session._conn, kind="failure", text="整文件重解析太慢", source="ep#3")
    result = recall_spec(session).handler({"query": "重解析"})
    assert result.ok is True
    assert "整文件重解析太慢" in result.content


def test_没有命中时明确说没找到(tmp_path: Path) -> None:
    result = recall_spec(_session(tmp_path)).handler({"query": "完全无关的词"})
    assert result.ok is True
    assert "没有找到" in result.content


def test_数量上限参数被校验(tmp_path: Path) -> None:
    assert recall_spec(_session(tmp_path)).handler({"query": "x", "limit": 0}).ok is False


def test_工具描述说明用途(tmp_path: Path) -> None:
    spec = recall_spec(_session(tmp_path))
    assert spec.name == "recall"
    assert "检索" in spec.description

