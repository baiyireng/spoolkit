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


def test_状态块被应用且完成后清理检查点(tmp_path: Path) -> None:
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
    # 检查点只在任务进行中保留；成功后它属于过程状态，应当被清掉，
    # 否则下一次运行会误以为还有活没干完。
    assert not (tmp_path / ".agent" / "tasks" / "task.json").exists()


def test_达到步数上限会停止(tmp_path: Path) -> None:
    # 必须用真实的工具调用：空回合现在会被当成「卡住」提前收尾，
    # 那是另一条路径，测不到步数上限。
    # 督导关掉，测的才是「上限本身」——开着的话撞上限要先问督导，
    # 那是 tests/agent/test_supervise_loop.py 的事。
    script = [
        _turn(f"第{i}步", [{"name": "list_dir", "arguments": {"path": "."}}])
        for i in range(20)
    ]
    loop = _build(tmp_path, script, max_steps=3, supervise=False)
    result = loop.run("没完没了")
    assert result.finished is False
    assert result.steps == 3
    assert result.final == "已达步数上限，任务未完成"


def test_服务端说提示词超长时丢掉历史重发(tmp_path: Path) -> None:
    """本地用估算分词判定放行、服务端用真实分词拒绝，这会发生。

    实测那条拒绝曾经变成未捕获异常，直接把整个运行打断——一次上下文
    估算偏差不该让任务崩掉。
    """
    from agents_dev.errors import ContextOverflowError
    from agents_dev.llm.gateway import ModelGateway
    from agents_dev.llm.types import ChatResponse

    class OverflowOnce:
        """第一次说超长，第二次正常。"""

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, request) -> ChatResponse:
            self.calls += 1
            if self.calls == 1:
                raise ContextOverflowError("提示词超过服务端上下文上限")
            return ChatResponse(
                text=json.dumps(
                    {
                        "thought": "重发之后正常了",
                        "tool_calls": [],
                        "state": None,
                        "done": True,
                        "final": "好",
                    },
                    ensure_ascii=False,
                ),
                prompt_tokens=10,
                completion_tokens=5,
            )

    gateway = OverflowOnce()
    loop = AgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        tokenizer=OfflineTokenCounter(),
        registry=ToolRegistry(),
        config=Config(project_root=tmp_path, context_window=4096, max_steps=5),
    )
    result = loop.run("做点事")
    assert result.finished is True, "超长只该让它重发一次，不该让运行崩掉"
    assert gateway.calls == 2
    assert any("提示词超长" in line for line in result.trace)


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
