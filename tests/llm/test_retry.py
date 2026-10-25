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


def test_截断重试回调拿到新旧预算() -> None:
    gateway = _Scripted([_cut(), _ok()])
    seen: list[tuple[int, int]] = []
    chat_with_escalation(
        gateway, _request(), on_truncated_retry=lambda a, b: seen.append((a, b))
    )
    assert seen == [(100, 200)]


def test_有chat_stream时on_delta收到增量() -> None:
    class _Streaming(_Scripted):
        def chat_stream(self, request, on_delta=None):
            response = self.chat(request)
            if on_delta is not None:
                on_delta(response.text[:1])
                on_delta(response.text[1:] or "")
            return response

    gateway = _Streaming([_ok("ab")])
    pieces: list[str] = []
    chat_with_escalation(
        gateway, _request(), on_delta=lambda p: pieces.append(p) if p else None
    )
    assert "".join(pieces) == "ab"


def test_截断重试的心跳标明输出加长() -> None:
    from spoolkit.llm.gateway import current_wait_phase, wait_phase

    seen: list[str] = []

    class _Phased(_Scripted):
        def chat(self, request: ChatRequest) -> ChatResponse:
            seen.append(current_wait_phase())
            return super().chat(request)

    gateway = _Phased([_cut(), _ok()])
    with wait_phase("主循环"):
        chat_with_escalation(gateway, _request())
    assert seen == ["主循环", "主循环·输出加长"]


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

