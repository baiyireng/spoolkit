"""模型输出的结构化协议。

每轮输出必须是一个 JSON 对象，字段固定。解析失败不抛异常，而是返回
ParseFailure，由主循环作为反馈回灌给模型重试——格式错误在小模型上
是常态而非异常，把它当异常处理会让主循环变得难以推理。

TURN_SCHEMA 同时用于生成 GBNF 语法约束，从采样层面消灭格式错误。
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from spoolkit.agent.state import StateDelta
from spoolkit.tools.types import ToolCall

logger = logging.getLogger(__name__)

SUMMARY_LIMIT = 200

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
                    "why": {"type": "string"},
                },
                "required": ["name", "arguments"],
            },
        },
        "state": {"type": "object"},
        "final": {"type": ["string", "null"]},
        "done": {"type": "boolean"},
        "assessment": {"type": "string"},
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


def build_turn_schema_v2(registry: Any, only: list[str] | None = None) -> dict[str, Any]:
    """控制回合 v2：没有 `final`，完成时改走短 `summary`。"""
    return build_turn_schema(registry, only, v2=True)


def build_turn_schema(
    registry: Any, only: list[str] | None = None, *, v2: bool = False
) -> dict[str, Any]:
    """按当前注册的工具生成回合输出的约束 schema。

    这是本项目最关键的一处细节。回合输出里的 arguments 是「自由键值对象」，
    但约束解码无法表达自由对象——它会被固化成「没有任何字段的对象」，
    模型于是只能输出空参数，每一轮工具调用都会因缺少必填参数而失败。

    所以 arguments 必须被约束成「本次可用工具参数的并集」：模型由此获得
    一个能放下所有合法参数的空间；而具体某个工具该带哪些参数，仍由工具
    注册表在调用时校验。语法约束负责形状，注册表负责语义。

    only 给定时只允许这几个工具。这不是裁剪提示词，是改采样本身——
    实测里文字提醒推不动小模型，把选项从语法里拿掉才推得动。

    `why` 必须在这里出现，而且必须与 `arguments` **平级**。放在 arguments
    里有两个后果，都不能接受：① 它会被当成某个工具的参数，撞上
    `additionalProperties: false` 的参数校验而整次调用失败；② 上面的并集
    只有「所有工具参数的并集」这一层，一个不属于任何工具的参数进不去。
    平级之后它不参与工具参数校验，语法层却能采样出它——这是「意图能被
    看见」这条要求唯一可靠的位置。
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

    properties: dict[str, Any] = {
        "thought": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": name_rule,
                    "arguments": arguments,
                    "why": {"type": "string"},
                },
                "required": ["name", "arguments"],
            },
        },
        "state": STATE_SCHEMA,
        "done": {"type": "boolean"},
    }
    if v2:
        # 主循环还要 state / assessment。v2 只拿掉 final，避免长文再进 JSON。
        properties["summary"] = {"type": "string"}
        properties["answer_plan"] = {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["short", "long"]},
                "sections": {"type": "integer"},
                "target_tokens": {"type": "integer"},
            },
            "required": ["mode", "target_tokens"],
        }
    else:
        properties["final"] = {"type": ["string", "null"]}
    properties["assessment"] = {"type": "string"}
    return {
        "type": "object",
        "properties": properties,
        "required": ["thought", "tool_calls", "done"],
    }


@dataclass(frozen=True)
class AnswerPlan:
    """控制回合对自己要写多长的申报。长文触发在后续阶段用它。"""

    mode: Literal["short", "long"]
    target_tokens: int
    sections: int | None = None


@dataclass(frozen=True)
class AgentTurn:
    """一轮输出的结构化表示。"""

    thought: str
    tool_calls: tuple[ToolCall, ...]
    state_delta: StateDelta | None
    final: str | None
    done: bool
    # 它对上一次督导建议的表态（接受 / 不接受 + 理由）。空串表示这一轮
    # 没有督导建议要回应——不是「它没表态」，见 loop 里注入时写死的三种回答。
    assessment: str = ""
    # v2 完成回合的短结论。旧协议留空，正文仍在 final。
    summary: str = ""
    answer_plan: AnswerPlan | None = None


