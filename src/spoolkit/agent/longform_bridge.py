"""把现有大纲接到 DocumentState，再在终稿路径上跑 LongFormRunner。

不另写规划器。大纲仍来自 sections.request_outline；没收下就返回 None，
由 compose_long_answer 走原来的 fallback_outline。
target_chars 按中文离线口径估算：1 token 约等于 1 字。这是写作目标，
不是 450 字的通过线。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from spoolkit.agent.chat_sink import CollectingChatSink
from spoolkit.agent.document_bodies import InMemoryBodyStore, section_ref
from spoolkit.agent.document_state import DocumentState, OutlineEntry
from spoolkit.agent.longform import LongFormRunner
from spoolkit.agent.protocol import AnswerPlan
from spoolkit.agent.sections import fallback_outline, request_outline
from spoolkit.agent.writing_state import OutlineFailure, WritingState
from spoolkit.llm.types import Message

# 与 OfflineTokenCounter 的非 ASCII 规则一致：一个汉字计 1 token，也计 1 字。
CHARS_PER_TOKEN = 1


@dataclass(frozen=True, slots=True)
class LongformDraft:
    text: str
    sections: int
    chars: int


def target_chars_for(target_tokens: int) -> int:
    """把大纲上的目标 tokens 换成目标字数。至少 1 字。"""
    return max(1, target_tokens * CHARS_PER_TOKEN)


def document_state_from_writing(state: WritingState) -> DocumentState | None:
    """同一套字段转换。request_outline 和 fallback_outline 都走这里。"""
    if not state.outline:
        return None
    outline = tuple(
        OutlineEntry(
            index=index,
            title=item.title,
            points=item.points,
            target_chars=target_chars_for(item.target_tokens),
        )
        for index, item in enumerate(state.outline)
    )
    return DocumentState(
        goal=state.goal,
        outline=outline,
        deliverable="chat",
        phase="plan",
        pending_sections=tuple(range(len(outline))),
    )


def build_document_state(
    goal: str,
    plan: AnswerPlan | None,
    prefix: Sequence[Message],
    gateway: Any,
    *,
    token_budget: int,
) -> DocumentState | None:
    """要一份大纲并收成 plan 阶段的 DocumentState。失败时返回 None。"""
    del plan
    if token_budget < 1 or not goal.strip():
        return None
    written = request_outline(gateway, prefix, goal=goal, token_budget=token_budget)
    if isinstance(written, OutlineFailure) or not isinstance(written, WritingState):
        return None
    return document_state_from_writing(written)


def render_document(state: DocumentState, bodies: InMemoryBodyStore) -> str | None:
    """按大纲顺序拼接。缺一节就不交出去。"""
    parts: list[str] = []
    total = 0
    for entry in state.outline:
        ref = section_ref(entry.index)
        if not bodies.exists(ref):
            return None
        body = bodies.get(ref).strip()
        if not body:
            return None
        total += len(body)
        parts.append(f"## {entry.title}\n\n{body}")
    if not parts:
        return None
    return "\n\n".join(parts)


def try_longform_text(
    *,
    gateway: Any,
    prefix: Sequence[Message],
    goal: str,
    plan: AnswerPlan | None,
    token_budget: int,
) -> LongformDraft | None:
    """跑完并校验通过才返回正文。否则返回 None，调用方回退旧的分节组装。"""
    state = build_document_state(goal, plan, prefix, gateway, token_budget=token_budget)
    if state is None:
        return None
    bodies = InMemoryBodyStore()
    sink = CollectingChatSink()
    result = LongFormRunner(state, gateway, bodies, sink).run()
    if not result.done:
        return None
    text = render_document(state, bodies)
    if not text:
        return None
    chars = 0
    for entry in state.outline:
        chars += len(bodies.get(section_ref(entry.index)).strip())
    return LongformDraft(text=text, sections=len(state.outline), chars=chars)


def fallback_document_state(goal: str, budget: int) -> DocumentState | None:
    """固定三节也用同一套字数换算。终稿失败回退不走这里，仍由 compose 调用。"""
    return document_state_from_writing(fallback_outline(goal, budget))
