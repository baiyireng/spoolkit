"""终稿截断：续写一次或改成短答，不做整段预算翻倍。"""

import logging

from spoolkit.agent.final_turn import SHORT_CONTINUE, estimate_tokens, request_final
from spoolkit.context.phase_budget import DEFAULT_MARGIN, MIN_OUTPUT, compute_budget
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatRequest, ChatResponse, Message


class _Cuts:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.requests: list[ChatRequest] = []
        self._responses = list(responses)

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self._responses.pop(0)


def _cut(text: str, prompt_tokens: int = 1000) -> ChatResponse:
    return ChatResponse(
        text=text, prompt_tokens=prompt_tokens, completion_tokens=40, truncated=True
    )


def _done(text: str, prompt_tokens: int = 1100) -> ChatResponse:
    return ChatResponse(
        text=text, prompt_tokens=prompt_tokens, completion_tokens=20, truncated=False
    )


_TAIL_INSTRUCTION = (
    "以上是你已经写出的最后一段。只输出紧接其后的新增内容，不要重复，不要重新开头，不要总结。"
)


def _last_assistant(request: ChatRequest) -> Message:
    assistants = [item for item in request.messages if item.role == "assistant"]
    assert assistants, "续写请求里没有 assistant 尾部"
    return assistants[-1]


def test_截断后续写一次且预算不是翻倍(caplog) -> None:
    caplog.set_level(logging.WARNING)
    prefix = (Message(role="user", content="写说明"),)
    gateway = _Cuts([_cut("开头还没写完"), _done("后面补上了")])
    draft = request_final(gateway, prefix, max_tokens=800, n_ctx=8192, n_past=1000)
    assert len(gateway.requests) == 2
    assert gateway.requests[1].max_tokens != gateway.requests[0].max_tokens * 2
    follow = gateway.requests[1]
    assert follow.messages[:-2] == prefix
    assert follow.messages[-2].role == "assistant"
    assert follow.messages[-2].content == "开头还没写完"
    assert follow.messages[-1].role == "user"
    assert follow.messages[-1].content == _TAIL_INSTRUCTION
    assert follow.response_schema is None
    assert draft.text == "开头还没写完后面补上了"
    assert draft.truncated is False
    assert any("续写" in record.message for record in caplog.records)
    assert not any("短答" in record.message for record in caplog.records)


def test_长正文续写只带最后一段尾部() -> None:
    prefix = (
        Message(role="system", content="系统"),
        Message(role="user", content="写长文"),
    )
    head = "甲" * 3000
    body = head + "可识别结尾"
    gateway = _Cuts([_cut(body), _done("紧接着的新段落")])
    draft = request_final(gateway, prefix, max_tokens=800, n_ctx=8192, n_past=1000)
    follow = gateway.requests[1]
    tail = _last_assistant(follow)
    assert follow.messages[: len(prefix)] == prefix
    assert follow.messages[-1].role == "user"
    assert follow.messages[-1].content == _TAIL_INSTRUCTION
    assert tail.content.endswith("可识别结尾")
    assert head not in tail.content
    assert 1500 <= len(tail.content) <= 2500
    assert draft.text == body + "紧接着的新段落"
    assert draft.text.count("可识别结尾") == 1


def test_空尾部与极短尾部不崩() -> None:
    prefix = (Message(role="user", content="写说明"),)
    empty = _Cuts([_cut(""), _done("从零补上")])
    empty_draft = request_final(empty, prefix, max_tokens=800, n_ctx=8192, n_past=1000)
    assert empty_draft.text == "从零补上"
    assert empty_draft.truncated is False
    empty_follow = empty.requests[1]
    assert empty_follow.messages[-1].content == _TAIL_INSTRUCTION
    assert all(item.role != "assistant" for item in empty_follow.messages)

    short = _Cuts([_cut("。"), _done("下一句")])
    short_draft = request_final(short, prefix, max_tokens=800, n_ctx=8192, n_past=1000)
    assert short_draft.text == "。下一句"
    assert _last_assistant(short.requests[1]).content == "。"


def test_续写重复尾部时merge只留一次() -> None:
    prefix = (Message(role="user", content="写说明"),)
    shared = "这里是衔接用的重叠结尾"
    body = ("前" * 2200) + shared
    follow_text = shared + "以及新写的后半段"
    gateway = _Cuts([_cut(body), _done(follow_text)])
    draft = request_final(gateway, prefix, max_tokens=800, n_ctx=8192, n_past=1000)
    tail = _last_assistant(gateway.requests[1])
    assert tail.content.endswith(shared)
    assert len(gateway.requests) == 2
    assert draft.text == body + "以及新写的后半段"
    assert draft.text.count(shared) == 1


def test_长会话续写把尾部算进上下文(caplog) -> None:
    caplog.set_level(logging.WARNING)
    prefix = (Message(role="user", content="写说明"),)
    partial = "甲" * 2000
    counter = OfflineTokenCounter()
    assert counter.count(partial) == 2000
    assert counter.count(partial) != 1250
    assert estimate_tokens(partial) == counter.count(partial)
    gateway = _Cuts([_cut(partial, prompt_tokens=7000), _done("紧接的新内容")])
    draft = request_final(
        gateway, prefix, n_ctx=8192, prompt_tokens=7000
    )
    assert len(gateway.requests) == 2
    follow = gateway.requests[1]
    tail = _last_assistant(follow).content
    instruction_tokens = counter.count(_TAIL_INSTRUCTION)
    room = 8192 - DEFAULT_MARGIN - 7000 - instruction_tokens - MIN_OUTPUT
    assert partial.endswith(tail)
    assert len(tail) == room
    assert counter.count(tail) == len(tail)
    assert len(tail) < len(partial)
    follow_prompt = 7000 + counter.count(tail) + instruction_tokens
    assert follow_prompt + follow.max_tokens <= 8192 - DEFAULT_MARGIN
    assert follow.max_tokens >= MIN_OUTPUT
    assert draft.text == partial + "紧接的新内容"


def test_续写预算不足时不发第二次请求(caplog) -> None:
    caplog.set_level(logging.WARNING)
    prefix = (Message(role="user", content="写说明"),)
    partial = "甲" * 400
    gateway = _Cuts([_cut(partial, prompt_tokens=7900)])
    draft = request_final(gateway, prefix, n_ctx=8192, n_past=1000)
    assert len(gateway.requests) == 1
    assert draft.truncated is True
    assert draft.text == partial + SHORT_CONTINUE
    assert "继续" in draft.text
    assert any("预算不足" in record.message for record in caplog.records)


def test_续写仍截断时改短答并不再请求(caplog) -> None:
    caplog.set_level(logging.WARNING)
    prefix = (Message(role="user", content="写说明"),)
    gateway = _Cuts([_cut("半截"), _cut("还是半截")])
    draft = request_final(gateway, prefix, max_tokens=800, n_ctx=8192, n_past=1000)
    assert len(gateway.requests) == 2
    assert gateway.requests[1].max_tokens != 1600
    assert draft.truncated is True
    assert "继续" in draft.text
    assert "半截" in draft.text
    assert any("短答" in record.message for record in caplog.records)


def test_终稿第一次预算来自阶段计算器() -> None:
    expected = compute_budget(
        n_ctx=8192, n_past=1000, phase="final", target_tokens=400
    ).max_tokens
    gateway = _Cuts([_done("写完了")])
    request_final(
        gateway,
        (Message(role="user", content="短"),),
        n_ctx=8192,
        n_past=1000,
        target_tokens=400,
    )
    assert gateway.requests[0].max_tokens == expected
    assert expected == 400
