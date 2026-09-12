"""输出被预算截断时的处置。

原先的反馈是「请只输出规定的 JSON」——那是在纠形状，而真正的毛病是
**这一轮说得太多**。模型照做重来一遍还是那么多，于是同样被截断：
实测一条会话里连撞两次，最后督导据此收手。

所以这里断言两件事：预算被抬高（记在这次任务里），以及反馈在告诉它
「把这一轮拆小」。
"""

import json
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.context.budget import OUTPUT_RESERVE_BOOST_RATIO, Budget
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.fs import list_dir_spec
from agents_dev.tools.registry import ToolRegistry


def _turn(thought: str, calls=None, final=None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _build(tmp_path: Path, script: list, **kwargs) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path))
    settings = {"context_window": 8192, "max_steps": 4, "supervise": False, **kwargs}
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, **settings),
    )


def test_被截断时预算抬高并且要它拆小(tmp_path: Path) -> None:
    truncated = '{"thought":"写一大段","tool_calls":[{"name":"list_dir","arguments":{"path":"."'
    loop = _build(
        tmp_path,
        [truncated, _turn("改小一点", [], final="好了")],
    )
    before = loop._budget.output_reserve()
    result = loop.run("做点什么")

    # 预算抬高了，而且是记在这次任务里
    assert loop._budget.output_reserve() > before
    assert loop._budget.output_reserve() == int(8192 * OUTPUT_RESERVE_BOOST_RATIO)
    assert any("输出被截断" in line for line in result.trace)

    # 抬高的同时要它把这一轮拆小——只抬预算的话它会原样再来一次
    second = loop.gateway.requests[1]
    text = "\n".join(message.content for message in second.messages)
    assert "拆小" in text
    assert str(loop._budget.output_reserve()) in text


def test_输出格式错误时不抬预算(tmp_path: Path) -> None:
    """这轮坏了和「说得太多」是两件事，别把格式错误也当成预算问题。"""
    loop = _build(tmp_path, ["这不是 JSON", _turn("好了", [], final="好了")])
    before = loop._budget.output_reserve()
    loop.run("做点什么")
    assert loop._budget.output_reserve() == before


def test_预算只抬一次且封顶() -> None:
    budget = Budget(window=8192)
    assert budget.output_reserve() == int(8192 * 0.15)
    assert budget.boost_output() == int(8192 * OUTPUT_RESERVE_BOOST_RATIO)
    # 再抬一次不会继续涨：抬的是输出的钱，付的是输入的预算
    assert budget.boost_output() == int(8192 * OUTPUT_RESERVE_BOOST_RATIO)


def test_抬高输出预算会压缩可装配的输入预算() -> None:
    """这条是代价，写下来免得以后只看见好处。"""
    budget = Budget(window=8192)
    before = budget.effective()
    budget.boost_output()
    assert budget.effective() < before
