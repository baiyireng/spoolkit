import httpx

from spoolkit.llm.gemini import GeminiGateway
from spoolkit.llm.llamacpp import LlamaCppGateway
from spoolkit.llm.retry import chat_with_escalation
from spoolkit.llm.types import ChatRequest, ChatResponse, Message


class _Scripted:
    """按顺序返回预设响应，并记录每次请求的预算。"""

    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = list(responses)
        self.budgets: list[int] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.budgets.append(request.max_tokens)
        return self._responses.pop(0)


def _request(max_tokens: int = 100) -> ChatRequest:
    return ChatRequest(
        messages=(Message(role="user", content="x"),), max_tokens=max_tokens
    )


def _ok(text: str = "{}") -> ChatResponse:
    return ChatResponse(text=text, prompt_tokens=1, completion_tokens=1)


def _cut(text: str = "{}") -> ChatResponse:
    return ChatResponse(
        text=text, prompt_tokens=1, completion_tokens=1, truncated=True
    )


def test_未截断时只调用一次() -> None:
    gateway = _Scripted([_ok()])
    chat_with_escalation(gateway, _request())
    assert gateway.budgets == [100]


def test_截断时翻倍预算重试() -> None:
    gateway = _Scripted([_cut(), _ok()])
    response = chat_with_escalation(gateway, _request())
    assert gateway.budgets == [100, 200]
    assert response.truncated is False


def test_重试次数受参数约束() -> None:
    gateway = _Scripted([_cut(), _ok()])
    chat_with_escalation(gateway, _request(), attempts=1)
    assert gateway.budgets == [100]


def test_仍然截断时如实返回截断标记() -> None:
    gateway = _Scripted([_cut(), _cut()])
    assert chat_with_escalation(gateway, _request()).truncated is True


def test_默认响应不是截断的() -> None:
    assert _ok().truncated is False


def test_gemini在达到输出上限时标记截断() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "{}"}]},
                        "finishReason": "MAX_TOKENS",
                    }
                ]
            },
        )

    gateway = GeminiGateway("k", transport=httpx.MockTransport(handler))
    assert gateway.chat(_request()).truncated is True
    gateway.close()


def test_gemini正常结束时不是截断() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}
                ]
            },
        )

    gateway = GeminiGateway("k", transport=httpx.MockTransport(handler))
    assert gateway.chat(_request()).truncated is False
    gateway.close()


def test_llamacpp在长度耗尽时标记截断() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]
            },
        )

    gateway = LlamaCppGateway(transport=httpx.MockTransport(handler))
    assert gateway.chat(_request()).truncated is True
    gateway.close()


def test_llamacpp正常结束时不是截断() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]
            },
        )

    gateway = LlamaCppGateway(transport=httpx.MockTransport(handler))
    assert gateway.chat(_request()).truncated is False
    gateway.close()

