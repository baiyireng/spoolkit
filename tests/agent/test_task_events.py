"""循环发出 TaskEvent。旧的 tool 事件还在，事件本身不进提示。"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult, ToolSpec
from spoolkit.web.protocol import TASK_EVENT, parse_line


def _turn(calls, final=None, thought="秘密思考") -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls,
            "state": None,
            "done": final is not None,
            "final": final or "",
        },
        ensure_ascii=False,
    )


def _spec(name: str, ok: bool, summary: str, raw: str) -> ToolSpec:
    def handler(_args: dict) -> ToolResult:
        return ToolResult(ok=ok, content=f"{name}正文", summary=summary, raw=raw)

    return ToolSpec(
        name=name,
        description="测试",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        brief="测试",
    )


def _loop(tmp_path: Path, script: list[str], events: list, **config) -> tuple[AgentLoop, PendingChanges]:
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(_spec("look", True, "看过了", "RAW_LOOK"))
    registry.register(_spec("miss", False, "没找到", "RAW_MISS"))
    tokenizer = OfflineTokenCounter()
    options = {
        "project_root": tmp_path,
        "context_window": 4096,
        "max_steps": 4,
        "supervise": False,
    }
    options.update(config)
    loop = AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=registry,
        config=Config(**options),
        pending=pending,
        prefetch=lambda _goal: "预取到的目录说明",
        on_event=lambda kind, data: events.append((kind, dict(data))),
    )
    return loop, pending


def test_一轮工具事件序号相位和预取(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    loop, pending = _loop(
        tmp_path,
        [
            _turn([
                {"name": "look", "arguments": {}},
                {"name": "miss", "arguments": {}},
            ]),
            _turn([], final="完成"),
        ],
        events,
    )
    result = loop.run("看一下", task_id="job-42")
    assert result.finished is True

    task_events = [data for kind, data in events if kind == TASK_EVENT]
    tools = [data for kind, data in events if kind == "tool"]
    assert tools[0]["name"] == "look" and tools[0]["ok"] is True
    assert tools[1]["ok"] is False

    assert [item["seq"] for item in task_events] == [1, 2, 3]
    assert task_events[0]["phase"] == "retrieval"
    assert task_events[0]["source"] == "prefetch"
    assert task_events[0]["status"] == "ok"
    assert "预取" in task_events[0]["summary"]

    assert task_events[1]["phase"] == "observe"
    assert task_events[1]["source"] == "tool"
    assert task_events[1]["status"] == "ok"
    assert task_events[1]["tool_call_id"] == "tc-0-0"
    assert task_events[1]["summary"] == "看过了"
    assert task_events[1]["step"] == 0

    assert task_events[2]["phase"] == "error"
    assert task_events[2]["source"] == "tool"
    assert task_events[2]["status"] == "error"
    assert task_events[2]["tool_call_id"] == "tc-0-1"
    assert task_events[2]["summary"] == "没找到"

    for item in task_events:
        assert item["task_id"] == "job-42"
        assert item["run_id"] == pending.run_id
        assert item["turn_id"] not in ("", "-", "task")
        assert "thought" not in item
        assert "raw" not in item
        assert "RAW_LOOK" not in json.dumps(item, ensure_ascii=False)
        assert "RAW_MISS" not in json.dumps(item, ensure_ascii=False)
        assert "秘密思考" not in json.dumps(item, ensure_ascii=False)

    prompt = "\n".join(
        message.content
        for request in loop.gateway.requests
        for message in request.messages
    )
    assert "tc-0-0" not in prompt
    assert "tc-0-1" not in prompt
    assert "RAW_LOOK" not in prompt
    assert "RAW_MISS" not in prompt
    line = parse_line(json.dumps({"type": TASK_EVENT, **task_events[1]}, ensure_ascii=False))
    assert line is not None and line.type == TASK_EVENT


def test_未完成的收尾发error且序号继续(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    loop, _pending = _loop(
        tmp_path,
        [_turn([])],
        events,
        max_steps=1,
    )
    result = loop.run("做不完", task_id="job-7")
    assert result.finished is False
    task_events = [data for kind, data in events if kind == TASK_EVENT]
    assert [item["seq"] for item in task_events] == list(range(1, len(task_events) + 1))
    final = task_events[-1]
    assert final["phase"] == "error"
    assert final["source"] == "system"
    assert final["status"] == "error"
    assert "未完成" in final["summary"] or "步数上限" in final["summary"]
    assert "thought" not in final


def test_异常出口收成error事件(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    loop, _pending = _loop(tmp_path, [], events)
    try:
        loop.run("会炸", task_id="job-9")
    except RuntimeError as exc:
        assert "脚本已耗尽" in str(exc)
    else:
        raise AssertionError("假模型应该把异常抛出来")
    task_events = [data for kind, data in events if kind == TASK_EVENT]
    assert task_events[-1]["phase"] == "error"
    assert task_events[-1]["source"] == "system"
    assert "RuntimeError" in task_events[-1]["summary"]
    assert "thought" not in task_events[-1]
    assert "raw" not in task_events[-1]
