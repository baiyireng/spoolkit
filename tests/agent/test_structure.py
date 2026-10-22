"""停笔但围栏没闭合时只续写一次。截断和重复仍走原来的恢复。"""

from spoolkit.agent.final_turn import CONTINUATION_INSTRUCTION, request_final
from spoolkit.agent.sections import generate_section
from spoolkit.agent.writing_state import OutlineSection, WritingState
from spoolkit.llm.types import ChatRequest, ChatResponse, Message

_PHRASE = "已写过的正文会被续写原样再写一遍。"


class _Script:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.requests: list[ChatRequest] = []
        self._responses = list(responses)

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self._responses.pop(0)


def _done(text: str) -> ChatResponse:
    return ChatResponse(text=text, prompt_tokens=100, completion_tokens=20, truncated=False)


def _cut(text: str) -> ChatResponse:
    return ChatResponse(text=text, prompt_tokens=100, completion_tokens=20, truncated=True)


def _state() -> WritingState:
    return WritingState(
        goal="写清楚",
        outline=(
            OutlineSection("背景", "说明现状", 400),
            OutlineSection("问题", "指出缺口", 400),
            OutlineSection("方案", "给出做法", 400),
        ),
    )


def test_围栏未闭合的终稿只续写一次且不重写() -> None:
    prefix = (Message(role="user", content="写说明"),)
    opened = "## 说明\n```\n" + (_PHRASE * 30)
    gateway = _Script([_done(opened), _done(opened[-400:]), _done("不该再请求")])
    draft = request_final(gateway, prefix, max_tokens=800)
    assert len(gateway.requests) == 2
    assert gateway.requests[1].messages[-1].content == CONTINUATION_INSTRUCTION
    assert "不该再请求" not in draft.text
    assert draft.truncated is False


def test_围栏续写仍截断则短答且不再请求() -> None:
    prefix = (Message(role="user", content="写说明"),)
    gateway = _Script([_done("```\n半截代码"), _cut("还没关上"), _done("多余")])
    draft = request_final(gateway, prefix, max_tokens=800)
    assert len(gateway.requests) == 2
    assert draft.truncated is True
    assert "继续" in draft.text
    assert "半截代码" in draft.text


def test_节在下一个标题处截断且不再请求() -> None:
    gateway = _Script([_done("本节正文\n## 下一节\n多余"), _done("不该续写")])
    draft = generate_section(gateway, _state(), facts="事实")
    assert len(gateway.requests) == 1
    assert draft.text == "本节正文"
    assert "下一节" not in draft.text


def test_节的围栏未闭合时续写一次不重写() -> None:
    opened = "```\n" + (_PHRASE * 30)
    gateway = _Script([_done(opened), _done(opened[-400:]), _done("重写不该发生")])
    draft = generate_section(gateway, _state(), facts="事实")
    assert len(gateway.requests) == 2
    assert gateway.requests[1].messages[-1].content == CONTINUATION_INSTRUCTION
    assert "重写不该发生" not in draft.text
    assert draft.truncated is False
