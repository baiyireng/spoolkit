import json
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.registry import ToolRegistry


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "完成", "tool_calls": [], "state": None, "final": final},
        ensure_ascii=False,
    )


def _loop(tmp_path: Path, prefetch=None) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    return AgentLoop(
        gateway=FakeModel(script=[_turn("好了")], tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=ToolRegistry(),
        config=Config(project_root=tmp_path, context_window=4096),
        prefetch=prefetch,
    )


def test_预取内容出现在首轮请求里(tmp_path: Path) -> None:
    loop = _loop(tmp_path, prefetch=lambda goal: "parser.py:\n  def parse_config(path)")
    loop.run("修 parse_config")
    first = loop.gateway.requests[0]
    assert any("parse_config" in m.content for m in first.messages)


def test_未提供预取时行为不变(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    result = loop.run("随便")
    assert result.finished is True
    assert not any("parser.py" in m.content for m in loop.gateway.requests[0].messages)


def test_预取返回空串时不产生多余区段(tmp_path: Path) -> None:
    loop = _loop(tmp_path, prefetch=lambda goal: "")
    loop.run("随便")
    assert len(loop.gateway.requests[0].messages) >= 1

