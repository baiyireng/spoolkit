"""模型输出的结构化协议。

每轮输出必须是一个 JSON 对象，字段固定。解析失败不抛异常，而是返回
ParseFailure，由主循环作为反馈回灌给模型重试——格式错误在小模型上
是常态而非异常，把它当异常处理会让主循环变得难以推理。

TURN_SCHEMA 同时用于生成 GBNF 语法约束，从采样层面消灭格式错误。
"""

import json
from dataclasses import dataclass
from typing import Any

from agents_dev.agent.state import StateDelta
from agents_dev.tools.types import ToolCall

TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
            },
        },
        "state": {"type": "object"},
        "final": {"type": ["string", "null"]},
    },
    "required": ["thought", "tool_calls"],
}

_STATE_FIELDS = {"done_added", "current", "verify", "excluded_added", "hypothesis"}


@dataclass(frozen=True)
class AgentTurn:
    """一轮输出的结构化表示。"""

    thought: str
    tool_calls: tuple[ToolCall, ...]
    state_delta: StateDelta | None
    final: str | None


@dataclass(frozen=True)
class ParseFailure:
    """解析失败，reason 会作为反馈回灌给模型。"""

    reason: str


def _parse_tool_calls(raw: Any) -> tuple[ToolCall, ...] | str:
    """返回工具调用元组，或返回错误说明字符串。"""
    if not isinstance(raw, list):
        return "tool_calls 必须是数组"

    calls: list[ToolCall] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return f"tool_calls[{index}] 必须是对象"
        name = item.get("name")
        if not isinstance(name, str) or not name:
            return f"tool_calls[{index}] 缺少合法的 name"
        arguments = item.get("arguments", {})
        if not isinstance(arguments, dict):
            return f"tool_calls[{index}].arguments 必须是对象"
        calls.append(ToolCall(name=name, arguments=arguments))
    return tuple(calls)


def _parse_state(raw: Any) -> StateDelta | None | str:
    """返回状态增量、None，或错误说明字符串。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "state 必须是对象"

    unknown = set(raw) - _STATE_FIELDS
    if unknown:
        return f"state 存在未知字段: {', '.join(sorted(unknown))}"

    return StateDelta(
        done_added=list(raw.get("done_added", [])),
        current=raw.get("current"),
        verify=raw.get("verify"),
        excluded_added=list(raw.get("excluded_added", [])),
        hypothesis=raw.get("hypothesis"),
    )


def parse_turn(text: str) -> AgentTurn | ParseFailure:
    """解析模型一轮输出。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return ParseFailure(reason=f"输出不是合法 JSON: {exc.msg}")

    if not isinstance(payload, dict):
        return ParseFailure(reason="输出必须是 JSON 对象")

    thought = payload.get("thought")
    if not isinstance(thought, str):
        return ParseFailure(reason="缺少字符串字段 thought")

    calls = _parse_tool_calls(payload.get("tool_calls", []))
    if isinstance(calls, str):
        return ParseFailure(reason=calls)

    state_delta = _parse_state(payload.get("state"))
    if isinstance(state_delta, str):
        return ParseFailure(reason=state_delta)

    final = payload.get("final")
    if final is not None and not isinstance(final, str):
        return ParseFailure(reason="final 必须是字符串或 null")

    if not calls and final is None:
        return ParseFailure(reason="既没有 tool_calls 也没有 final，本轮没有产出")

    return AgentTurn(
        thought=thought,
        tool_calls=calls,
        state_delta=state_delta,
        final=final,
    )

