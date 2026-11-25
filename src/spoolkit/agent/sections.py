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
from spoolkit.agent.metrics import (
    commit_section,
    metric_scope,
    note_empty_section,
    note_retry,
    reset_section_progress,
)
from spoolkit.agent.final_turn import CONTINUATION_INSTRUCTION, SHORT_CONTINUE
from spoolkit.agent.structure import cut_before_next_heading, fences_closed
from spoolkit.agent.writing_state import (
    WritingState,
    apply_user_stop,
    fallback_outline,
    merge_decisions,
    request_outline,
    split_section_decisions,
)
from spoolkit.context.phase_budget import HARD_CAP, compute_budget
from spoolkit.llm.cancel import GenerationCancelled
from spoolkit.llm.llamacpp import EmptyContentError
from spoolkit.llm.types import ChatRequest, ChatResponse, Message

logger = logging.getLogger(__name__)

STOP_MARK = "已停止。已提交的节保留。"

SECTION_INSTRUCTION = (
    "只写第 {index} 节「{title}」。要点：{points}。目标约 {target_tokens} tokens。\n"
    "不要写其他节，不要重复已写内容，不要总结全文。\n"
    "节末用「决定：」单独成行写出 1–3 条结论，每条不超过 40 字。"
)
SECTION_SLACK = 200
EMPTY_SECTION = "（本节没有生成。）"
EMPTY_RETRY_INSTRUCTION = (
    "必须输出正文，至少 100 字。没有材料时基于目标写，不要留空。"
)


@dataclass(frozen=True)
class SectionDraft:
    index: int
    title: str
    text: str
    prompt_tokens: int
    truncated: bool
    decisions: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssemblyFailure:
    reason: str


class WritingStopped(Exception):
    """这一节还没提交就停了。drafts 只含已经写完的节。"""

    def __init__(self, drafts: tuple[SectionDraft, ...], effect: Any) -> None:
        super().__init__(STOP_MARK)
        self.drafts = drafts
        self.effect = effect


def compose_long_answer(
    gateway: Any,
    prefix: Sequence[Message],
    *,
    goal: str,
    facts: str,
    token_budget: int,
    n_ctx: int | None = None,
    on_event: Any = None,
) -> str | None:
    """大纲 → 逐节 → 组装。大纲没收下时用固定三节继续。组装失败时返回 None。

    用户停掉当前请求时，返回已经写完的节，不再发下一次请求。
    """
    try:
        state = request_outline(
            gateway,
            prefix,
            goal=goal,
            token_budget=token_budget,
        )
    except GenerationCancelled:
        return f"（{STOP_MARK}还没有提交的节。）"
    if not isinstance(state, WritingState):
        logger.warning("长文大纲没有收下：%s，改用固定三节", getattr(state, "reason", ""))
        state = fallback_outline(goal, token_budget)
    try:
        drafts = write_sections(
            gateway,
            state,
            facts=facts,
            n_ctx=n_ctx,
            on_event=on_event,
            cancel=getattr(gateway, "cancel_token", None),
        )
    except WritingStopped as stopped:
        kept = replace(state, outline=state.outline[: len(stopped.drafts)])
        partial = assemble(kept, stopped.drafts) if stopped.drafts else ""
        body = partial if isinstance(partial, str) else ""
        index = stopped.effect.incomplete
        which = f"第 {index + 1} 节未完成。" if index is not None else ""
        return f"{body}\n\n（{STOP_MARK}{which}）".strip()
    text = assemble(state, drafts)
    if not isinstance(text, str):
        logger.warning("长文组装失败：%s", text.reason)
        return None
    return text


def _empty_draft(state: WritingState, title: str, exc: EmptyContentError) -> SectionDraft:
    """重试仍空：这一节用固定句子占位，后面的节继续写。"""
    if exc.context_tight:
        logger.warning(
            "本节空内容：prompt_tokens=%s 接近 n_ctx=%s 的余量",
            exc.prompt_tokens,
            exc.n_ctx,
        )
    else:
        logger.warning("本节空内容，重试后仍空")
    note_empty_section()
    response = ChatResponse(
        text="",
        prompt_tokens=exc.prompt_tokens,
        completion_tokens=0,
        truncated=True,
    )
    return _draft(state, title, response, EMPTY_SECTION, True)


