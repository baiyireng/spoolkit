"""长文触发：结构线索不看题目长短。用户说短则不进。"""

import pytest

from spoolkit.agent.answer_plan import should_use_long_answer
from spoolkit.agent.long_regression import LONG_PROMPTS
from spoolkit.agent.protocol import AnswerPlan


def test_mode_long必进长文() -> None:
    plan = AnswerPlan(mode="long", target_tokens=10)
    assert should_use_long_answer(" hi ", plan) is True


def test_强线索即使模型报short也进长文() -> None:
    plan = AnswerPlan(mode="short", target_tokens=9000)
    assert should_use_long_answer("写一份完整方案", plan) is True


def test_强线索没有申报也进长文() -> None:
    assert should_use_long_answer("写一份完整方案", None) is True


def test_mode_short且没有结构线索不进() -> None:
    plan = AnswerPlan(mode="short", target_tokens=9000)
    assert should_use_long_answer("请看这段话。" + ("字" * 8000), plan) is False


def test_没有申报时长问句没有线索不进() -> None:
    assert should_use_long_answer("请看这段话。" + ("字" * 8000), None) is False


def test_用户要求简短时结构线索也不进() -> None:
    assert should_use_long_answer("简短说一下完整方案", None) is False


@pytest.mark.parametrize("prompt", LONG_PROMPTS, ids=[item.text for item in LONG_PROMPTS])
def test_二十条长文题目都进分节(prompt) -> None:
    assert should_use_long_answer(prompt.text) is True


@pytest.mark.parametrize("text", ("简短对比一下", "今天天气", "修复 loop.py"))
def test_短问和编码任务不进长文(text: str) -> None:
    assert should_use_long_answer(text) is False
