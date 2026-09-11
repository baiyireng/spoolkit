import pytest

from agents_dev.context.assembler import Assembler
from agents_dev.context.budget import Budget
from agents_dev.context.sections import Section
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.llm.types import Message


def _asm(window: int = 1000) -> Assembler:
    return Assembler(tokenizer=OfflineTokenCounter(), budget=Budget(window=window))


def test_按优先级顺序拼接区段() -> None:
    result = _asm().assemble(
        [
            Section(name="task_state", text="状态", priority=20),
            Section(name="system", text="系统提示", priority=10),
        ]
    )
    assert result.messages[0].content == "系统提示"
    assert result.messages[1].content == "状态"


def test_普通区段超配额被裁剪并记录() -> None:
    result = _asm(window=1000).assemble(
        [Section(name="code", text="x" * 4000, priority=50)]
    )
    assert "code" in result.dropped
    assert result.total_tokens <= 1000


def test_需求体积反映裁剪前的量() -> None:
    result = _asm(window=1000).assemble(
        [Section(name="code", text="x" * 4000, priority=50)]
    )
    assert result.demand_tokens > result.total_tokens


def test_需求体积计入全部历史轮次() -> None:
    turns = [Message(role="user", content="z" * 4000) for _ in range(10)]
    result = _asm(window=1000).assemble([], recent_turns=turns)
    assert result.demand_tokens == 10000


def test_必留区段超限时报错() -> None:
    with pytest.raises(ValueError):
        _asm(window=100).assemble(
            [Section(name="system", text="y" * 5000, priority=1, mandatory=True)]
        )


def test_最近轮次从旧到新排列() -> None:
    turns = [Message(role="user", content=f"第{i}轮") for i in range(1, 4)]
    result = _asm(window=1000).assemble(
        [Section(name="system", text="S", priority=1)], recent_turns=turns
    )
    contents = [m.content for m in result.messages if m.role == "user"]
    assert contents == ["第1轮", "第2轮", "第3轮"]


def test_余量不足时丢弃更旧的轮次() -> None:
    turns = [Message(role="user", content="z" * 400) for _ in range(10)]
    result = _asm(window=1000).assemble([], recent_turns=turns)
    assert 0 < len(result.messages) < 10
    assert result.messages[-1].content == "z" * 400


def test_装配结果报告占用比例() -> None:
    result = _asm(window=1000).assemble(
        [Section(name="system", text="abc", priority=1)]
    )
    assert 0 < result.ratio < 1

