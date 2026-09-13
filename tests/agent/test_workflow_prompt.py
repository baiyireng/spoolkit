from pathlib import Path

from spoolkit.agent.loop import build_workflow
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.session import MemorySession
from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.tools import recall_spec
from spoolkit.store.db import open_db
from spoolkit.tools.edit import PendingChanges, replace_lines_spec, write_file_spec
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.index.tools import find_callers_spec, find_symbol_spec


def _full_registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    pending = PendingChanges(tmp_path)
    registry.register(write_file_spec(tmp_path, pending))
    registry.register(replace_lines_spec(tmp_path, pending))
    return registry


def test_空注册表不提任何工具() -> None:
    text = build_workflow(ToolRegistry())
    for name in ("find_symbol", "find_callers", "replace_lines", "write_file", "recall"):
        assert name not in text


def test_有写工具时给出改动方式(tmp_path: Path) -> None:
    text = build_workflow(_full_registry(tmp_path))
    assert "replace_lines" in text
    assert "write_file" in text
    # 必须说清楚「不用等确认」。实测模型会把它读成「停下来等用户」，
    # 然后连发十几次空回合，把步数预算烧光。
    assert "不用停下来等确认" in text


def test_有索引工具时给出查符号优先规则(tmp_path: Path) -> None:
    from spoolkit.store.db import init_schema

    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    registry.register(find_symbol_spec(tmp_path, conn))
    registry.register(find_callers_spec(conn))
    text = build_workflow(registry)
    assert "要改的文件必须先看到它的当前内容" in text
    assert "看波及面" in text


def test_有写工具时才提动手改(tmp_path: Path) -> None:
    """只读角色看到「动手改」只会浪费步数去试它没有的工具。"""
    from spoolkit.store.db import init_schema

    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    registry.register(find_symbol_spec(tmp_path, conn))
    assert "信息够了就动手改" not in build_workflow(registry)

    registry.register(replace_lines_spec(tmp_path, PendingChanges(tmp_path)))
    assert "信息够了就动手改" in build_workflow(registry)


def test_只读角色看不到写工具(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    text = build_workflow(registry)
    assert "write_file" not in text
    assert "replace_lines" not in text


def test_注册recall后才提recall(tmp_path: Path) -> None:
    registry = _full_registry(tmp_path)
    assert "recall" not in build_workflow(registry)
    conn = open_db(tmp_path / "m.db")
    init_memory_schema(conn)
    session = MemorySession(
        conn=conn,
        hot_path=tmp_path / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=8192,
    )
    registry.register(recall_spec(session))
    assert "recall" in build_workflow(registry)
