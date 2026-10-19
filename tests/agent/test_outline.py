"""大纲：3–7 节，超窗缩节，写作状态能存能读。"""

import json

from spoolkit.agent.writing_state import (
    OUTLINE_INSTRUCTION,
    OutlineFailure,
    OutlineSection,
    WritingState,
    parse_outline,
    request_outline,
)
from spoolkit.llm.types import ChatRequest, ChatResponse, Message


def _lines(*rows: tuple[str, str, int]) -> str:
    return "\n".join(f"{title} | {points} | {tokens}" for title, points, tokens in rows)


def test_五行大纲原样收下() -> None:
    text = _lines(
        ("背景", "说明现状", 400),
        ("问题", "指出缺口", 500),
        ("方案", "给出做法", 800),
        ("步骤", "列出顺序", 600),
        ("风险", "写明代价", 300),
    )
    state = parse_outline(text, goal="把方案写清楚", token_budget=8000)
    assert isinstance(state, WritingState)
    assert [item.title for item in state.outline] == ["背景", "问题", "方案", "步骤", "风险"]
    assert state.outline[2].points == "给出做法"
    assert state.outline[2].target_tokens == 800
    assert state.next_section == 0
    assert state.completed == ()
    assert state.decisions == ()


def test_少于三节失败() -> None:
    text = _lines(("背景", "说明现状", 400), ("问题", "指出缺口", 500))
    failed = parse_outline(text, goal="短", token_budget=8000)
    assert isinstance(failed, OutlineFailure)
    assert "3" in failed.reason


def test_超过七节失败() -> None:
    rows = [(f"第{i}节", "要点", 200) for i in range(1, 10)]
    failed = parse_outline(_lines(*rows), goal="多", token_budget=8000)
    assert isinstance(failed, OutlineFailure)
    assert "7" in failed.reason


def test_超窗时从末尾删到能放下() -> None:
    text = _lines(
        ("背景", "说明现状", 1000),
        ("问题", "指出缺口", 1000),
        ("方案", "给出做法", 1000),
        ("步骤", "列出顺序", 1000),
        ("风险", "写明代价", 1000),
    )
    state = parse_outline(text, goal="缩", token_budget=3200)
    assert isinstance(state, WritingState)
    assert [item.title for item in state.outline] == ["背景", "问题", "方案"]
    assert sum(item.target_tokens for item in state.outline) <= 3200


def test_三节仍超窗时按比例缩小目标() -> None:
    text = _lines(
        ("背景", "说明现状", 2000),
        ("问题", "指出缺口", 2000),
        ("方案", "给出做法", 2000),
    )
    state = parse_outline(text, goal="缩目标", token_budget=3000)
    assert isinstance(state, WritingState)
    assert len(state.outline) == 3
    assert [item.target_tokens for item in state.outline] == [1000, 1000, 1000]


def test_轻JSON同样能解析() -> None:
    text = """
    {"outline": [
      {"title": "背景", "points": "说明现状", "target_tokens": 400},
      {"title": "问题", "points": "指出缺口", "target_tokens": 500},
      {"title": "方案", "points": "给出做法", "target_tokens": 600}
    ]}
    """
    state = parse_outline(text, goal="json", token_budget=8000)
    assert isinstance(state, WritingState)
    assert state.outline[1].title == "问题"


def test_状态序列化后能原样读回() -> None:
    state = WritingState(
        goal="写报告",
        outline=(
            OutlineSection("背景", "说明现状", 400),
            OutlineSection("问题", "指出缺口", 500),
            OutlineSection("方案", "给出做法", 600),
        ),
        completed=(0,),
        decisions=("术语用「窗口」",),
        next_section=1,
    )
    restored = WritingState.from_dict(state.to_dict())
    assert restored == state


class _Once:
    def __init__(self, text: str) -> None:
        self.request: ChatRequest | None = None
        self._text = text

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.request = request
        return ChatResponse(text=self._text, prompt_tokens=20, completion_tokens=30)


def test_大纲请求沿用前缀且不带schema() -> None:
    prefix = (
        Message(role="system", content="系统"),
        Message(role="user", content="工具结果：读过 README"),
    )
    body = _lines(
        ("背景", "说明现状", 400),
        ("问题", "指出缺口", 500),
        ("方案", "给出做法", 600),
    )
    gateway = _Once(body)
    state = request_outline(gateway, prefix, goal="交代清楚", token_budget=2400)
    assert isinstance(state, WritingState)
    assert len(state.outline) == 3
    request = gateway.request
    assert request is not None
    assert request.messages[:-1] == prefix
    assert request.response_schema is None
    assert request.cache_prompt is True
    assert "2400" in request.messages[-1].content
    assert "不是控制层" in request.messages[-1].content


def _three() -> str:
    return _lines(
        ("背景", "说明现状", 400),
        ("问题", "指出缺口", 500),
        ("方案", "给出做法", 600),
    )


def test_控制JSON包着行格式也能解析() -> None:
    wrapped = json.dumps(
        {"thought": "给大纲", "tool_calls": [], "done": True, "final": _three()},
        ensure_ascii=False,
    )
    state = parse_outline(wrapped, goal="包一层", token_budget=8000)
    assert isinstance(state, WritingState)
    assert [item.title for item in state.outline] == ["背景", "问题", "方案"]


def test_控制JSON包着JSON数组也能解析() -> None:
    rows = json.dumps(
        [
            {"title": "背景", "points": "说明现状", "target_tokens": 400},
            {"title": "问题", "points": "指出缺口", "target_tokens": 500},
            {"title": "方案", "points": "给出做法", "target_tokens": 600},
        ],
        ensure_ascii=False,
    )
    wrapped = json.dumps({"final": rows}, ensure_ascii=False)
    state = parse_outline(wrapped, goal="数组", token_budget=8000)
    assert isinstance(state, WritingState)
    assert len(state.outline) == 3


def test_围栏里的JSON数组能解析() -> None:
    body = """```json
[{"title": "背景", "points": "说明现状", "target_tokens": 400},
 {"title": "问题", "points": "指出缺口", "target_tokens": 500},
 {"title": "方案", "points": "给出做法", "target_tokens": 600}]
```"""
    state = parse_outline(body, goal="围栏", token_budget=8000)
    assert isinstance(state, WritingState)
    assert state.outline[0].title == "背景"


def test_嵌套超过两层就失败() -> None:
    core = json.dumps({"final": _three()}, ensure_ascii=False)
    mid = json.dumps({"final": core}, ensure_ascii=False)
    outer = json.dumps({"final": mid}, ensure_ascii=False)
    failed = parse_outline(outer, goal="太深", token_budget=8000)
    assert isinstance(failed, OutlineFailure)
    assert "嵌套" in failed.reason


def test_大纲失败时固定三节() -> None:
    from spoolkit.agent.writing_state import fallback_outline

    state = fallback_outline("写方案", 4000)
    assert [item.title for item in state.outline] == ["背景与目标", "方案与结构", "风险与下一步"]
    assert sum(item.target_tokens for item in state.outline) == 4000
