import json
from pathlib import Path

from agents_dev.agent.loop import MAX_DONE_NOTES, AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.edit import PendingChanges, replace_lines_spec, write_file_spec
from agents_dev.tools.fs import read_file_spec
from agents_dev.tools.registry import ToolRegistry


def _call(name: str, **arguments) -> dict:
    return {"name": name, "arguments": arguments}


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


def _loop(tmp_path: Path, script: list[str], max_steps: int = 6) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    pending = PendingChanges(tmp_path)
    registry.register(write_file_spec(tmp_path, pending))
    registry.register(replace_lines_spec(tmp_path, pending))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=registry,
        config=Config(
            project_root=tmp_path, context_window=4096, max_steps=max_steps
        ),
    )


def test_写操作被记进状态(tmp_path: Path) -> None:
    loop = _loop(
        tmp_path,
        [
            _turn([_call("write_file", path="a.py", content="x = 1\n")]),
            _turn([], final="完成"),
        ],
    )
    result = loop.run("写个文件")
    assert any("write_file" in item for item in result.state.done)


def test_读操作不记进状态(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_turn([_call("read_file", path="a.py")]), _turn([], final="完成")],
    )
    result = loop.run("看看")
    assert result.state.done == []


def test_失败的操作不记进状态(tmp_path: Path) -> None:
    loop = _loop(
        tmp_path,
        [_turn([_call("read_file", path="不存在.py")]), _turn([], final="完成")],
    )
    result = loop.run("看看")
    assert result.state.done == []


def test_同一步重复写同一文件只记一次(tmp_path: Path) -> None:
    loop = _loop(
        tmp_path,
        [
            _turn(
                [
                    _call("write_file", path="a.py", content="1"),
                    _call("write_file", path="a.py", content="2"),
                ]
            ),
            _turn([], final="完成"),
        ],
    )
    result = loop.run("写两次")
    assert len([item for item in result.state.done if "a.py" in item]) == 1


def test_进度条数有上限(tmp_path: Path) -> None:
    script = [
        _turn([_call("write_file", path=f"f{i}.py", content="x")])
        for i in range(MAX_DONE_NOTES + 4)
    ]
    script.append(_turn([], final="完成"))
    result = _loop(tmp_path, script, max_steps=len(script)).run("写很多文件")
    assert len(result.state.done) == MAX_DONE_NOTES
    # 保留的是最近的
    assert any(f"f{MAX_DONE_NOTES + 3}.py" in item for item in result.state.done)


def test_进度写进检查点(tmp_path: Path) -> None:
    script = [
        _turn([_call("write_file", path="a.py", content="x")]),
        "不是 JSON",
    ]
    result = _loop(tmp_path, script + ["不是 JSON"] * 5).run("做一半")
    assert result.finished is False
    data = json.loads(
        (tmp_path / ".agent" / "tasks" / "task.json").read_text(encoding="utf-8")
    )
    assert any("write_file" in item for item in data["done"])


def test_模型自己填的进度不会被覆盖(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "thought": "t",
            "tool_calls": [_call("write_file", path="a.py", content="x")],
            "state": {"done_added": ["模型自己记的"], "current": "在写"},
            "done": False,
            "final": None,
        },
        ensure_ascii=False,
    )
    result = _loop(tmp_path, [raw, _turn([], final="完成")]).run("写")
    assert "模型自己记的" in result.state.done
    assert any("write_file" in item for item in result.state.done)
