"""tool_call_id 从循环传到待确认改动和 diff/await/confirm。"""

import io
import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.cli.commands.run import _events_approver
from spoolkit.cli.events import EventWriter, settle_with_events
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.policy import ASK
from spoolkit.tools.call_context import current_tool_call_id, tool_call_context
from spoolkit.tools.edit import PendingChanges, replace_lines_spec
from spoolkit.tools.exec import run_command_spec
from spoolkit.tools.registry import ToolRegistry


class _Lines:
    def __init__(self, *lines: str) -> None:
        self._lines = list(lines)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


def _edit(content: str, expect: str) -> dict:
    return {
        "name": "replace_lines",
        "arguments": {
            "path": "a.py",
            "start_line": 1,
            "end_line": 1,
            "content": content,
            "expect": expect,
        },
    }


def _turn(calls: list[dict], final: str = "") -> str:
    return json.dumps(
        {
            "thought": "改一行",
            "tool_calls": calls,
            "done": final != "",
            "final": final,
        },
        ensure_ascii=False,
    )


def _loop(tmp_path: Path, pending: PendingChanges, script: list[str], registry: ToolRegistry) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    return AgentLoop(
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
    )


def _events(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_replace_lines的call_id进diff和确认(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(replace_lines_spec(tmp_path, pending))
    loop = _loop(tmp_path, pending, [_turn([_edit("x = 2\n", "x = 1")]), _turn([], "改完了")], registry)
    result = loop.run("改 a.py", task_id="job-edit")
    assert result.finished is True
    assert current_tool_call_id() == ""

    change = pending.items()[0]
    assert change.call_id == "tc-0-0"

    buffer = io.StringIO()
    settle_with_events(pending, ASK, (), None, EventWriter(buffer), io.StringIO("y\n"))
    lines = _events(buffer)
    diff = next(line for line in lines if line["type"] == "diff")
    waiting = next(line for line in lines if line["type"] == "await")
    confirm = next(line for line in lines if line["type"] == "confirm")
    assert diff["tool_call_id"] == change.call_id == "tc-0-0"
    assert waiting["count"] == 1
    assert waiting["tool_call_ids"] == ["tc-0-0"]
    assert confirm["tool_call_ids"] == ["tc-0-0"]
    assert confirm["applied"] is True


def test_同一路径连续编辑的diff不串(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    captured: list = []
    original = pending.propose

    def spy(path: str, new_text: str, declared: str = ""):
        change = original(path, new_text, declared)
        captured.append(change)
        return change

    pending.propose = spy
    registry = ToolRegistry()
    registry.register(replace_lines_spec(tmp_path, pending))
    loop = _loop(
        tmp_path,
        pending,
        [
            _turn([_edit("x = 2\n", "x = 1")]),
            _turn([_edit("x = 3\n", "x = 2")]),
            _turn([], "改完了"),
        ],
        registry,
    )
    loop.run("连改两次", task_id="job-twice")
    assert [change.call_id for change in captured] == ["tc-0-0", "tc-1-0"]
    assert pending.items()[0].call_id == "tc-1-0"
    assert current_tool_call_id() == ""

    seen: list[str] = []
    for change in captured:
        box = PendingChanges(tmp_path)
        box._changes[change.path] = change
        buffer = io.StringIO()
        settle_with_events(box, ASK, (), None, EventWriter(buffer), io.StringIO("n\n"))
        diff = next(line for line in _events(buffer) if line["type"] == "diff")
        waiting = next(line for line in _events(buffer) if line["type"] == "await")
        assert diff["tool_call_id"] == change.call_id
        assert diff["tool_call_id"] in waiting["tool_call_ids"]
        seen.append(diff["tool_call_id"])
    assert seen == ["tc-0-0", "tc-1-0"]


def test_命令授权await带上这次run_command的id(tmp_path: Path) -> None:
    class _Writer:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def emit(self, kind: str, **data) -> None:
            self.events.append((kind, data))

    writer = _Writer()
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(
        run_command_spec(tmp_path, approver=_events_approver(writer, reader=_Lines("n")))
    )
    loop = _loop(
        tmp_path,
        pending,
        [
            _turn([{"name": "run_command", "arguments": {"command": ["find", "."]}}]),
            _turn([], "问过了"),
        ],
        registry,
    )
    loop.run("找文件", task_id="job-cmd")
    waiting = next(data for kind, data in writer.events if kind == "await")
    confirm = next(data for kind, data in writer.events if kind == "confirm")
    assert waiting["command"] == "find ."
    assert waiting["count"] == 1
    assert "需要你批准" in waiting["reason"]
    assert waiting["tool_call_id"] == "tc-0-0"
    assert confirm["tool_call_id"] == "tc-0-0"
    assert confirm["command"] == "find ."
    assert confirm["count"] == 1
    assert confirm["applied"] is False
    assert current_tool_call_id() == ""


def test_命令confirm不改读后来的call_id(monkeypatch) -> None:
    """confirm 用的是发出 await 时留下的 ID，不重新读当前上下文。"""
    answers = iter(("tc-0-0", "tc-9-9"))
    monkeypatch.setattr(
        "spoolkit.cli.commands.run.current_tool_call_id",
        lambda: next(answers),
    )

    class _Writer:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def emit(self, kind: str, **data) -> None:
            self.events.append((kind, data))

    writer = _Writer()
    approver = _events_approver(writer, reader=_Lines("y"))
    assert approver(["find", "."], "找文件") == "once"
    waiting = next(data for kind, data in writer.events if kind == "await")
    confirm = next(data for kind, data in writer.events if kind == "confirm")
    assert waiting["tool_call_id"] == "tc-0-0"
    assert confirm["tool_call_id"] == "tc-0-0"
    assert confirm["applied"] is True


def test_没有调用上下文时call_id留空(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path)
    change = pending.propose("a.py", "x = 1\n")
    assert change.call_id == ""
    with tool_call_context("tc-9-0"):
        marked = pending.propose("b.py", "y = 1\n")
    assert marked.call_id == "tc-9-0"
    assert current_tool_call_id() == ""
    buffer = io.StringIO()
    box = PendingChanges(tmp_path)
    box._changes[change.path] = change
    settle_with_events(box, ASK, (), None, EventWriter(buffer), io.StringIO("n\n"))
    diff = next(line for line in _events(buffer) if line["type"] == "diff")
    assert diff["tool_call_id"] == ""
