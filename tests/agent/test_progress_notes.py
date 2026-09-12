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


def test_状态块只列最近几条但记录不丢(tmp_path: Path) -> None:
    """块里只列最近几条（它每轮都注入，是每个请求的税），但**记录不能丢**。

    实测一条会话做完 20 道题，收尾时它报「还有 17 道没做」——因为它只看得见
    块里那 8 条。所以：全量留在状态与清单文件里，块里给总数 + 指针。
    """
    script = [
        _turn([_call("write_file", path=f"f{i}.py", content="x")])
        for i in range(MAX_DONE_NOTES + 4)
    ]
    script.append(_turn([], final="完成"))
    result = _loop(tmp_path, script, max_steps=len(script)).run("写很多文件")

    total = MAX_DONE_NOTES + 4
    # 记录是全的
    assert len(result.state.done) == total
    assert any("f0.py" in item for item in result.state.done)
    # 块里只列最近几条，但说清了总数与完整清单在哪
    block = result.state.render()
    assert f"已完成 {total} 项" in block
    assert f"只列最近 {MAX_DONE_NOTES} 项" in block
    assert ".agent/progress.md" in block
    assert "f0.py" not in block
    assert f"f{total - 1}.py" in block
    # 清单落盘，模型需要细节时可以读
    progress = (tmp_path / ".agent" / "progress.md").read_text(encoding="utf-8")
    assert "f0.py" in progress and f"f{total - 1}.py" in progress


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
