import json
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.fs import read_file_spec
from agents_dev.tools.fs import list_dir_spec
from agents_dev.tools.registry import ToolRegistry


def _turn(thought: str, calls=None, state=None, final=None, done=None) -> str:
    if done is None:
        done = final is not None
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": state,
            "done": done,
            "final": final,
        },
        ensure_ascii=False,
    )


def _build(tmp_path: Path, script: list, **config_kwargs) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(list_dir_spec(tmp_path))
    settings = {"context_window": 4096, **config_kwargs}
    config = Config(project_root=tmp_path, **settings)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=config,
    )


def test_调用工具后给出最终答复(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("内容ABC\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读文件", [{"name": "read_file", "arguments": {"path": "a.txt"}}]),
            _turn("看到了内容", [], final="文件里有 内容ABC"),
        ],
    )
    result = loop.run("看看 a.txt 里有什么")
    assert result.finished is True
    assert "内容ABC" in result.final
    assert result.steps == 2


def test_工具结果被喂回模型(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("标记内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读文件", [{"name": "read_file", "arguments": {"path": "a.txt"}}]),
            _turn("完成", [], final="好"),
        ],
    )
    loop.run("读文件")
    second = loop.gateway.requests[1]
    assert any("标记内容" in m.content for m in second.messages)


def test_解析失败时把原因回灌并继续(tmp_path: Path) -> None:
    loop = _build(tmp_path, ["这不是 JSON", _turn("改正了", [], final="好了")])
    result = loop.run("做点什么")
    assert result.finished is True
    second = loop.gateway.requests[1]
    assert any("JSON" in m.content for m in second.messages)


def test_状态块被应用并落盘(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn(
                "开始",
                [{"name": "read_file", "arguments": {"path": "a.txt"}}],
                state={"current": "正在分析"},
            ),
            _turn("结束", [], final="完成"),
        ],
    )
    result = loop.run("分析一下")
    assert result.state.current == "正在分析"
    assert (tmp_path / ".agent" / "tasks" / "task.json").exists()


def test_达到步数上限会停止(tmp_path: Path) -> None:
    loop = _build(tmp_path, [_turn(f"第{i}步") for i in range(20)], max_steps=3)
    result = loop.run("没完没了")
    assert result.finished is False
    assert result.steps == 3


def test_上下文需求过大时触发重置(tmp_path: Path) -> None:
    long_state = {"current": "很长的当前状态" * 200}
    script = [
        _turn(
            f"步骤{i}",
            [{"name": "list_dir", "arguments": {"path": "."}}],
            state=long_state,
        )
        for i in range(12)
    ]
    loop = _build(tmp_path, script, context_window=2000)
    result = loop.run("把上下文撑爆")
    assert result.resets >= 1


def test_轨迹记录每一步(tmp_path: Path) -> None:
    result = _build(tmp_path, [_turn("结束", [], final="好")]).run("简单任务")
    assert len(result.trace) >= 1
