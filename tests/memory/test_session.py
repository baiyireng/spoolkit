from pathlib import Path

from agents_dev.agent.state import TaskState
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.memory.hot import write_hot
from agents_dev.memory.session import MemorySession
from agents_dev.memory.store import add_memory, init_memory_schema
from agents_dev.store.db import open_db


def _session(tmp_path: Path, window: int = 8000) -> MemorySession:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return MemorySession(
        conn=conn,
        hot_path=tmp_path / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=window,
        session_id="s1",
    )


def test_没有热记忆时返回空串(tmp_path: Path) -> None:
    assert _session(tmp_path).hot_text() == ""


def test_热记忆被渲染出来(tmp_path: Path) -> None:
    session = _session(tmp_path)
    write_hot(tmp_path / "memory.md", {"fact": ["测试命令是 pytest -q"]})
    text = session.hot_text()
    assert "项目事实" in text
    assert "pytest -q" in text


def test_冷记忆召回附来源(tmp_path: Path) -> None:
    session = _session(tmp_path)
    add_memory(session._conn, kind="failure", text="整文件重解析太慢", source="ep#3")
    recalled = session.recall("重解析")
    assert "整文件重解析太慢" in recalled
    assert "ep#3" in recalled


def test_召回无命中返回空串(tmp_path: Path) -> None:
    assert _session(tmp_path).recall("完全无关的词") == ""


def test_归档会更新热记忆文件(tmp_path: Path) -> None:
    session = _session(tmp_path)
    state = TaskState(task_id="t", goal="目标", done=["做了事"], excluded=["坏方案"])
    result = session.archive(state, promote=[("fact", "事实一")])
    assert result.episode_id > 0
    assert "事实一" in session.hot_text()


def test_归档后可按关键词召回失败记录(tmp_path: Path) -> None:
    session = _session(tmp_path)
    state = TaskState(task_id="t", goal="目标", excluded=["整文件重解析"])
    session.archive(state)
    assert "整文件重解析" in session.recall("重解析")

