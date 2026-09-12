import json

import httpx
import pytest

from agents_dev.llm.llamacpp import (
    LlamaCppError,
    LlamaCppGateway,
    LlamaCppTokenCounter,
    proxy_for,
)
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


def test_本机地址不走代理() -> None:
    """llama-server 就在本机，把它的请求交给系统代理只会绕远路。

    实测踩过：系统代理开着时，发往 127.0.0.1 的请求被代理拒成 502——
    而这只在「用户的代理是开着的」时候才出现，最容易漏掉。
    """
    assert proxy_for("http://127.0.0.1:8080") is None
    assert proxy_for("http://localhost:8080") is None
    assert proxy_for("http://[::1]:8080") is None


def test_非本机地址仍按系统代理走() -> None:
    """不写死「永远不用代理」——远程的 llama-server 该走代理还是走。"""
    import agents_dev.llm.llamacpp as module

    original = module.system_proxy
    module.system_proxy = lambda: "http://127.0.0.1:7890"
    try:
        assert proxy_for("http://192.168.1.9:8080") == "http://127.0.0.1:7890"
    finally:
        module.system_proxy = original
