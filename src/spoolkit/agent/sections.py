"""按节写正文，再按大纲组装。

每一节的提示词只带大纲、已定约束和本节要求，以及必要事实。
已写正文不送回去，避免提示词随节数变长。
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from spoolkit.agent.writing_state import WritingState
from spoolkit.context.phase_budget import HARD_CAP, compute_budget
from spoolkit.llm.types import ChatRequest, ChatResponse, Message

SECTION_INSTRUCTION = (
    "只写第 {index} 节「{title}」。要点：{points}。目标约 {target_tokens} tokens。\n"
    "不要写其他节，不要重复已写内容，不要总结全文。"
)


@dataclass(frozen=True)
class SectionDraft:
    index: int
    title: str
    text: str
    prompt_tokens: int
    truncated: bool


@dataclass(frozen=True)
class AssemblyFailure:
    reason: str


def generate_section(
    gateway: Any,
    state: WritingState,
    *,
    facts: str,
    n_ctx: int | None = None,
    n_past: int = 0,
) -> SectionDraft:
    """写当前节。system 是大纲和约束，user 只有事实。"""
    if not 0 <= state.next_section < len(state.outline):
        raise ValueError("没有这一节")
    section = state.outline[state.next_section]
    request = ChatRequest(
        messages=_messages(state, facts),
        max_tokens=_limit(section.target_tokens, n_ctx=n_ctx, n_past=n_past),
        response_schema=None,
        cache_prompt=True,
    )
    response: ChatResponse = gateway.chat(request)
    return SectionDraft(
        index=state.next_section,
        title=section.title,
        text=response.text.strip(),
        prompt_tokens=response.prompt_tokens,
        truncated=response.truncated,
    )


def write_sections(
    gateway: Any,
    state: WritingState,
    *,
    facts: str,
    n_ctx: int | None = None,
    n_past: int = 0,
) -> tuple[SectionDraft, ...]:
    """按大纲顺序逐节写。每一节都用同一份事实，不把前文拼回去。"""
    drafts: list[SectionDraft] = []
    for index in range(len(state.outline)):
        current = replace(state, next_section=index)
        drafts.append(
            generate_section(
                gateway,
                current,
                facts=facts,
                n_ctx=n_ctx,
                n_past=n_past,
            )
        )
    return tuple(drafts)


def assemble(
    state: WritingState,
    drafts: Sequence[SectionDraft],
) -> str | AssemblyFailure:
    """按大纲顺序拼接。缺节或标题对不上则失败；重复段落只留第一次。"""
    if len(drafts) != len(state.outline):
        return AssemblyFailure(
            f"标题不齐：大纲 {len(state.outline)} 节，收到 {len(drafts)} 节"
        )
    ordered = sorted(drafts, key=lambda item: item.index)
    parts: list[str] = []
    for expected, (section, draft) in enumerate(
        zip(state.outline, ordered, strict=True)
    ):
        if draft.index != expected or draft.title != section.title:
            return AssemblyFailure(
                f"标题不齐：第 {expected + 1} 节应为「{section.title}」"
            )
        parts.append(_with_heading(section.title, draft.text))
    return _dedupe(parts)


def _messages(state: WritingState, facts: str) -> tuple[Message, ...]:
    section = state.outline[state.next_section]
    lines = [f"目标：{state.goal}", "", "大纲："]
    for index, item in enumerate(state.outline, start=1):
        lines.append(f"{index}. {item.title}｜{item.points}｜{item.target_tokens}")
    lines.append("")
    if state.decisions:
        lines.append("已定约束：")
        lines.extend(f"- {item}" for item in state.decisions)
        lines.append("")
    lines.append(
        SECTION_INSTRUCTION.format(
            index=state.next_section + 1,
            title=section.title,
            points=section.points,
            target_tokens=section.target_tokens,
        )
    )
    return (
        Message(role="system", content="\n".join(lines)),
        Message(role="user", content=facts),
    )


def _limit(target_tokens: int, *, n_ctx: int | None, n_past: int) -> int:
    if n_ctx is not None:
        return compute_budget(
            n_ctx=n_ctx,
            n_past=n_past,
            phase="section",
            target_tokens=target_tokens,
        ).max_tokens
    return min(HARD_CAP["section"], target_tokens)


def _with_heading(title: str, text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return f"## {title}"
    for marker in (f"# {title}", f"## {title}", f"### {title}"):
        if stripped.startswith(marker):
            return stripped
    return f"## {title}\n\n{stripped}"


def _dedupe(parts: Sequence[str]) -> str:
    seen: set[str] = set()
    kept_parts: list[str] = []
    for part in parts:
        kept: list[str] = []
        for paragraph in part.split("\n\n"):
            key = " ".join(paragraph.split())
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(paragraph.strip())
        if kept:
            kept_parts.append("\n\n".join(kept))
    return "\n\n".join(kept_parts)
