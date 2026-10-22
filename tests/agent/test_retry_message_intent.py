"""变量验证：拦截话术必须**回应它的意图**。

用户的第一条质疑是「看不到它想干什么，于是分不清工具不完善还是它理解错了」。
话术里带上意图原文，是这条质疑在**失败路径**上的落点——只说「换一种做法」
时，一个真正想做别的事的模型会把那句话判成「不适用于我」，然后原样重试。

这里直接调 `_retry_block_message`（拦截话术的唯一出处），断的是：
意图原文在里面，而且被当成「你在解释为什么又发一遍」来回应的。
"""

from spoolkit.agent.loop import _retry_block_message
from spoolkit.tools.types import ToolCall

FAILURE = "不是目录: snake.html（survey 只对目录生效）。"


def test_拦截话术带上它的意图原文() -> None:
    call = ToolCall(
        name="survey",
        arguments={"path": "snake.html"},
        intent="一次收齐 snake.html 全部内容",
    )
    text = _retry_block_message(call, FAILURE)
    assert "一次收齐 snake.html 全部内容" in text
    # 回应的方式要说清「为什么又发一遍」，而不只是「错在哪」。
    assert "同样的意图换个说法重发" in text
    # 上一次的结论与照做的下一步仍然要在（不能为了加意图而丢信息）。
    assert "不是目录" in text
    assert "没有执行" in text


def test_没有意图时话术不编造一个() -> None:
    call = ToolCall(name="survey", arguments={"path": "snake.html"})
    text = _retry_block_message(call, FAILURE)
    assert "你写的意图是" not in text
    # 没有意图也要给得出来的下一步，不能因此少说。
    assert "换一种做法" in text
