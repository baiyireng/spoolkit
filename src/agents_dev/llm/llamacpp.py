"""llama.cpp 网关。

走 llama-server 的 OpenAI 兼容接口。结构化输出用 response_format 携带
JSON Schema，由 llama.cpp 在服务端转换成 GBNF 语法约束采样——
也就是说，本项目不需要自己写 GBNF 生成器，
「按工具动态生成约束」这件事在客户端只是把 schema 传过去。

token 计数走 /tokenize，拿到的是模型真实分词结果，而不是估算。
"""

from typing import Any

import httpx

from agents_dev.llm.types import ChatRequest, ChatResponse, Message
from agents_dev.net import system_proxy

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_MODEL = "local"


class LlamaCppError(RuntimeError):
    """调用 llama.cpp 服务失败。"""


def _to_payload(request: ChatRequest, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ],
        "max_tokens": max(1, request.max_tokens),
        "temperature": 0.0,
        "stream": False,
    }
    if request.response_schema:
        # llama.cpp 会把这里转成 GBNF 语法做约束采样。
        payload["response_format"] = {
            "type": "json_object",
            "schema": request.response_schema,
        }
    return payload


class LlamaCppGateway:
    """实现 ModelGateway 协议的 llama.cpp 客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 180.0,
        proxy: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            proxy=proxy if proxy is not None else system_proxy(),
        )

    def chat(self, request: ChatRequest) -> ChatResponse:
        try:
            response = self._client.post(
                "/v1/chat/completions", json=_to_payload(request, self.model)
            )
        except httpx.HTTPError as exc:
            raise LlamaCppError(
                f"无法连接 llama-server（{type(exc).__name__}），"
                "请确认服务已启动并监听正确端口"
            ) from exc

        if response.status_code != 200:
            raise LlamaCppError(f"服务返回 {response.status_code}")

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LlamaCppError("响应中没有 choices")

        text = (choices[0].get("message") or {}).get("content") or ""
        if not text:
            raise LlamaCppError("模型返回了空内容")

        usage = data.get("usage") or {}
        return ChatResponse(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            truncated=choices[0].get("finish_reason") in ("length", "max_tokens"),
        )

    def close(self) -> None:
        self._client.close()


class LlamaCppTokenCounter:
    """用服务端 /tokenize 做精确计数。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        proxy: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            proxy=proxy if proxy is not None else system_proxy(),
        )

    def count(self, text: str) -> int:
        if not text:
            return 0
        response = self._client.post("/tokenize", json={"content": text})
        if response.status_code != 200:
            raise LlamaCppError(f"tokenize 返回 {response.status_code}")
        return len(response.json().get("tokens", []))

    def close(self) -> None:
        self._client.close()
