import json
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.registry import ToolRegistry


def _turn(final: str | None) -> str:
    return json.dumps(
        {
            "thought": "t",
            "tool_calls": [],
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _loop(tmp_path: Path, script: list[str], lessons=None) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=ToolRegistry(),
        config=Config(project_root=tmp_path, context_window=4096, max_steps=4),
        lessons=lessons,
    )


def test_推送的教训出现在首轮提示里(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [_turn("完成")], lessons=lambda goal: [(7, "先上语法约束")])
    loop.run("修解析逻辑")
    first = loop.gateway.requests[0]
    assert any("先上语法约束" in m.content for m in first.messages)


def test_没有教训时不产生空标题(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [_turn("完成")], lessons=lambda goal: [])
    loop.run("修解析逻辑")
    first = loop.gateway.requests[0]
    assert not any("踩过的坑" in m.content for m in first.messages)


def test_不提供教训来源时行为不变(tmp_path: Path) -> None:
    result = _loop(tmp_path, [_turn("完成")]).run("随便")
    assert result.finished is True
    assert result.lessons_pushed == ()


def test_结果里带回被推送的教训编号(tmp_path: Path) -> None:
    loop = _loop(
        tmp_path, [_turn("完成")], lessons=lambda goal: [(3, "甲"), (9, "乙")]
    )
    result = loop.run("任务")
    assert result.lessons_pushed == (3, 9)


def test_任务未完成时同样带回编号(tmp_path: Path) -> None:
    loop = _loop(
        tmp_path, ["不是 JSON"] * 6, lessons=lambda goal: [(5, "丙")]
    )
    result = loop.run("做不完")
    assert result.finished is False
    assert result.lessons_pushed == (5,)


def test_推送内容带上来源说明(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [_turn("完成")], lessons=lambda goal: [(1, "规则")])
    loop.run("任务")
    text = "\n".join(m.content for m in loop.gateway.requests[0].messages)
    # 点明这是历史教训而不是当前要求，否则模型会把坑当约束照搬
    assert "过去类似任务里踩过的坑" in text


def test_轨迹里记录推送了几条(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [_turn("完成")], lessons=lambda goal: [(1, "规则甲")])
    result = loop.run("任务")
    assert any("推送 1 条历史教训" in line for line in result.trace)


def test_没有推送时不产生轨迹噪音(tmp_path: Path) -> None:
    result = _loop(tmp_path, [_turn("完成")], lessons=lambda goal: []).run("任务")
    assert not any("历史教训" in line for line in result.trace)
