"""P3-2：v2 的 done 走终稿二次请求；旧协议仍用 JSON final，不发第二次。"""

import json
from pathlib import Path

from spoolkit.agent.final_turn import FINAL_INSTRUCTION, PREFIX_GROWTH_LIMIT, prefix_growth
from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.context.phase_budget import compute_budget
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.registry import ToolRegistry


def _v2_done() -> str:
    return json.dumps(
        {
            "thought": "可以收了",
            "tool_calls": [],
            "done": True,
            "summary": "无需外部信息。结论已经齐了。",
        },
        ensure_ascii=False,
    )


def _old_done() -> str:
    return json.dumps(
        {
            "thought": "可以收了",
            "tool_calls": [],
            "state": None,
            "done": True,
            "final": "写完了",
        },
        ensure_ascii=False,
    )


_MARKDOWN = "# 说明\n\n这是终稿正文。"


def _loop(tmp_path: Path, script: list[str], *, v2: bool, events: list) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    return AgentLoop(
        gateway=FakeModel(script, tokenizer),
        tokenizer=tokenizer,
        registry=ToolRegistry(),
        config=Config(
            project_root=tmp_path,
            context_window=8192,
            max_steps=2,
            supervise=False,
            overrides={"protocol_v2": 1 if v2 else 0},
        ),
        on_event=lambda kind, data: events.append((kind, dict(data))),
    )


def test_v2完成时复制前缀再请求终稿(tmp_path: Path) -> None:
    events: list = []
    loop = _loop(tmp_path, [_v2_done(), _MARKDOWN], v2=True, events=events)
    result = loop.run("把数字说清楚")
    gateway = loop.gateway
    assert len(gateway.requests) == 2
    control, final = gateway.requests
    assert final.messages[:-1] == control.messages
    assert final.messages[-1].role == "user"
    assert final.messages[-1].content == FINAL_INSTRUCTION
    assert final.response_schema is None
    assert final.cache_prompt is True
    assert control.response_schema is not None
    measured = sum(loop.tokenizer.count(item.content) for item in control.messages)
    assert final.max_tokens == compute_budget(
        n_ctx=8192,
        prompt_tokens=measured,
        phase="final",
    ).max_tokens
    assert final.max_tokens <= 2500
    growth = prefix_growth(
        sum(loop.tokenizer.count(item.content) for item in control.messages),
        sum(loop.tokenizer.count(item.content) for item in final.messages),
    )
    assert 0 < growth <= PREFIX_GROWTH_LIMIT
    assert "这是终稿正文" in result.final
    assert "\\n" not in result.final
    streamed = "".join(data["text"] for kind, data in events if kind == "final_delta")
    assert streamed == _MARKDOWN
    assert not any(kind == "delta" and "final" in data.get("text", "") for kind, data in events)


def test_旧协议完成时不发终稿二次请求(tmp_path: Path) -> None:
    events: list = []
    loop = _loop(tmp_path, [_old_done()], v2=False, events=events)
    result = loop.run("把数字说清楚")
    assert len(loop.gateway.requests) == 1
    assert loop.gateway.requests[0].response_schema is not None
    assert not any(
        item.content == FINAL_INSTRUCTION
        for item in loop.gateway.requests[0].messages
    )
    assert "写完了" in result.final
    assert not any(kind == "final_delta" for kind, _data in events)
