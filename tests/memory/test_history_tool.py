"""`search_history` 工具的接线与边界。

工具是模型唯一看得见的那一层：查询为空、数量越界、时间格式错、没命中，
都必须变成**说人话的结果**（而不是异常或空串）——小模型拿不到可用出口时
会开始编。
"""

from pathlib import Path

import pytest

from spoolkit.cli.runtime import LoopWiring, assemble_loop
from spoolkit.config import Config
from spoolkit.kernel import sessions as kernel_sessions
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.session import MemorySession
from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.tools import search_history_spec
from spoolkit.memory.transcript import ASSISTANT, USER, record_message
from spoolkit.store.db import open_db
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
SNIPPET = 240


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    return root


def _spec(root: Path, snippet_chars: int = SNIPPET) -> tuple:
    conn = open_db(root / ".agent" / "memory.db")
    init_memory_schema(conn)
    session = MemorySession(
        conn=conn,
        hot_path=root / ".agent" / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=8192,
    )
    spec = search_history_spec(
        session,
        root,
        default_limit=DEFAULT_LIMIT,
        max_limit=MAX_LIMIT,
        snippet_chars=snippet_chars,
    )
    return spec, conn


def test_工具名与说明(tmp_path: Path) -> None:
    spec, conn = _spec(_root(tmp_path))
    assert spec.name == "search_history"
    assert spec.group == "记"  # 和 recall 同一类：过去的事
    assert spec.brief == "检索历史消息"
    # 说明里必须点出「历史默认不进上下文」：模型的工具表只有这一行可见。
    assert "历史" in spec.description and "出处" in spec.description
    assert set(spec.parameters["properties"]) == {
        "query",
        "limit",
        "session",
        "since",
        "until",
    }
    assert spec.parameters["required"] == ["query"]
    assert spec.parameters["additionalProperties"] is False
    conn.close()


def test_检索到另一个会话的消息并给出处(tmp_path: Path) -> None:
    root = _root(tmp_path)
    spec, conn = _spec(root)
    record_message(conn, "上次排查", USER, "归档那条命令为什么这么写")
    record_message(conn, "现在", USER, "接着说")
    result = spec.handler({"query": "归档"})
    assert result.ok is True
    assert "上次排查" in result.content
    assert "你" in result.content
    assert "命中第" in result.content
    conn.close()


def test_会话标题进出处(tmp_path: Path) -> None:
    """标题在 meta.json 里，不在库里：出处置要有它，人才认得出是哪条会话。"""
    root = _root(tmp_path)
    spec, conn = _spec(root)
    record_message(conn, "alpha", USER, "归档要加锁")
    kernel_sessions.write_meta(root, "alpha", title="修 calc.py", created_at=1.0)
    assert "修 calc.py" in spec.handler({"query": "归档"}).content
    conn.close()


def test_没命中时明说没找到(tmp_path: Path) -> None:
    spec, conn = _spec(_root(tmp_path))
    record_message(conn, "s", USER, "无关的话")
    result = spec.handler({"query": "完全无关的词"})
    assert result.ok is True
    assert "没有找到" in result.content
    assert result.content.strip() != ""
    conn.close()


def test_空查询被拒(tmp_path: Path) -> None:
    spec, conn = _spec(_root(tmp_path))
    result = spec.handler({"query": "   "})
    assert result.ok is False
    assert "query" in result.content
    conn.close()


def test_limit_小于一被拒(tmp_path: Path) -> None:
    spec, conn = _spec(_root(tmp_path))
    assert spec.handler({"query": "x", "limit": 0}).ok is False
    conn.close()


def test_limit_超上限被拒并说明上限(tmp_path: Path) -> None:
    """上限不是小气：一次多给几条，历史就整段进了上下文。"""
    spec, conn = _spec(_root(tmp_path))
    result = spec.handler({"query": "x", "limit": MAX_LIMIT + 1})
    assert result.ok is False
    assert str(MAX_LIMIT) in result.content
    conn.close()


def test_片段长度标定值坏掉时给说法(tmp_path: Path) -> None:
    """标定值是可覆盖的：写成 0 时要报错，而不是给一堆空片段。"""
    spec, conn = _spec(_root(tmp_path), snippet_chars=0)
    result = spec.handler({"query": "x"})
    assert result.ok is False
    assert "片段" in result.content
    conn.close()


def test_limit_在范围内按它给(tmp_path: Path) -> None:
    spec, conn = _spec(_root(tmp_path))
    for index in range(6):
        record_message(conn, "s", USER, f"归档任务{index}")
    result = spec.handler({"query": "归档", "limit": 2})
    assert result.content.count("\n- ") == 2
    conn.close()


def test_时间格式错时给人话(tmp_path: Path) -> None:
    spec, conn = _spec(_root(tmp_path))
    result = spec.handler({"query": "归档", "since": "前天"})
    assert result.ok is False
    assert "前天" in result.content and "2026-09-27" in result.content
    conn.close()


def test_只看一条会话(tmp_path: Path) -> None:
    spec, conn = _spec(_root(tmp_path))
    record_message(conn, "a", USER, "归档要加锁")
    record_message(conn, "b", USER, "归档已经加锁了")
    result = spec.handler({"query": "归档", "session": "a"})
    assert "a" in result.content and "已经加锁" not in result.content
    conn.close()


def test_没命中时不提别的会话的命中(tmp_path: Path) -> None:
    spec, conn = _spec(_root(tmp_path))
    record_message(conn, "a", USER, "归档要加锁")
    result = spec.handler({"query": "归档", "session": "b"})
    assert "没有找到" in result.content
    conn.close()


def test_密钥不进结果(tmp_path: Path) -> None:
    spec, conn = _spec(_root(tmp_path))
    record_message(conn, "s", USER, "QQ_AppSecret=abcdef123456 先放着")
    content = spec.handler({"query": "QQ_AppSecret"}).content
    assert "abcdef123456" not in content
    conn.close()


def test_注册表的参数校验仍然管用(tmp_path: Path) -> None:
    """schema 是闸门：编出来的参数不该进到检索里。"""
    root = _root(tmp_path)
    spec, conn = _spec(root)
    registry = ToolRegistry()
    registry.register(spec)
    assert registry.invoke(ToolCall("search_history", {})).ok is False
    assert registry.invoke(ToolCall("search_history", {"query": "x", "nope": 1})).ok is False
    assert registry.invoke(ToolCall("search_history", {"query": "x"})).ok is True
    conn.close()


def test_装配后工具在注册表与提示里(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """接线断言：挂上记忆就必须有这条路，且提示词里提得到它。"""
    root = _root(tmp_path)
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(tmp_path / "missing.toml"))
    conn = open_db(root / ".agent" / "memory.db")
    init_memory_schema(conn)
    session = MemorySession(
        conn=conn,
        hot_path=root / ".agent" / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=4096,
        session_id="new",
    )
    loop = assemble_loop(
        root,
        FakeModel(script=[], tokenizer=OfflineTokenCounter()),
        config=Config(project_root=root, context_window=4096),
        wiring=LoopWiring(memory=session, mcp=False),
    )
    assert "search_history" in loop.registry.names()
    index = loop.registry.describe()
    assert "search_history(query,limit,session,since,until)" in index
    assert "检索历史消息" in index
    conn.close()
