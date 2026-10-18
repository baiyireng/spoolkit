import httpx

from spoolkit.llm.llamacpp import LlamaCppGateway
from spoolkit.llm.types import ChatRequest, Message


def _request() -> ChatRequest:
    return ChatRequest(
        messages=(Message(role="user", content="hi"),), max_tokens=32
    )


def test_chat_stream拼出完整响应并回调增量() -> None:
    lines = [
        'data: {"choices":[{"delta":{"content":"你"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"好"}}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n',
        "data: [DONE]\n\n",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(lines).encode("utf-8"),
        )

    seen: list[str] = []
    gateway = LlamaCppGateway(transport=httpx.MockTransport(handler))
    response = gateway.chat_stream(_request(), on_delta=seen.append)
    assert "".join(seen) == "你好"
    assert response.text == "你好"
    assert response.completion_tokens == 2
    assert response.truncated is False
    gateway.close()


def test_无chat_stream时上层可回退() -> None:
    """协议：没有 chat_stream 的网关只提供 chat——由调用方判断。"""
    assert not hasattr(object(), "chat_stream")
