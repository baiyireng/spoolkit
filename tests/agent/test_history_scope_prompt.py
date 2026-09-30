"""历史检索的作用域口径必须**真的到达模型**（Task 3）。

`build_workflow` 是一条派生路径：提示词里写没写、写了什么，只有把装配好的
提示词抓下来看才算数。所以这里跑一次真实的主循环（假模型），从它收到的
`ChatRequest` 里读系统提示——顺带钉住「跨会话要显式」这条不会因为改别处而丢。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.session import MemorySession
from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.tools import recall_spec, search_history_spec
from spoolkit.store.db import open_db
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "收尾", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _loop(tmp_path: Path) -> AgentLoop:
    conn = open_db(tmp_path / "m.db")
    init_memory_schema(conn)
    session = MemorySession(
        conn=conn,
        hot_path=tmp_path / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=4096,
        session_id="现在",
    )
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(recall_spec(session))
    registry.register(
        search_history_spec(
            session,
            tmp_path,
            default_limit=5,
            max_limit=20,
            snippet_chars=240,
        )
    )
    gateway = FakeModel(script=[_turn("好")], tokenizer=OfflineTokenCounter())
    return AgentLoop(
        gateway=gateway,
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, context_window=4096),
        memory=session,
    )


def test_作用域口径进到系统提示(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.run("读一下 a.py")
    prompt = "\n".join(m.content for m in loop.gateway.requests[0].messages)
    # 默认只查当前会话，跨会话必须显式——这条不写进提示，模型就不知道。
    assert "默认只查当前会话" in prompt
    assert 'session="*"' in prompt
    assert "来自多条会话" in prompt or "标着来源会话" in prompt
    # 指代/承接那几条规则也在（原文里的关键词）。
    for mark in ("那个", "上次", "刚才", "继续", "它", "前面提到的", "还是按之前那样"):
        assert mark in prompt
    assert "没找到就直说没找到" in prompt
    # 「关键词取自当前问题里的实体」——否则它会把整句话当查询，命中率归零。
    assert "当前问题里的实体" in prompt