def _ask(gateway: Any, request: ChatRequest) -> ChatResponse:
    """有流式入口就走流式，停止时才能关掉连接。"""
    stream = getattr(gateway, "chat_stream", None)
    if callable(stream):
        response: ChatResponse = stream(request, on_delta=None)
        return response
    return gateway.chat(request)


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
    """写当前节。system 只放大纲和约束，本节要求放在 user 末尾。"""
    if not 0 <= state.next_section < len(state.outline):
        raise ValueError("没有这一节")
    section = state.outline[state.next_section]
    request = _section_request(
        _messages(state, facts),
        _limit(
            section.target_tokens,
            n_ctx=n_ctx,
            n_past=n_past,
            prompt_tokens=prompt_tokens,
        ),
    )
    with metric_scope(
        phase="section",
        task_type="long",
        n_ctx=n_ctx or 0,
        target_tokens=section.target_tokens,
    ):
        try:
            response: ChatResponse = _ask(gateway, request)
        except EmptyContentError as exc:
            return _empty_draft(state, section.title, exc)
    kept = cut_before_next_heading(response.text)
    structural = not response.truncated and not fences_closed(kept)
    if not response.truncated and not structural:
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
        try:
            follow: ChatResponse = _ask(
                gateway,
                _section_request(
                    request.messages + (
                        Message(role="user", content=CONTINUATION_INSTRUCTION),
                    ),
                    follow_limit,
                ),
            )
        except EmptyContentError as exc:
            return _empty_draft(state, section.title, exc)
    merged = merge_continuation(
        response.text, follow.text, overlap=overlap_chars(overlap)
    )
    if follow.truncated or not follow.text.strip():
        logger.warning("本节续写未完成，改为短答并留下继续入口")
        return _draft(state, section.title, follow, merged + SHORT_CONTINUE, True)
    if not structural and looks_repeated(response.text, follow.text):
        logger.warning("本节续写与已写内容重复，重写该节")
        with metric_scope(
            phase="section",
            task_type="long",
            n_ctx=n_ctx or 0,
            retry_index=2,
            recovery="rewrite",
            target_tokens=section.target_tokens,
        ):
            try:
                rewrite: ChatResponse = _ask(
                    gateway,
                    _section_request(request.messages, request.max_tokens),
                )
            except EmptyContentError as exc:
                return _empty_draft(state, section.title, exc)
        if rewrite.truncated or looks_repeated(response.text, rewrite.text):
            logger.warning("本节重写未完成，改为短答并留下继续入口")
            kept = rewrite.text.strip() or response.text
            return _draft(state, section.title, rewrite, kept + SHORT_CONTINUE, True)
        return _draft(state, section.title, rewrite, rewrite.text.strip(), False)
    return _draft(state, section.title, follow, merged.strip(), False)


def _stopped(state: WritingState, drafts: list[SectionDraft], index: int) -> WritingStopped:
    effect = apply_user_stop(
        replace(state, next_section=index, completed=tuple(range(index))),
        phase="section",
        note="用户停止",
    )
    return WritingStopped(tuple(drafts), effect)


def write_sections(
    gateway: Any,
    state: WritingState,
    *,
    facts: str,
    n_ctx: int | None = None,
    n_past: int = 0,
    prompt_tokens: int | None = None,
    on_event: Any = None,
    cancel: Any = None,
) -> tuple[SectionDraft, ...]:
    """按大纲顺序逐节写。每一节都用同一份事实，不把前文拼回去。"""
    total = len(state.outline)
    reset_section_progress()
    drafts: list[SectionDraft] = []
    carried = state.decisions
    for index in range(total):
        if cancel is not None and getattr(cancel, "cancelled", False):
            raise _stopped(state, drafts, index)
        section = state.outline[index]
        _emit_section(
            on_event,
            "section_start",
            index=index,
            title=section.title,
            total=total,
            state="draft",
        )
        current = replace(state, next_section=index, decisions=carried)
        try:
            draft = generate_section(
                gateway,
                current,
                facts=facts,
                n_ctx=n_ctx,
                n_past=n_past,
                prompt_tokens=prompt_tokens,
            )
        except GenerationCancelled:
            raise _stopped(state, drafts, index) from None
        drafts.append(draft)
        carried = draft.decisions
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


def _section_request(messages: tuple[Message, ...], max_tokens: int) -> ChatRequest:
    """节请求带上换提示重试。空了不能把同一段再发一遍。"""
    return ChatRequest(
        messages=messages,
        max_tokens=max_tokens,
        response_schema=None,
        cache_prompt=True,
        empty_retry=EMPTY_RETRY_INSTRUCTION,
    )


def _messages(state: WritingState, facts: str) -> tuple[Message, ...]:
    section = state.outline[state.next_section]
    lines = [f"目标：{state.goal}", "", "大纲："]
    for index, item in enumerate(state.outline, start=1):
        lines.append(f"{index}. {item.title}｜{item.points}｜{item.target_tokens}")
    if state.decisions:
        lines.extend(["", "已定约束："])
        lines.extend(f"- {item}" for item in state.decisions)
    instruction = SECTION_INSTRUCTION.format(
        index=state.next_section + 1,
        title=section.title,
        points=section.points,
        target_tokens=section.target_tokens,
    )
    body = facts.strip()
    user = f"{body}\n\n{instruction}" if body else instruction
    return (
        Message(role="system", content="\n".join(lines)),
        Message(role="user", content=user),
    )


def _draft(
    state: WritingState,
    title: str,
    response: ChatResponse,
    text: str,
    truncated: bool,
) -> SectionDraft:
    raw = cut_before_next_heading(text)
    suffix = ""
    if raw.endswith(SHORT_CONTINUE):
        raw = raw[: -len(SHORT_CONTINUE)]
        suffix = SHORT_CONTINUE
    body, found = split_section_decisions(raw)
    return SectionDraft(
        index=state.next_section,
        title=title,
        text=body + suffix,
        prompt_tokens=response.prompt_tokens,
        truncated=truncated,
        decisions=merge_decisions(state.decisions, found),
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
    allowance = min(HARD_CAP["section"], max(0, target_tokens) + SECTION_SLACK)
    if n_ctx is not None:
        return compute_budget(
            n_ctx=n_ctx,
            prompt_tokens=prompt_tokens,
            n_past=None if prompt_tokens is not None else n_past,
            phase="section",
            target_tokens=allowance,
        ).max_tokens
    return allowance


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
