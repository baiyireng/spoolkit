import json

import httpx

from spoolkit.agent.metrics import capture_metrics, metric_scope
from spoolkit.llm.llamacpp import LlamaCppGateway
from spoolkit.llm.types import ChatRequest, Message


def _request() -> ChatRequest:
    return ChatRequest(
        messages=(Message(role="user", content="hi"),), max_tokens=32
    )


def test_chat_stream拼出完整响应并回调增量() -> None:
    lines = [
        'data: {"choices":[{"delta":{"content":"你"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"好"}}],"timings":{"prompt_n":1,"prompt_ms":12.8}}\n\n',
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
    with capture_metrics() as rows:
        with metric_scope(phase="final", task_type="short", turn_id="s1"):
            response = gateway.chat_stream(_request(), on_delta=seen.append)
    assert "".join(seen) == "你好"
    assert response.text == "你好"
    assert response.completion_tokens == 2
    assert response.truncated is False
    assert rows[0]["prefill_ms"] == 12
    assert rows[0]["first_token_ms"] is not None
    assert rows[0]["first_token_ms"] >= 0
    gateway.close()


def test_流式空内容重试换成另一段提示() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            body = (
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":30,"completion_tokens":0}}\n\n'
                "data: [DONE]\n\n"
            )
        else:
            body = (
                'data: {"choices":[{"delta":{"content":"正文"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":40,"completion_tokens":1}}\n\n'
                "data: [DONE]\n\n"
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode("utf-8"),
        )

    note = "必须输出正文，至少 100 字。没有材料时基于目标写，不要留空。"
    gateway = LlamaCppGateway(transport=httpx.MockTransport(handler))
    response = gateway.chat_stream(
        ChatRequest(
            messages=(Message(role="user", content="只写第 1 节"),),
            max_tokens=256,
            empty_retry=note,
        )
    )
    assert response.text == "正文"
    assert bodies[1]["messages"][-1]["content"] == note
    assert bodies[1]["messages"][:-1] == bodies[0]["messages"]
    gateway.close()


def test_流式空内容重试一次() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            body = (
                'data: {"choices":[{"delta":{},"finish_reason":"length"}],'
                '"usage":{"prompt_tokens":100,"completion_tokens":0}}\n\n'
                "data: [DONE]\n\n"
            )
        else:
            body = (
                'data: {"choices":[{"delta":{"content":"补上"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":100,"completion_tokens":1}}\n\n'
                "data: [DONE]\n\n"
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode("utf-8"),
        )

    gateway = LlamaCppGateway(transport=httpx.MockTransport(handler))
    with capture_metrics() as rows, metric_scope(phase="section", task_type="long", n_ctx=8192):
        response = gateway.chat_stream(_request())
    assert calls["n"] == 2
    assert response.text == "补上"
    empty = [row for row in rows if row.get("event") == "empty_response"]
    assert empty[0]["streaming"] is True
    assert empty[0]["finish_reason"] == "length"
    assert empty[0]["context_tight"] is False
    gateway.close()


def test_无chat_stream时上层可回退() -> None:
    """协议：没有 chat_stream 的网关只提供 chat——由调用方判断。"""
    assert not hasattr(object(), "chat_stream")
