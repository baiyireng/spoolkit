"""按节写正文，再按大纲组装。

每一节的提示词只带大纲、已定约束和本节要求，以及必要事实。
已写正文不送回去，避免提示词随节数变长。截断后续写一次；
续写若在复述已写内容，就重写这一节，而不是把重复段接上去。
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from spoolkit.agent.continuation import looks_repeated, merge_continuation, overlap_chars
from spoolkit.agent.metrics import commit_section, metric_scope, note_retry, reset_section_progress
from spoolkit.agent.final_turn import CONTINUATION_INSTRUCTION, SHORT_CONTINUE
from spoolkit.agent.writing_state import WritingState
from spoolkit.context.phase_budget import HARD_CAP, compute_budget
from spoolkit.llm.types import ChatRequest, ChatResponse, Message

logger = logging.getLogger(__name__)

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
    prompt_tokens: int | None = None,
    overlap: int | None = None,
) -> SectionDraft:
    """写当前节。system 是大纲和约束，user 只有事实。"""
    if not 0 <= state.next_section < len(state.outline):
        raise ValueError("没有这一节")
    section = state.outline[state.next_section]
    request = ChatRequest(
        messages=_messages(state, facts),
        max_tokens=_limit(
            section.target_tokens,
            n_ctx=n_ctx,
            n_past=n_past,
            prompt_tokens=prompt_tokens,
        ),
        response_schema=None,
        cache_prompt=True,
    )
    with metric_scope(phase="section", task_type="long", n_ctx=n_ctx or 0):
        response: ChatResponse = gateway.chat(request)
    if not response.truncated:
        return _draft(state, section.title, response, response.text.strip(), False)

    follow_limit = _continuation_limit(
        request.max_tokens,
        n_ctx=n_ctx,
        n_past=n_past,
        prompt_tokens=response.prompt_tokens or prompt_tokens,
    )
    if follow_limit <= 0:
        logger.warning("本节续写未完成，改为短答并留下继续入口")
        return _draft(state, section.title, response, response.text + SHORT_CONTINUE, True)

    note_retry()
    with metric_scope(
        phase="continuation",
        task_type="long",
        n_ctx=n_ctx or 0,
        retry_index=1,
        recovery="continue",
    ):
        follow: ChatResponse = gateway.chat(
            ChatRequest(
                messages=request.messages + (
                    Message(role="user", content=CONTINUATION_INSTRUCTION),
                ),
                max_tokens=follow_limit,
                response_schema=None,
                cache_prompt=True,
            )
        )
    merged = merge_continuation(
        response.text, follow.text, overlap=overlap_chars(overlap)
    )
    if follow.truncated or not follow.text.strip():
        logger.warning("本节续写未完成，改为短答并留下继续入口")
        return _draft(state, section.title, follow, merged + SHORT_CONTINUE, True)
    if looks_repeated(response.text, follow.text):
        logger.warning("本节续写与已写内容重复，重写该节")
        with metric_scope(
            phase="section",
            task_type="long",
            n_ctx=n_ctx or 0,
            retry_index=2,
            recovery="rewrite",
        ):
            rewrite: ChatResponse = gateway.chat(request)
        if rewrite.truncated or looks_repeated(response.text, rewrite.text):
            logger.warning("本节重写未完成，改为短答并留下继续入口")
            kept = rewrite.text.strip() or response.text
            return _draft(state, section.title, rewrite, kept + SHORT_CONTINUE, True)
        return _draft(state, section.title, rewrite, rewrite.text.strip(), False)
    return _draft(state, section.title, follow, merged.strip(), False)


def write_sections(
    gateway: Any,
    state: WritingState,
    *,
    facts: str,
    n_ctx: int | None = None,
    n_past: int = 0,
    prompt_tokens: int | None = None,
    on_event: Any = None,
) -> tuple[SectionDraft, ...]:
    """按大纲顺序逐节写。每一节都用同一份事实，不把前文拼回去。"""
    total = len(state.outline)
    reset_section_progress()
    drafts: list[SectionDraft] = []
    for index in range(total):
        section = state.outline[index]
        _emit_section(
            on_event,
            "section_start",
            index=index,
            title=section.title,
            total=total,
            state="draft",
        )
        current = replace(state, next_section=index)
        draft = generate_section(
            gateway,
            current,
            facts=facts,
            n_ctx=n_ctx,
            n_past=n_past,
            prompt_tokens=prompt_tokens,
        )
        drafts.append(draft)
        commit_section(total=total, completed=len(drafts))
        _emit_section(
            on_event,
            "section_done",
            index=draft.index,
            title=draft.title,
            total=total,
            state="final",
            text=draft.text,
        )
    return tuple(drafts)


def apply_section_event(
    parts: dict[int, str], kind: str, data: dict
) -> dict[int, str]:
    """按节更新。完成事件只替换这一节，前面的节保持原样。"""
    updated = dict(parts)
    index = int(data["index"])
    if kind == "section_start":
        updated.setdefault(index, "")
    elif kind == "section_done":
        updated[index] = str(data.get("text") or "")
    return updated


def _emit_section(on_event: Any, kind: str, **data: object) -> None:
    if on_event is not None:
        on_event(kind, data)


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


def _draft(
    state: WritingState,
    title: str,
    response: ChatResponse,
    text: str,
    truncated: bool,
) -> SectionDraft:
    return SectionDraft(
        index=state.next_section,
        title=title,
        text=text,
        prompt_tokens=response.prompt_tokens,
        truncated=truncated,
    )


def _continuation_limit(
    section_limit: int,
    *,
    n_ctx: int | None,
    n_past: int,
    prompt_tokens: int | None,
) -> int:
    if n_ctx is not None:
        return compute_budget(
            n_ctx=n_ctx,
            prompt_tokens=prompt_tokens,
            n_past=None if prompt_tokens is not None else n_past,
            phase="continuation",
        ).max_tokens
    return min(HARD_CAP["continuation"], section_limit)


def _limit(
    target_tokens: int,
    *,
    n_ctx: int | None,
    n_past: int,
    prompt_tokens: int | None = None,
) -> int:
    if n_ctx is not None:
        return compute_budget(
            n_ctx=n_ctx,
            prompt_tokens=prompt_tokens,
            n_past=None if prompt_tokens is not None else n_past,
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
