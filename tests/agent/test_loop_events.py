import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry


def _turn(calls, final=None) -> str:
    return json.dumps(
        {
            "thought": "t",
            "tool_calls": calls,
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _loop(tmp_path: Path, script: list[str], on_event=None) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=registry,
        config=Config(project_root=tmp_path, context_window=4096, max_steps=4),
        on_event=on_event,
    )


def test_每轮开始发出step事件(tmp_path: Path) -> None:
    seen: list[tuple[str, dict]] = []
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    _loop(
        tmp_path,
        [
            _turn([{"name": "read_file", "arguments": {"path": "a.txt"}}]),
            _turn([], final="完成"),
        ],
        on_event=lambda kind, data: seen.append((kind, data)),
    ).run("读文件")
    assert ("step", {"n": 0}) in seen
    assert ("step", {"n": 1}) in seen


def test_工具结束发出tool事件(tmp_path: Path) -> None:
    seen: list[tuple[str, dict]] = []
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    _loop(
        tmp_path,
        [
            _turn([{"name": "read_file", "arguments": {"path": "a.txt"}}]),
            _turn([], final="完成"),
        ],
        on_event=lambda kind, data: seen.append((kind, data)),
    ).run("读文件")
    tools = [data for kind, data in seen if kind == "tool"]
    assert tools[0]["name"] == "read_file"
    assert tools[0]["ok"] is True


def test_失败的工具也发事件(tmp_path: Path) -> None:
    seen: list[tuple[str, dict]] = []
    _loop(
        tmp_path,
        [
            _turn([{"name": "read_file", "arguments": {"path": "不存在.txt"}}]),
            _turn([], final="完成"),
        ],
        on_event=lambda kind, data: seen.append((kind, data)),
    ).run("读文件")
    assert [d for k, d in seen if k == "tool"][0]["ok"] is False


def test_不传回调时行为不变(tmp_path: Path) -> None:
    result = _loop(tmp_path, [_turn([], final="完成")]).run("随便")
    assert result.finished is True


def test_回调抛异常不影响任务(tmp_path: Path) -> None:
    """回调是壳挂上来的。它出错不能让任务崩——壳的问题不该毁掉内核的工作。"""

    def broken(kind, data):
        raise RuntimeError("壳挂了")

    result = _loop(tmp_path, [_turn([], final="完成")], on_event=broken).run("任务")
    assert result.finished is True

