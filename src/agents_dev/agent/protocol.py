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
        "done": {"type": "boolean"},
    },
    "required": ["thought", "tool_calls", "done"],
}

_STATE_FIELDS = {"done_added", "current", "verify", "excluded_added", "hypothesis"}

STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "done_added": {"type": "array", "items": {"type": "string"}},
        "current": {"type": ["string", "null"]},
        "verify": {"type": ["string", "null"]},
        "excluded_added": {"type": "array", "items": {"type": "string"}},
        "hypothesis": {"type": ["string", "null"]},
    },
}


def build_turn_schema(registry: Any, only: list[str] | None = None) -> dict[str, Any]:
    """按当前注册的工具生成回合输出的约束 schema。

    这是本项目最关键的一处细节。回合输出里的 arguments 是「自由键值对象」，
    但约束解码无法表达自由对象——它会被固化成「没有任何字段的对象」，
    模型于是只能输出空参数，每一轮工具调用都会因缺少必填参数而失败。

    所以 arguments 必须被约束成「本次可用工具参数的并集」：模型由此获得
    一个能放下所有合法参数的空间；而具体某个工具该带哪些参数，仍由工具
    注册表在调用时校验。语法约束负责形状，注册表负责语义。

    only 给定时只允许这几个工具。这不是裁剪提示词，是改采样本身——
    实测里文字提醒推不动小模型，把选项从语法里拿掉才推得动。
    """
    tool_names = tuple(only) if only is not None else registry.names()
    merged: dict[str, Any] = {}
    for name in tool_names:
        spec = registry.get(name)
        if spec is None:
            continue
        for key, rule in spec.parameters.get("properties", {}).items():
            merged.setdefault(key, rule)

    arguments: dict[str, Any] = {"type": "object", "properties": merged}
    if merged:
        arguments["additionalProperties"] = False

    name_rule: dict[str, Any] = {"type": "string"}
    if tool_names:
        name_rule["enum"] = list(tool_names)

    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string"},
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": name_rule, "arguments": arguments},
                    "required": ["name", "arguments"],
                },
            },
            "state": STATE_SCHEMA,
            "done": {"type": "boolean"},
            "final": {"type": ["string", "null"]},
        },
        "required": ["thought", "tool_calls", "done"],
    }


@dataclass(frozen=True)
class AgentTurn:
    """一轮输出的结构化表示。"""

    thought: str
    tool_calls: tuple[ToolCall, ...]
    state_delta: StateDelta | None
    final: str | None
    done: bool


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
        # 截断与「模型写错格式」是两种完全不同的故障：前者要加预算，
        # 后者要纠正写法。这里把可能性指出来，避免修错地方。
        hint = ""
        if "Unterminated" in exc.msg or exc.pos >= len(text) - 1:
            hint = "（内容疑似被输出预算截断）"
        return ParseFailure(reason=f"输出不是合法 JSON{hint}: {exc.msg}")

    if not isinstance(payload, dict):
        return ParseFailure(reason="输出必须是 JSON 对象")

    thought = payload.get("thought")
    if not isinstance(thought, str):
        return ParseFailure(reason="缺少字符串字段 thought")

    # 完成状态必须显式声明。早期版本靠「没有工具调用也没有答复」来隐含表达完成，
    # 实测真实模型读不出这层隐含语义，会反复产出「什么都没做」的空回合。
    done = payload.get("done")
    if not isinstance(done, bool):
        return ParseFailure(reason="缺少布尔字段 done")

    calls = _parse_tool_calls(payload.get("tool_calls", []))
    if isinstance(calls, str):
        return ParseFailure(reason=calls)

    state_delta = _parse_state(payload.get("state"))
    if isinstance(state_delta, str):
        return ParseFailure(reason=state_delta)

    final = payload.get("final")
    if final is not None and not isinstance(final, str):
        return ParseFailure(reason="final 必须是字符串或 null")

    if done:
        if not isinstance(final, str) or not final.strip():
            return ParseFailure(reason="已标记 done，但没有给出 final 答复文本")
    elif not calls:
        return ParseFailure(reason="既没有调用工具，也没有标记 done")

    return AgentTurn(
        thought=thought,
        tool_calls=calls,
        state_delta=state_delta,
        final=final,
        done=done,
    )