@dataclass(frozen=True)
class ParseFailure:
    """解析失败，reason 会作为反馈回灌给模型。"""

    reason: str
    # 失败的种类。空回合要单独标出来：它不是「格式写坏了」，而是模型
    # 在表达一件协议里没有的事（比如「我在等用户确认」），纠正方式完全不同。
    kind: str = ""


def _parse_tool_calls(raw: Any, thought: str = "") -> tuple[ToolCall, ...] | str:
    """返回工具调用元组，或返回错误说明字符串。

    `why` 与 `arguments` 平级读，读不到就退回这一轮的 `thought`——只在
    **只有一个调用**时退。多个调用共用一句说话时，把它绑到每一个上会把
    第二个调用的意图写成第一个的，那比没有意图更坏：排查时它是一句假话。

    意图的赋值刻意不做校验（不查长度、不查内容）：它是给人看的旁证，
    校验只会多出「模型写长了就整轮失败」这种与行为无关的失败。
    """
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
        why = item.get("why")
        intent = why.strip() if isinstance(why, str) and why.strip() else ""
        calls.append(ToolCall(name=name, arguments=arguments, intent=intent))
    if len(calls) == 1 and not calls[0].intent and thought.strip():
        calls[0] = ToolCall(
            name=calls[0].name,
            arguments=calls[0].arguments,
            intent=thought.strip(),
        )
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


def _load_object(text: str) -> dict[str, Any] | ParseFailure:
    """把一轮输出读成 JSON 对象。失败不抛异常。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # 截断与「模型写错格式」是两种完全不同的故障：前者要加预算，
        # 后者要纠正写法。这里把可能性指出来，避免修错地方。
        hint = ""
        if "Unterminated" in exc.msg or exc.pos >= len(text) - 1:
            hint = "（内容疑似被输出预算截断）"
        # kind 要标出来：截断和「格式写坏」的处置完全不同——前者要**把这一轮
        # 的输出变小**并抬高预算，后者要纠正写法。只放在 reason 里，调用方
        # 就得去认字符串；实测它会照着「请只输出规定的 JSON」再来一遍同样大
        # 的输出，于是同样被截断。
        return ParseFailure(
            reason=f"输出不是合法 JSON{hint}: {exc.msg}",
            kind="truncated" if hint else "",
        )

    if not isinstance(payload, dict):
        return ParseFailure(reason="输出必须是 JSON 对象")
    return payload


def _parse_answer_plan(raw: Any) -> AnswerPlan | None | str:
    """返回计划、None，或错误说明。缺省表示这一轮没申报。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "answer_plan 必须是对象"
    mode = raw.get("mode")
    if mode not in ("short", "long"):
        return "answer_plan.mode 必须是 short 或 long"
    target = raw.get("target_tokens")
    if isinstance(target, bool) or not isinstance(target, int) or target < 0:
        return "answer_plan.target_tokens 必须是非负整数"
    sections = raw.get("sections")
    if sections is not None and (isinstance(sections, bool) or not isinstance(sections, int)):
        return "answer_plan.sections 必须是整数"
    return AnswerPlan(mode=mode, target_tokens=target, sections=sections)


def parse_turn(text: str) -> AgentTurn | ParseFailure:
    """解析模型一轮输出。"""
    payload = _load_object(text)
    if isinstance(payload, ParseFailure):
        return payload

    thought = payload.get("thought")
    if not isinstance(thought, str):
        return ParseFailure(reason="缺少字符串字段 thought")

    # 完成状态必须显式声明。早期版本靠「没有工具调用也没有答复」来隐含表达完成，
    # 实测真实模型读不出这层隐含语义，会反复产出「什么都没做」的空回合。
    done = payload.get("done")
    if not isinstance(done, bool):
        return ParseFailure(reason="缺少布尔字段 done")

    calls = _parse_tool_calls(payload.get("tool_calls", []), thought)
    if isinstance(calls, str):
        return ParseFailure(reason=calls)

    state_delta = _parse_state(payload.get("state"))
    if isinstance(state_delta, str):
        return ParseFailure(reason=state_delta)

    final = payload.get("final")
    if final is not None and not isinstance(final, str):
        return ParseFailure(reason="final 必须是字符串或 null")

    assessment = payload.get("assessment")
    if assessment is None:
        assessment = ""
    elif not isinstance(assessment, str):
        return ParseFailure(reason="assessment 必须是字符串")

    if done:
        if not isinstance(final, str) or not final.strip():
            return ParseFailure(reason="已标记 done，但没有给出 final 答复文本")
    elif not calls:
        return ParseFailure(
            reason="既没有调用工具，也没有标记 done", kind="empty_turn"
        )

    return AgentTurn(
        thought=thought,
        tool_calls=calls,
        state_delta=state_delta,
        final=final,
        done=done,
        assessment=assessment.strip(),
    )


