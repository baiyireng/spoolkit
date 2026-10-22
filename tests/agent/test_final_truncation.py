"""终稿截断：续写一次或改成短答，不做整段预算翻倍。"""

import logging

from spoolkit.agent.final_turn import CONTINUATION_INSTRUCTION, request_final
from spoolkit.context.phase_budget import compute_budget
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


def test_截断后续写一次且预算不是翻倍(caplog) -> None:
    caplog.set_level(logging.WARNING)
    prefix = (Message(role="user", content="写说明"),)
    gateway = _Cuts([_cut("开头还没写完"), _done("后面补上了")])
    draft = request_final(gateway, prefix, max_tokens=800, n_ctx=8192, n_past=1000)
    assert len(gateway.requests) == 2
    assert gateway.requests[1].max_tokens != gateway.requests[0].max_tokens * 2
    assert gateway.requests[1].messages[:-1] == prefix
    assert gateway.requests[1].messages[-1].content == CONTINUATION_INSTRUCTION
    assert gateway.requests[1].response_schema is None
    assert draft.text == "开头还没写完后面补上了"
    assert draft.truncated is False
    assert any("续写" in record.message for record in caplog.records)
    assert not any("短答" in record.message for record in caplog.records)


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
