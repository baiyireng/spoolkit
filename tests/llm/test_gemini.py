import json
from pathlib import Path

import httpx
import pytest

from agents_dev.llm.gemini import (
    GeminiError,
    GeminiGateway,
    load_env_file,
    to_gemini_schema,
)
from agents_dev.llm.types import ChatRequest, Message

API_KEY = "test-key-not-real"


def _reply(text: str = '{"thought":"t","tool_calls":[],"done":true,"final":"好"}') -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7},
    }


def _gateway(handler, **kwargs) -> GeminiGateway:
    return GeminiGateway(API_KEY, transport=httpx.MockTransport(handler), **kwargs)


def _request(*messages: Message, max_tokens: int = 256, schema=None) -> ChatRequest:
    return ChatRequest(messages=messages, max_tokens=max_tokens, response_schema=schema)


def test_解析文本与用量统计() -> None:
    gateway = _gateway(lambda request: httpx.Response(200, json=_reply()))
    response = gateway.chat(_request(Message(role="user", content="你好")))
    assert "final" in response.text
    assert response.prompt_tokens == 11
    assert response.completion_tokens == 7
    gateway.close()


def test_密钥放在请求头而不是URL() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_reply())

    gateway = _gateway(handler)
    gateway.chat(_request(Message(role="user", content="你好")))
    assert seen["key"] == API_KEY
    assert API_KEY not in seen["url"]
    assert seen["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-3.6-flash:generateContent"
    )
    gateway.close()


def test_系统消息进入systemInstruction() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_reply())

    gateway = _gateway(handler)
    gateway.chat(
        _request(
            Message(role="system", content="你是助手"),
            Message(role="user", content="任务"),
        )
    )
    assert seen["payload"]["systemInstruction"]["parts"][0]["text"] == "你是助手"
    assert [c["role"] for c in seen["payload"]["contents"]] == ["user"]
    gateway.close()


def test_助手消息映射为model角色() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_reply())

    gateway = _gateway(handler)
    gateway.chat(
        _request(
            Message(role="user", content="任务"),
            Message(role="assistant", content="{}"),
            Message(role="tool", content="文件内容"),
        )
    )
    roles = [c["role"] for c in seen["payload"]["contents"]]
    assert roles == ["user", "model", "user"]
    assert "【工具结果】" in seen["payload"]["contents"][2]["parts"][0]["text"]
    gateway.close()


def test_默认附带schema且使用请求指定的结构() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_reply())

    gateway = _gateway(handler)
    custom = {
        "type": "object",
        "properties": {"arguments": {"type": "object", "properties": {"path": {"type": "string"}}}},
    }
    gateway.chat(_request(Message(role="user", content="任务"), schema=custom))
    config = seen["payload"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["temperature"] == 0.0
    assert config["responseSchema"]["properties"]["arguments"]["properties"] == {
        "path": {"type": "STRING"}
    }
    gateway.close()


def test_可以关闭schema以兼容接口差异() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_reply())

    gateway = _gateway(handler, use_schema=False)
    gateway.chat(_request(Message(role="user", content="任务")))
    assert "responseSchema" not in seen["payload"]["generationConfig"]
    gateway.close()


def test_非200状态抛出错误() -> None:
    gateway = _gateway(lambda request: httpx.Response(403, json={"error": {}}))
    with pytest.raises(GeminiError):
        gateway.chat(_request(Message(role="user", content="x")))
    gateway.close()


def test_响应没有候选时抛出错误() -> None:
    gateway = _gateway(
        lambda request: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    )
    with pytest.raises(GeminiError):
        gateway.chat(_request(Message(role="user", content="x")))
    gateway.close()


def test_文本为空时抛出错误() -> None:
    gateway = _gateway(
        lambda request: httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})
    )
    with pytest.raises(GeminiError):
        gateway.chat(_request(Message(role="user", content="x")))
    gateway.close()


def test_缺少密钥时构造失败() -> None:
    with pytest.raises(GeminiError):
        GeminiGateway("")


def test_schema转换把类型名大写() -> None:
    converted = to_gemini_schema(
        {"type": "object", "properties": {"a": {"type": "string"}}}
    )
    assert converted["type"] == "OBJECT"
    assert converted["properties"]["a"]["type"] == "STRING"


def test_schema转换把可空类型变成nullable() -> None:
    converted = to_gemini_schema({"type": ["string", "null"]})
    assert converted["type"] == "STRING"
    assert converted["nullable"] is True


def test_schema转换保证OBJECT带properties() -> None:
    assert to_gemini_schema({"type": "object"})["properties"] == {}


def test_读取env文件(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# 注释\nGEMINI_API_KEY='abc'\nOTHER=1\n\n坏行没有等号\n", encoding="utf-8"
    )
    values = load_env_file(path)
    assert values["GEMINI_API_KEY"] == "abc"
    assert values["OTHER"] == "1"
    assert len(values) == 2


def test_env文件不存在时返回空(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope.env") == {}
