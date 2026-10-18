"""长文触发：模型自报优先，否则看线索和预估 token，不看问句长短。"""

from spoolkit.agent.answer_plan import should_use_long_answer
from spoolkit.agent.protocol import AnswerPlan


def test_mode_long必进长文() -> None:
    plan = AnswerPlan(mode="long", target_tokens=10)
    assert should_use_long_answer(" hi ", plan) is True


def test_mode_short不进长文() -> None:
    plan = AnswerPlan(mode="short", target_tokens=9000)
    text = "请写一份完整方案，要有架构、表格、对比、报告、实施步骤，尽量详细。\n" + ("行" * 8000)
    assert should_use_long_answer(text, plan) is False


def test_没有申报时短问句即使命中线索也不进() -> None:
    assert should_use_long_answer("写一份完整方案", None) is False


def test_没有申报时长问句没有线索不进() -> None:
    assert should_use_long_answer("请看这段说明。" + ("字" * 8000), None) is False


def test_线索齐全且预估超过1500才进() -> None:
    body = "请用表格对比两套架构，并给出实施步骤。\n" + ("| 项 | 值 |\n" * 800)
    assert should_use_long_answer(body, None) is True


def test_每条线索都能单独触发() -> None:
    cues = ("完整方案", "报告", "对比", "架构", "表格", "实施步骤", "详细")
    padding = "补充" * 2000
    for cue in cues:
        assert should_use_long_answer(f"请写{cue}。\n{padding}", None) is True
