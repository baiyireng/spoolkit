"""Gemini 网关。

存在的意义不是取代本地模型，而是**验证 agent 循环本身**：
把 FakeModel 换成真实模型，看协议解析、工具调用、状态更新、
预算控制在「真实模型那些不听话的输出」面前还成不成立。

结构化输出用 responseMimeType=application/json 加 responseSchema，
这是云端对应 GBNF 语法约束的等价物——同样是从采样层面消灭格式错误。

密钥只从环境变量或 .env 读取，绝不写入日志或异常信息。
"""

from pathlib import Path
from typing import Any

import httpx

from spoolkit.agent.protocol import TURN_SCHEMA
from spoolkit.llm.types import ChatRequest, ChatResponse, Message
from spoolkit.net import system_proxy

DEFAULT_MODEL = "gemini-3.6-flash"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
TOOL_RESULT_PREFIX = "【工具结果】"

_TYPE_NAMES = {
    "object": "OBJECT",
    "array": "ARRAY",
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
}


class GeminiError(RuntimeError):
    """调用 Gemini 失败。异常信息中不含密钥。"""


def load_env_file(path: Path) -> dict[str, str]:
    """读取最简单的 KEY=VALUE 形式 .env。不引入第三方依赖。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def to_gemini_schema(node: Any) -> Any:
    """把通用 JSON Schema 转成 Gemini 的 Schema 方言。

    主要差别是类型名要大写，可空类型用 nullable 表达。
    OBJECT 必须带 properties，否则接口会拒绝。
    """
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    declared = node.get("type")
    nullable = False
    if isinstance(declared, list):
        nullable = "null" in declared
        declared = next((item for item in declared if item != "null"), "string")
    if isinstance(declared, str):
        out["type"] = _TYPE_NAMES.get(declared, declared.upper())
    if nullable:
        out["nullable"] = True

    if "properties" in node:
        out["properties"] = {
            key: to_gemini_schema(value) for key, value in node["properties"].items()
        }
    elif out.get("type") == "OBJECT":
        out["properties"] = {}

    if "items" in node:
        out["items"] = to_gemini_schema(node["items"])
    if "required" in node:
        out["required"] = list(node["required"])
    if "enum" in node:
        out["enum"] = list(node["enum"])
    if "description" in node:
        out["description"] = node["description"]
    return out


def _to_contents(messages: tuple[Message, ...]) -> tuple[list[dict], list[dict]]:
    """拆分出 systemInstruction 与 contents。"""
    system_parts: list[dict] = []
    contents: list[dict] = []

    for message in messages:
        if message.role == "system":
            system_parts.append({"text": message.content})
            continue
        if message.role == "assistant":
            contents.append({"role": "model", "parts": [{"text": message.content}]})
            continue
        text = message.content
        if message.role == "tool":
            # 本项目的工具调用走自描述 JSON 协议，不用 Gemini 原生 function calling，
            # 因此把工具结果作为用户侧输入回灌，并加标记让模型区分来源。
            text = f"{TOOL_RESULT_PREFIX}\n{text}"
        contents.append({"role": "user", "parts": [{"text": text}]})

    return system_parts, contents


class GeminiGateway:
    """实现 ModelGateway 协议的 Gemini 客户端。"""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = 90.0,
        transport: httpx.BaseTransport | None = None,
        use_schema: bool = True,
        proxy: str | None = None,
    ) -> None:
        if not api_key:
            raise GeminiError("缺少 API 密钥")
        self.model = model
        self._use_schema = use_schema
        # 密钥走请求头而不是 URL，避免被任何一层日志记录下来。
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            transport=transport,
            # 显式给了 transport（测试用的 MockTransport）就别再套代理：
            # 两者同时给的时候请求不走 transport，测试会去真的打网络。
            proxy=(
                None
                if transport is not None
                else (proxy if proxy is not None else system_proxy())
            ),
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        )

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        system_parts, contents = _to_contents(request.messages)
        generation: dict[str, Any] = {
            "temperature": 0.0,
            "maxOutputTokens": max(1, request.max_tokens),
            "responseMimeType": "application/json",
        }
        if self._use_schema:
            generation["responseSchema"] = to_gemini_schema(
                request.response_schema or TURN_SCHEMA
            )

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation,
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        return payload

    def chat(self, request: ChatRequest) -> ChatResponse:
        # 注意：路径不能以 "/" 开头，否则 httpx 会丢掉 base_url 里的 /v1beta 前缀。
        url = f"models/{self.model}:generateContent"
        try:
            response = self._client.post(url, json=self._payload(request))
        except httpx.HTTPError as exc:
            raise GeminiError(f"请求失败: {type(exc).__name__}") from exc

        if response.status_code != 200:
            detail = ""
            try:
                error = (response.json().get("error") or {})
                detail = error.get("message") or error.get("status") or ""
            except ValueError:
                pass
            raise GeminiError(
                f"接口返回 {response.status_code}: {detail}"
                if detail
                else f"接口返回 {response.status_code}（{response.request.url.path}）"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise GeminiError("响应不是合法 JSON") from exc

        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "无候选结果")
            raise GeminiError(f"没有返回内容: {reason}")

        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        if not text:
            raise GeminiError("候选内容为空")

        usage = data.get("usageMetadata") or {}
        return ChatResponse(
            text=text,
            prompt_tokens=int(usage.get("promptTokenCount", 0)),
            completion_tokens=int(usage.get("candidatesTokenCount", 0)),
            truncated=candidates[0].get("finishReason") == "MAX_TOKENS",
        )

    def context_window(self) -> int | None:
        """向模型信息接口询问输入长度上限。"""
        try:
            response = self._client.get(f"models/{self.model}")
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        limit = response.json().get("inputTokenLimit")
        return limit if isinstance(limit, int) and limit > 0 else None

    def close(self) -> None:
        self._client.close()