def parse_turn_v2(text: str) -> AgentTurn | ParseFailure:
    """解析不含 final 的控制回合。旧格式单独标成 legacy_format，由会话决定是否降级。"""
    payload = _load_object(text)
    if isinstance(payload, ParseFailure):
        return payload

    if "final" in payload:
        return ParseFailure(
            reason=(
                "这是旧协议（含 final）。请改用 v2：不要写 final，"
                "完成时用 summary 写不超过 200 字的结论与依据"
            ),
            kind="legacy_format",
        )

    thought = payload.get("thought")
    if not isinstance(thought, str):
        return ParseFailure(reason="缺少字符串字段 thought")

    done = payload.get("done")
    if not isinstance(done, bool):
        return ParseFailure(reason="缺少布尔字段 done")

    calls = _parse_tool_calls(payload.get("tool_calls", []), thought)
    if isinstance(calls, str):
        return ParseFailure(reason=calls)

    state_delta = _parse_state(payload.get("state"))
    if isinstance(state_delta, str):
        return ParseFailure(reason=state_delta)

    assessment = payload.get("assessment")
    if assessment is None:
        assessment = ""
    elif not isinstance(assessment, str):
        return ParseFailure(reason="assessment 必须是字符串")

    summary = payload.get("summary", "")
    if summary is None:
        summary = ""
    if not isinstance(summary, str):
        return ParseFailure(reason="summary 必须是字符串")
    summary = summary.strip()

    plan = _parse_answer_plan(payload.get("answer_plan"))
    if isinstance(plan, str):
        return ParseFailure(reason=plan)

    if done:
        if calls:
            return ParseFailure(reason="done 与 tool_calls 互斥：标记完成时不要调用工具")
        if not summary:
            return ParseFailure(reason="已标记 done，但 summary 为空")
        if len(summary) > SUMMARY_LIMIT:
            return ParseFailure(reason=f"summary 超过 {SUMMARY_LIMIT} 字")
    else:
        if summary:
            return ParseFailure(reason="未完成时 summary 必须为空")
        if not calls:
            return ParseFailure(
                reason="既没有调用工具，也没有标记 done", kind="empty_turn"
            )

    return AgentTurn(
        thought=thought,
        tool_calls=calls,
        state_delta=state_delta,
        final=None,
        done=done,
        assessment=assessment.strip(),
        summary=summary,
        answer_plan=plan,
    )


class TurnProtocol:
    """一次运行用哪套控制协议。v2 连续两次收到旧格式就降级，并打日志。"""

    def __init__(self, *, v2: bool) -> None:
        self.v2 = v2
        self._legacy_streak = 0
        self._downgrade_pending = False

    def schema(self, registry: Any, only: list[str] | None = None) -> dict[str, Any]:
        if self.v2:
            return build_turn_schema_v2(registry, only)
        return build_turn_schema(registry, only)

    def parse(self, text: str) -> AgentTurn | ParseFailure:
        if not self.v2:
            return parse_turn(text)
        result = parse_turn_v2(text)
        if isinstance(result, ParseFailure) and result.kind == "legacy_format":
            self._legacy_streak += 1
            if self._legacy_streak >= 2:
                self.v2 = False
                self._downgrade_pending = True
                logger.warning("ControlTurn v2 连续收到旧格式，降级到旧协议")
                return parse_turn(text)
            return result
        if isinstance(result, AgentTurn):
            self._legacy_streak = 0
        return result

    def take_downgrade(self) -> bool:
        """降级只通知调用方一次，避免每轮都重装 schema。"""
        pending = self._downgrade_pending
        self._downgrade_pending = False
        return pending

