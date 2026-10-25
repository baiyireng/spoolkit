"""一次运行的任务身份，以及 understand / verify / final。"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop, event_task_id
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult, ToolSpec
from spoolkit.web.protocol import TASK_EVENT, Event
from spoolkit.web.runner import Runner


def _turn(calls=None, final: str = "") -> str:
    return json.dumps(
        {
            "thought": "做",
            "tool_calls": calls or [],
            "done": bool(final),
            "final": final,
        },
        ensure_ascii=False,
    )


def _collect(loop: AgentLoop, goal: str) -> list[dict]:
    found: list[dict] = []
    loop.on_event = lambda kind, data: found.append(dict(data)) if kind == TASK_EVENT else None
    loop.run(goal)
    return found


def _loop(tmp_path: Path, script: list[str], *, verify=None, prefetch=None) -> tuple[AgentLoop, PendingChanges]:
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(write_file_spec(tmp_path, pending))
    tokenizer = OfflineTokenCounter()
    loop = AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=8192,
            max_steps=4,
            supervise=False,
        ),
        pending=pending,
        verify=verify,
        prefetch=prefetch,
    )
    return loop, pending


def test_两次运行身份不同且同一次共用(tmp_path: Path) -> None:
    loop, pending = _loop(
        tmp_path,
        [_turn(final="第一次完成"), _turn(final="第二次完成")],
    )
    # 假模型脚本是共享的：两次 run 各吃一条终稿。
    first = _collect(loop, "第一次")
    second = _collect(loop, "第二次")
    assert first[0]["phase"] == "understand"
    assert first[0]["status"] == "running"
    assert first[0]["seq"] == 1
    assert first[0]["step"] == 0
    assert "第一次" in first[0]["summary"]
    assert first[-1]["phase"] == "final"
    assert first[-1]["status"] == "ok"
    assert {item["task_id"] for item in first} == {first[0]["task_id"]}
    assert {item["task_id"] for item in second} == {second[0]["task_id"]}
    assert first[0]["task_id"] != second[0]["task_id"]
    assert first[0]["task_id"] != "task"
    assert first[0]["run_id"] == second[0]["run_id"] == pending.run_id
    assert first[0]["task_id"] == event_task_id(pending.run_id, first[0]["turn_id"])
    assert second[0]["task_id"] == event_task_id(pending.run_id, second[0]["turn_id"])


def test_验证与预取工具共用身份且final在最后(tmp_path: Path) -> None:
    def verify(_changed: list[str]) -> ToolResult:
        return ToolResult(ok=True, content="没有发现问题", raw="RAW_VERIFY_OK")

    loop, _pending = _loop(
        tmp_path,
        [
            _turn([{"name": "write_file", "arguments": {"path": "a.py", "content": "x = 1\n"}}]),
            _turn(final="写好了"),
        ],
        verify=verify,
        prefetch=lambda _goal: "预取目录",
    )
    events = _collect(loop, "写一个文件")
    phases = [item["phase"] for item in events]
    assert phases[0] == "understand"
    assert phases.index("retrieval") > 0
    assert phases.index("observe") > phases.index("retrieval")
    assert "verify" in phases
    assert phases[-1] == "final"
    verify_event = next(item for item in events if item["phase"] == "verify")
    assert verify_event["source"] == "system"
    assert verify_event["status"] == "ok"
    assert "没有发现问题" in verify_event["summary"]
    assert "RAW_VERIFY_OK" not in json.dumps(verify_event, ensure_ascii=False)
    assert len({item["task_id"] for item in events}) == 1
    assert events[-1]["status"] == "ok"
    assert "写好了" in events[-1]["summary"]


def test_验证失败是error且没有raw(tmp_path: Path) -> None:
    def verify(_changed: list[str]) -> ToolResult:
        return ToolResult(ok=False, content="编译失败", raw="RAW_VERIFY_SECRET")

    loop, _pending = _loop(
        tmp_path,
        [
            _turn([{"name": "write_file", "arguments": {"path": "a.py", "content": "x = 1\n"}}]),
            _turn(final="写坏了"),
        ],
        verify=verify,
    )
    events = _collect(loop, "写一个文件")
    verify_event = next(item for item in events if item["phase"] == "verify")
    assert verify_event["status"] == "error"
    assert "编译失败" in verify_event["summary"]
    assert "RAW_VERIFY_SECRET" not in json.dumps(verify_event, ensure_ascii=False)


def test_未完成和取消不重复final与error(tmp_path: Path) -> None:
    loop, _pending = _loop(tmp_path, [_turn()], prefetch=lambda _goal: "预取")
    loop.config = Config(
        project_root=tmp_path,
        context_window=8192,
        max_steps=1,
        supervise=False,
    )
    failed = _collect(loop, "做不完")
    assert failed[-1]["phase"] == "error"
    assert "final" not in {item["phase"] for item in failed}

    stopped = {"loop": None}

    def handler(_args: dict) -> ToolResult:
        stopped["loop"]._user_stopped = True
        return ToolResult(ok=True, content="停", summary="停了")

    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="halt",
            description="停",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=handler,
            brief="停",
        )
    )
    tokenizer = OfflineTokenCounter()
    halt = AgentLoop(
        gateway=FakeModel(
            script=[_turn([{"name": "halt", "arguments": {}}]), _turn(final="停下来")],
            tokenizer=tokenizer,
        ),
        tokenizer=tokenizer,
        registry=registry,
        config=Config(project_root=tmp_path, context_window=8192, max_steps=4, supervise=False),
        pending=pending,
    )
    stopped["loop"] = halt
    cancelled = _collect(halt, "停")
    phases = [item["phase"] for item in cancelled]
    assert phases[-1] == "final"
    assert cancelled[-1]["status"] == "cancelled"
    assert phases.count("final") == 1
    assert "error" not in phases


def test_恢复后task_id不变(tmp_path: Path) -> None:
    loop, _pending = _loop(tmp_path, [_turn(final="完成")])
    events = _collect(loop, "记下来")
    runner = Runner(tmp_path, session="life")
    for item in events:
        runner._absorb(Event(TASK_EVENT, item))
    restored = Runner(tmp_path, session="life").snapshot(health={"ok": True})["task_events"]
    assert [item["task_id"] for item in restored] == [item["task_id"] for item in events]
    assert restored[0]["phase"] == "understand"
    assert restored[-1]["phase"] == "final"
    assert len({item["task_id"] for item in restored}) == 1
