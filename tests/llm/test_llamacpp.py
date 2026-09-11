import json

import httpx
import pytest

from agents_dev.llm.llamacpp import LlamaCppError, LlamaCppGateway, LlamaCppTokenCounter
from agents_dev.llm.types import ChatRequest, Message


def _completion(text: str = '{"thought":"t","tool_calls":[],"done":true,"final":"好"}') -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 21, "completion_tokens": 9},
    }


def _gateway(handler, **kwargs) -> LlamaCppGateway:
    return LlamaCppGateway(transport=httpx.MockTransport(handler), **kwargs)


def _request(schema=None) -> ChatRequest:
    return ChatRequest(
        messages=(Message(role="user", content="任务"),),
        max_tokens=256,
        response_schema=schema,
    )


def test_解析内容与用量() -> None:
    gateway = _gateway(lambda request: httpx.Response(200, json=_completion()))
    response = gateway.chat(_request())
    assert "final" in response.text
    assert response.prompt_tokens == 21
    assert response.completion_tokens == 9
    gateway.close()


def test_请求打到OpenAI兼容路径() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    gateway = _gateway(handler)
    gateway.chat(_request())
    assert seen["path"] == "/v1/chat/completions"
    assert seen["payload"]["messages"][0]["role"] == "user"
    assert seen["payload"]["stream"] is False
    gateway.close()


def test_有schema时通过response_format下发() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    gateway = _gateway(handler)
    gateway.chat(_request(schema))
    assert seen["payload"]["response_format"]["schema"] == schema
    gateway.close()


def test_无schema时不带response_format() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    gateway = _gateway(handler)
    gateway.chat(_request())
    assert "response_format" not in seen["payload"]
    gateway.close()


def test_服务返回非200时报错() -> None:
    gateway = _gateway(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(LlamaCppError):
        gateway.chat(_request())
    gateway.close()


def test_没有choices时报错() -> None:
    gateway = _gateway(lambda request: httpx.Response(200, json={"choices": []}))
    with pytest.raises(LlamaCppError):
        gateway.chat(_request())
    gateway.close()


def test_内容为空时报错() -> None:
    payload = {"choices": [{"message": {"content": ""}}]}
    gateway = _gateway(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(LlamaCppError):
        gateway.chat(_request())
    gateway.close()


def test_连不上服务时给出可操作的提示() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    gateway = _gateway(handler)
    with pytest.raises(LlamaCppError) as excinfo:
        gateway.chat(_request())
    assert "llama-server" in str(excinfo.value)
    gateway.close()


def test_token计数走服务端tokenize() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"tokens": [1, 2, 3, 4]})

    counter = LlamaCppTokenCounter(transport=httpx.MockTransport(handler))
    assert counter.count("一段文本") == 4
    assert seen["path"] == "/tokenize"
    counter.close()


def test_空文本不请求服务端() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("空文本不应发起请求")

    counter = LlamaCppTokenCounter(transport=httpx.MockTransport(handler))
    assert counter.count("") == 0
    counter.close()

