import json
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.memory.hot import write_hot
from agents_dev.memory.session import MemorySession
from agents_dev.memory.store import init_memory_schema
from agents_dev.store.db import open_db
from agents_dev.tools.registry import ToolRegistry


def _turn(final: str, done: bool = True) -> str:
    return json.dumps(
        {
            "thought": "结束",
            "tool_calls": [],
            "state": None,
            "done": done,
            "final": final,
        },
        ensure_ascii=False,
    )


def _memory(tmp_path: Path, window: int = 4096) -> MemorySession:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return MemorySession(
        conn=conn,
        hot_path=tmp_path / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=window,
        session_id="s1",
    )


def _loop(tmp_path: Path, script: list[str], memory=None, distiller=None) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=ToolRegistry(),
        config=Config(project_root=tmp_path, context_window=4096),
        memory=memory,
        distiller=distiller,
    )


def test_热记忆出现在首轮请求里(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    write_hot(tmp_path / "memory.md", {"fact": ["测试命令是 pytest -q"]})
    loop = _loop(tmp_path, [_turn("好")], memory=memory)
    loop.run("随便")
    first = loop.gateway.requests[0]
    assert any("pytest -q" in m.content for m in first.messages)


def test_不提供记忆时行为不变(tmp_path: Path) -> None:
    result = _loop(tmp_path, [_turn("好")]).run("随便")
    assert result.finished is True


def test_任务完成后写入事件(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    _loop(tmp_path, [_turn("好")], memory=memory).run("做完这件事")
    rows = memory._conn.execute("SELECT task, outcome FROM episode").fetchall()
    assert rows[0]["task"] == "做完这件事"
    assert rows[0]["outcome"] == "success"


def test_达到步数上限时记为失败(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    loop = _loop(tmp_path, ["不是 JSON"] * 3, memory=memory)
    loop.config = Config(project_root=tmp_path, context_window=4096, max_steps=2)
    loop.run("做不完的事")
    rows = memory._conn.execute("SELECT outcome FROM episode").fetchall()
    assert rows[0]["outcome"] == "fail"


def test_归纳结果被提升进热记忆(tmp_path: Path) -> None:
    memory = _memory(tmp_path)

    def distiller(state, final):
        return [("fact", "这是归纳出来的事实")]

    _loop(tmp_path, [_turn("好")], memory=memory, distiller=distiller).run("任务")
    assert "这是归纳出来的事实" in memory.hot_text()


def test_归纳器抛异常不影响任务收尾(tmp_path: Path) -> None:
    memory = _memory(tmp_path)

    def broken(state, final):
        raise RuntimeError("归纳失败")

    loop = _loop(tmp_path, [_turn("好")], memory=memory, distiller=broken)
    assert loop.run("任务").finished is True
