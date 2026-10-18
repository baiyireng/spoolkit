"""续写去掉重叠；整段重复则重写，截断的续写不再加请求。"""

import logging

from spoolkit.agent.continuation import looks_repeated, merge_continuation
from spoolkit.agent.final_turn import (
    CONTINUATION_INSTRUCTION,
    FINAL_INSTRUCTION,
    request_final,
)
from spoolkit.agent.sections import generate_section
from spoolkit.agent.writing_state import OutlineSection, WritingState
from spoolkit.llm.types import ChatRequest, ChatResponse, Message

_OVERLAP = "这里是衔接用的重叠结尾"
_HEAD = ("甲" * 220) + _OVERLAP
_TAIL = _OVERLAP + "以及新写的后半段"
_PHRASE = "已写过的正文会被续写原样再写一遍。"
_REPEATED_HEAD = _PHRASE * 30
_REPEATED_TAIL = _REPEATED_HEAD[-400:]
_REWRITE = "重写后的正文，不再复述前文。"
_FACTS = "工具结果：窗口是 8192。"


class _Script:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.requests: list[ChatRequest] = []
        self._responses = list(responses)

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self._responses.pop(0)


def _cut(text: str) -> ChatResponse:
    return ChatResponse(text=text, prompt_tokens=100, completion_tokens=40, truncated=True)


def _done(text: str) -> ChatResponse:
    return ChatResponse(text=text, prompt_tokens=120, completion_tokens=20, truncated=False)


def _state() -> WritingState:
    return WritingState(
        goal="写清楚",
        outline=(
            OutlineSection("背景", "说明现状", 800),
            OutlineSection("问题", "指出缺口", 800),
            OutlineSection("方案", "给出做法", 800),
        ),
    )


def test_重叠区从续写里切掉() -> None:
    merged = merge_continuation(_HEAD, _TAIL)
    assert merged == _HEAD + "以及新写的后半段"
    assert merged.count(_OVERLAP) == 1


def test_没有重叠就原样拼接() -> None:
    assert merge_continuation("开头还没写完", "后面补上了") == "开头还没写完后面补上了"


def test_整段十二字重合超过一半算重复() -> None:
    assert looks_repeated(_REPEATED_HEAD, _REPEATED_TAIL)


def test_新内容以及刚好一半重合都不算重复() -> None:
    assert not looks_repeated("甲" * 400, "乙" * 400)
    assert not looks_repeated("A" * 12, "A" * 12 + "B")


def test_节被截断后续写去掉重叠() -> None:
    gateway = _Script([_cut(_HEAD), _done(_TAIL)])
    draft = generate_section(gateway, _state(), facts=_FACTS)
    assert len(gateway.requests) == 2
    assert gateway.requests[1].messages[:-1] == gateway.requests[0].messages
    assert gateway.requests[1].messages[-1].content == CONTINUATION_INSTRUCTION
    assert gateway.requests[1].max_tokens != gateway.requests[0].max_tokens * 2
    assert gateway.requests[1].response_schema is None
    assert draft.text == _HEAD + "以及新写的后半段"
    assert draft.truncated is False


def test_节的续写仍截断时不再重写() -> None:
    gateway = _Script([_cut("半截"), _cut("还是半截")])
    draft = generate_section(gateway, _state(), facts=_FACTS)
    assert len(gateway.requests) == 2
    assert draft.truncated is True
    assert "继续" in draft.text
    assert "半截" in draft.text


def test_续写重复时重写该节且不保留重复(caplog) -> None:
    caplog.set_level(logging.WARNING)
    gateway = _Script([
        _cut(_REPEATED_HEAD),
        _done(_REPEATED_TAIL),
        _done(_REWRITE),
    ])
    draft = generate_section(gateway, _state(), facts=_FACTS)
    assert len(gateway.requests) == 3
    assert gateway.requests[2].messages == gateway.requests[0].messages
    assert gateway.requests[2].max_tokens == gateway.requests[0].max_tokens
    assert draft.text == _REWRITE
    assert draft.truncated is False
    assert _REPEATED_TAIL not in draft.text
    assert any("重写" in record.message for record in caplog.records)


def test_终稿续写重叠只留一次() -> None:
    prefix = (Message(role="user", content="写说明"),)
    gateway = _Script([_cut(_HEAD), _done(_TAIL)])
    draft = request_final(gateway, prefix, max_tokens=800)
    assert len(gateway.requests) == 2
    assert draft.text == _HEAD + "以及新写的后半段"
    assert draft.truncated is False


def test_终稿续写重复时按一点五倍重写且不是两倍(caplog) -> None:
    caplog.set_level(logging.WARNING)
    prefix = (Message(role="user", content="写说明"),)
    gateway = _Script([
        _cut(_REPEATED_HEAD),
        _done(_REPEATED_TAIL),
        _done(_REWRITE),
    ])
    draft = request_final(gateway, prefix, max_tokens=800)
    assert len(gateway.requests) == 3
    assert gateway.requests[2].max_tokens == 1200
    assert gateway.requests[2].max_tokens != 1600
    assert gateway.requests[2].messages[-1].content == FINAL_INSTRUCTION
    assert gateway.requests[2].response_schema is None
    assert draft.text == _REWRITE
    assert draft.truncated is False
    assert any("重写" in record.message for record in caplog.records)
