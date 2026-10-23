"""显式长文 runner。

不造通用图运行时。下面这些 phase 按固定顺序走，每次只做一件事：
finish_current、repair_current、rewrite_current、write_pending、
repair_pending、rewrite_pending、validate、finalize。
正文在 BodyStore。state 只更新字数、摘要和尾部。
通过线是 length_floor。outline.target_chars 只写进提示，不是通过线。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from spoolkit.agent.chat_sink import ChatSink
from spoolkit.agent.document_bodies import InMemoryBodyStore, section_ref
from spoolkit.agent.document_source import SourceContext
from spoolkit.agent.document_state import (
    SUMMARY_CAP,
    TAIL_CAP,
    CompletedSection,
    CurrentSection,
    DocumentState,
    has_think,
    overlap_ratio,
    repeats_existing,
    scaffold_hits,
    section_repeats,
    sentence_closed,
)
from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.types import ChatRequest, Message

MAX_REPAIR = 2
FINISH_MAX = 1500
REPAIR_MAX = 800
REWRITE_MAX = 1200
WRITE_MAX = 1500
FINAL_MAX = 32
PROMPT_TAIL = 80

SYSTEM = (
    "你是技术写作者。只输出 Markdown 正文。"
    "不要输出思考过程，不要调用工具，不要解释你在做什么。"
)
GUARD_RETRY = "只输出中文 Markdown 正文，不要思考过程/英文分析。"
SCAFFOLD_RETRY = "只输出正文，不复述提示词结构。"
NOVELTY_RETRY = "只写新的句子，不要重复已有内容。"
DONE_RETRY = "不要写「完」。"

_STATE_PHASE = {
    "finish_current": "continue",
    "repair_current": "repair",
    "repair_pending": "repair",
    "rewrite_current": "rewrite",
    "rewrite_pending": "rewrite",
    "write_pending": "write",
    "validate": "commit",
    "finalize": "finalize",
}


class LongFormError(Exception):
    """没有可写的节，或当前节缺少正文。"""


@dataclass(frozen=True, slots=True)
class PhaseStep:
    phase: str
    index: int
    accepted: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class LongFormResult:
    state: DocumentState
    steps: tuple[PhaseStep, ...]
    reasons: tuple[str, ...]
    done: bool
    ending: str
    failures: tuple[tuple[int, str], ...]


def finish_messages(
    title: str,
    index: int,
    tail: str,
    source: SourceContext | None = None,
) -> tuple[Message, ...]:
    user = _attach_source(
        f"当前节：第 {index} 节「{title}」。只补完这一节，不要写其他标题，不要写「完」。",
        source,
    )
    follow = "只从最后一个字之后继续，把这一节收成完整段落。不要写其他节，不要写「完」。"
    items = [Message("system", SYSTEM), Message("user", user)]
    if tail.strip():
        items.append(Message("assistant", tail.strip()[-PROMPT_TAIL:]))
        items.append(Message("user", follow))
    else:
        items.append(Message("user", follow))
    return tuple(items)


def repair_messages(
    *,
    title: str,
    points: str,
    target_chars: int,
    body: str,
    current: bool,
    source: SourceContext | None = None,
) -> tuple[Message, ...]:
    gap = max(0, target_chars - len(body.strip()))
    verb = "只补完当前节" if current else "只补完这一节"
    user = _attach_source("\n".join(
        [
            f"这一节：## {title}",
            f"要点：{points}",
            f"目标剩余字数：{gap}",
            f"{verb}，不写下一节，不写「完」。",
        ]
    ), source)
    follow = f"只从最后一个字之后继续。{verb}，不写下一节，不写「完」。"
    items = [Message("system", SYSTEM), Message("user", user)]
    tail = body.strip()[-PROMPT_TAIL:]
    if tail:
        items.append(Message("assistant", tail))
        items.append(Message("user", follow))
    else:
        items.append(Message("user", follow))
    return tuple(items)


def rewrite_messages(
    *,
    title: str,
    points: str,
    previous_summary: str,
    target_chars: int,
    source: SourceContext | None = None,
) -> tuple[Message, ...]:
    user = _attach_source("\n".join(
        [
            f"重写当前节：## {title}",
            f"要点：{points}",
            f"上一节摘要：{previous_summary}",
            f"目标字数：{target_chars}",
            "只输出这一节的完整正文。不要写其他标题，不要写「完」，不要写思考过程。",
        ]
    ), source)
    return (Message("system", SYSTEM), Message("user", user))


def write_pending_messages(
    *,
    title: str,
    points: str,
    target_chars: int,
    summaries: str,
    source: SourceContext | None = None,
) -> tuple[Message, ...]:
    user = _attach_source("\n\n".join(
        [
            "已完成节摘要：\n" + (summaries or "（还没有）"),
            f"未开始标题：## {title}：目标 {target_chars} 字。要点：{points}",
            (
                "只写这一节的正文，写到目标字数，收成完整段落。"
                "不要写「完」。不要把大纲要点原样抄一遍。不要重复已完成的节。"
            ),
        ]
    ), source)
    return (Message("system", SYSTEM), Message("user", user))


def finalize_messages() -> tuple[Message, ...]:
    return (
        Message("system", SYSTEM),
        Message("user", "各节都已写完。只输出最后一行「完」。不要写其他内容。"),
    )


class LongFormRunner:
    """持有 state、gateway 和 BodyStore。调用方的 state 对象不会被改写。

    sink 可选。它只接收已经接受的节正文，异常不会改变 runner 的结果。
    """

    def __init__(
        self,
        state: DocumentState,
        gateway: ModelGateway,
        bodies: InMemoryBodyStore,
        sink: ChatSink | None = None,
        source: SourceContext | tuple[SourceContext, ...] | None = None,
    ) -> None:
        if state.current_section is None:
            if not state.pending_sections:
                raise LongFormError("没有可写的节")
            origin = state.pending_sections[0]
        else:
            if not bodies.exists(section_ref(state.current_section.index)):
                raise LongFormError("当前节没有正文")
            origin = state.current_section.index
        self.state = state
        self.bodies = bodies
        self._gateway = gateway
        self._origin = origin
        self._sink = sink
        self._sources = _contexts(source)
        self._steps: list[PhaseStep] = []
        self._failures: dict[int, str] = {}
        self._anchor_unresolved = False
        self._published: set[int] = set()
        # 已经发给 sink 的节。校验重开后再提交同一节时必须 replace，
        # 否则聊天层会把同一节追加两遍。
        self._committed: set[int] = set()

    def run(self) -> LongFormResult:
        self._notify("start", self.state)
        self._open_from_outline()
        self._drive_current()
        while self.state.pending_sections:
            self._drive_pending()
        reasons = self._validate_and_route()
        ending = self._finalize() if not reasons else ""
        done = not reasons and ending == "完"
        if done:
            self.state = replace(self.state, phase="finalize", status="done", error="")
        else:
            self.state = replace(
                self.state,
                phase=self.state.phase if self.state.phase == "finalize" else "commit",
                status="failed",
                error=",".join(reasons) if reasons else "finalize",
            )
        result = LongFormResult(
            state=self.state,
            steps=tuple(self._steps),
            reasons=reasons,
            done=done,
            ending=ending,
            failures=tuple(sorted(self._failures.items())),
        )
        if done:
            self._notify("complete", result)
        else:
            self._notify("fail", reasons if reasons else ("finalize",))
        return result

    def _open_from_outline(self) -> None:
        if self.state.current_section is not None:
            return
        if not self.state.pending_sections:
            raise LongFormError("没有可写的节")
        index = self.state.pending_sections[0]
        entry = self.state.outline[index]
        self.bodies.put(index, "")
        phase = "write" if self.state.phase == "plan" else self.state.phase
        self.state = replace(
            self.state,
            current_section=CurrentSection(index, entry.title, "", "", 0, "open"),
            pending_sections=self.state.pending_sections[1:],
            phase=phase,
        )
        self._origin = index

    def _drive_current(self) -> None:
        current = self.state.current_section
        if current is None:
            return
        index = current.index
        self._notify("section_start", index, current.title)
        if not self._section_ok(index):
            hard = self._finish(index)
            if hard and not self._section_ok(index):
                self._rewrite(index, "rewrite_current")
            else:
                self._recover(index, "current")
        self._commit_current()

    def _drive_pending(self) -> None:
        index = self.state.pending_sections[0]
        entry = self.state.outline[index]
        self.state = replace(
            self.state,
            current_section=CurrentSection(index, entry.title, "", "", 0, "open"),
            pending_sections=self.state.pending_sections[1:],
            phase="write",
        )
        self._notify("section_start", index, entry.title)
        hard = self._write(index)
        if hard and not self._section_ok(index):
            self._rewrite(index, "rewrite_pending")
        else:
            self._recover(index, "pending")
        self._commit_current()

    def _recover(self, index: int, kind: str) -> None:
        last_failed = False
        while True:
            if self._section_ok(index) and not last_failed:
                return
            if self.state.rewrite_attempts[index] >= 1:
                return
            if self.state.repair_attempts[index] < MAX_REPAIR and not last_failed:
                last_failed = not self._repair(index, f"repair_{kind}")
                continue
            self._rewrite(index, f"rewrite_{kind}")
            return

    def _validate_and_route(self) -> tuple[str, ...]:
        reasons: tuple[str, ...] = ()
        for _ in range(len(self.state.outline) * 3):
            reasons = self._check()
            self._record("validate", -1, not reasons, ",".join(reasons))
            if not reasons:
                return ()
            index = self._pick(reasons)
            if index is None:
                return reasons
            self._reopen(index)
            kind = "current" if index == self._origin else "pending"
            if self.state.repair_attempts[index] < MAX_REPAIR:
                applied = self._repair(index, f"repair_{kind}")
                if not applied and self.state.rewrite_attempts[index] < 1:
                    self._rewrite(index, f"rewrite_{kind}")
            elif self.state.rewrite_attempts[index] < 1:
                self._rewrite(index, f"rewrite_{kind}")
            self._commit_current()
        reasons = self._check()
        self._record("validate", -1, not reasons, ",".join(reasons))
        return reasons

    def _finish(self, index: int) -> bool:
        entry = self.state.outline[index]
        body = self.bodies.get(section_ref(index))
        text, reason, rejected = self._accept(
            finish_messages(entry.title, index + 1, _prompt_tail(body), self._source_for(index)),
            FINISH_MAX,
            allow_done=False,
        )
        if text is None:
            self._record("finish_current", index, False, reason)
            return True
        if rejected and len(body.strip()) >= 12 and _anchor_lost(body, rejected, text):
            self._anchor_unresolved = True
            self._record("finish_current", index, False, "anchor_lost")
            return True
        merged = _append(body, _strip_heading(entry.title, text))
        if _blocked(merged):
            self._record("finish_current", index, False, "early_done")
            return True
        self._store(index, merged)
        self._published.add(index)
        self._sync(index, "open")
        self._record("finish_current", index, True, "")
        return False

    def _write(self, index: int) -> bool:
        entry = self.state.outline[index]
        text, reason, _rejected = self._accept(
            write_pending_messages(
                title=entry.title,
                points=entry.points,
                target_chars=entry.target_chars,
                summaries=self._completed_summaries(),
                source=self._source_for(index),
            ),
            WRITE_MAX,
            allow_done=False,
        )
        if text is None:
            self._record("write_pending", index, False, reason)
            return True
        cleaned = _strip_heading(entry.title, text)
        if not cleaned or _blocked(cleaned):
            self._record("write_pending", index, False, reason or "early_done")
            return True
        self._store(index, cleaned)
        self._published.add(index)
        self._sync(index, "open")
        self._record("write_pending", index, True, "")
        return False

    def _repair(self, index: int, phase: str) -> bool:
        entry = self.state.outline[index]
        body = self.bodies.get(section_ref(index)) if self.bodies.exists(section_ref(index)) else ""
        self._bump("repair_attempts", index)
        self._sync_status(index, "repairing")
        messages = repair_messages(
            title=entry.title,
            points=entry.points,
            target_chars=entry.target_chars,
            body=body,
            current=phase.endswith("current"),
            source=self._source_for(index),
        )
        text, reason, _rejected = self._accept(messages, REPAIR_MAX, allow_done=False)
        if text is not None and (repeats_existing(body, text) or section_repeats(_append(body, text))):
            text, reason, _rejected = self._accept(
                _with_instruction(messages, NOVELTY_RETRY),
                REPAIR_MAX,
                allow_done=False,
            )
            if text is not None and repeats_existing(body, text):
                text, reason = None, "repeated"
        if text is None:
            self._failures[index] = reason or "repair_rejected"
            self._record(phase, index, False, self._failures[index])
            return False
        merged = _append(body, _strip_heading(entry.title, text))
        if _blocked(merged):
            self._failures[index] = "early_done"
            self._record(phase, index, False, "early_done")
            return False
        self._store(index, merged)
        self._published.add(index)
        self._sync(index, "repairing")
        self._record(phase, index, True, "")
        return True

    def _rewrite(self, index: int, phase: str) -> bool:
        entry = self.state.outline[index]
        ref = section_ref(index)
        old = self.bodies.get(ref) if self.bodies.exists(ref) else ""
        self._bump("rewrite_attempts", index)
        self._sync_status(index, "rewriting")
        text, reason, _rejected = self._accept(
            rewrite_messages(
                title=entry.title,
                points=entry.points,
                previous_summary=self._previous_summary(index),
                target_chars=entry.target_chars,
                source=self._source_for(index),
            ),
            REWRITE_MAX,
            allow_done=False,
        )
        cleaned = "" if text is None else _strip_heading(entry.title, text)
        if cleaned and self._rewrite_ok(old, cleaned, entry.points):
            self._store(index, cleaned)
            self._published.add(index)
            self._sync(index, "closed")
            self._failures.pop(index, None)
            if index == self._origin:
                self._anchor_unresolved = False
            self._record(phase, index, True, "")
            return True
        why = reason or "rewrite_rejected"
        self._failures[index] = why
        self._record(phase, index, False, why)
        return False

    def _finalize(self) -> str:
        text, reason, _rejected = self._accept(finalize_messages(), FINAL_MAX, allow_done=True)
        ok = text == "完"
        self._record("finalize", -1, ok, "" if ok else reason or "finalize")
        return "完" if ok else ""

    def _accept(
        self,
        messages: tuple[Message, ...],
        max_tokens: int,
        *,
        allow_done: bool,
    ) -> tuple[str | None, str, str]:
        first = self._chat(messages, max_tokens)
        reason = _block_reason(first, allow_done=allow_done)
        if not reason:
            return first.strip(), "", ""
        if reason not in {"think", "scaffold", "early_done"}:
            return None, reason, first
        second = self._chat(_with_instruction(messages, _instruction(reason)), max_tokens)
        second_reason = _block_reason(second, allow_done=allow_done)
        if second_reason:
            return None, second_reason, first
        return second.strip(), "", first

    def _chat(self, messages: tuple[Message, ...], max_tokens: int) -> str:
        response = self._gateway.chat(ChatRequest(messages=messages, max_tokens=max_tokens))
        return response.text or ""

    def _check(self) -> tuple[str, ...]:
        bodies = []
        for index in range(len(self.state.outline)):
            ref = section_ref(index)
            bodies.append((index, self.bodies.get(ref) if self.bodies.exists(ref) else ""))
        return self.state.check_bodies(tuple(bodies), anchor_lost_unresolved=self._anchor_unresolved)

    def _pick(self, reasons: tuple[str, ...]) -> int | None:
        if {"short_section", "open_section", "scaffold", "think", "repeated"} & set(reasons):
            for index in range(len(self.state.outline)):
                if self._section_ok(index):
                    continue
                if self._has_budget(index):
                    return index
            return None
        if "short_doc" in reasons:
            for index in range(len(self.state.outline) - 1, -1, -1):
                if self._has_budget(index):
                    return index
        return None

    def _has_budget(self, index: int) -> bool:
        return self.state.repair_attempts[index] < MAX_REPAIR or self.state.rewrite_attempts[index] < 1

    def _section_ok(self, index: int) -> bool:
        ref = section_ref(index)
        if not self.bodies.exists(ref):
            return False
        body = self.bodies.get(ref).strip()
        if len(body) < self.state.length_floor or not sentence_closed(body):
            return False
        return not _blocked(body) and not section_repeats(body)

    def _rewrite_ok(self, old: str, new: str, points: str) -> bool:
        if _blocked(new) or not sentence_closed(new) or section_repeats(new):
            return False
        if len(new) < self.state.length_floor:
            return False
        if overlap_ratio(old, new) >= 0.5 or overlap_ratio(points, new) >= 0.5:
            return False
        return True

    def _reopen(self, index: int) -> None:
        found = None
        kept = []
        for item in self.state.completed_sections:
            if item.index == index:
                found = item
            else:
                kept.append(item)
        if found is None:
            raise LongFormError("无法重开这一节")
        body = self.bodies.get(section_ref(index))
        self.state = replace(
            self.state,
            completed_sections=tuple(kept),
            current_section=CurrentSection(
                index,
                found.title,
                found.summary,
                _tail(body),
                len(body.strip()),
                "repairing",
            ),
            pending_sections=(),
            phase="repair",
        )

    def _commit_current(self) -> None:
        current = self.state.current_section
        if current is None:
            return
        ref = section_ref(current.index)
        if not self.bodies.exists(ref):
            self.bodies.put(current.index, "")
        body = self.bodies.get(ref)
        if self._section_ok(current.index):
            self._failures.pop(current.index, None)
        elif current.index not in self._failures:
            self._failures[current.index] = "short_section"
        artifact_id = ""
        section_path = ""
        if self.state.deliverable in ("file", "both"):
            artifact_id = str(getattr(self._sink, "artifact_id", "") or "")
            if not artifact_id:
                section_path = self.state.path
        self.state = replace(
            self.state,
            completed_sections=self.state.completed_sections
            + (
                CompletedSection(
                    index=current.index,
                    title=current.title,
                    summary=_summary(body),
                    artifact_id=artifact_id,
                    path=section_path,
                    char_count=len(body.strip()),
                ),
            ),
            current_section=None,
            phase="commit",
        )
        if current.index in self._published and body.strip():
            if current.index in self._committed:
                self._notify("section_replace", current.index, body)
            else:
                self._committed.add(current.index)
                self._notify("section_commit", current.index, body)

    def _store(self, index: int, body: str) -> None:
        ref = section_ref(index)
        if self.bodies.exists(ref):
            self.bodies.update(ref, body)
        else:
            self.bodies.put(index, body)

    def _sync(self, index: int, status: str) -> None:
        body = self.bodies.get(section_ref(index))
        entry = self.state.outline[index]
        self.state = replace(
            self.state,
            current_section=CurrentSection(
                index,
                entry.title,
                _summary(body),
                _tail(body),
                len(body.strip()),
                status,  # type: ignore[arg-type]
            ),
        )

    def _sync_status(self, index: int, status: str) -> None:
        ref = section_ref(index)
        if self.bodies.exists(ref) and self.bodies.get(ref).strip():
            self._sync(index, status)
            return
        entry = self.state.outline[index]
        self.state = replace(
            self.state,
            current_section=CurrentSection(index, entry.title, "", "", 0, status),  # type: ignore[arg-type]
        )

    def _bump(self, field: str, index: int) -> None:
        values = list(getattr(self.state, field))
        values[index] += 1
        self.state = replace(self.state, **{field: tuple(values)})

    def _notify(self, name: str, *args: object) -> None:
        if self._sink is None:
            return
        method = getattr(self._sink, name, None)
        if not callable(method):
            return
        try:
            method(*args)
        except Exception:
            return

    def _record(self, phase: str, index: int, accepted: bool, reason: str = "") -> None:
        self._steps.append(PhaseStep(phase, index, accepted, reason))
        state_phase = _STATE_PHASE[phase]
        if self.state.phase != state_phase:
            self.state = replace(self.state, phase=state_phase)
        self._notify("progress", phase, index)

    def _source_for(self, index: int) -> SourceContext | None:
        for item in self._sources:
            if item.index == index:
                return item
        return None

    def _completed_summaries(self) -> str:
        lines = [f"- {item.title}：{item.summary or '（空）'}" for item in self.state.completed_sections]
        return "\n".join(lines) or "（还没有）"

    def _previous_summary(self, index: int) -> str:
        if index == 0:
            return "（没有上一节）"
        for item in self.state.completed_sections:
            if item.index == index - 1:
                return item.summary or "（空）"
        return "（空）"


def _contexts(source: SourceContext | tuple[SourceContext, ...] | None) -> tuple[SourceContext, ...]:
    if source is None:
        return ()
    if isinstance(source, SourceContext):
        return (source,)
    return tuple(source)


def _attach_source(text: str, source: SourceContext | None) -> str:
    if source is None:
        return text
    return "\n".join(
        [
            text,
            f"旧节标题：{source.title}",
            f"旧节摘要：{source.summary}",
            f"旧节摘录：{source.excerpt}",
            "只作为素材，不要照抄整段。",
        ]
    )


def _summary(body: str) -> str:
    compact = "".join(line.strip() for line in body.splitlines() if line.strip())
    return compact[:SUMMARY_CAP]


def _tail(body: str) -> str:
    return body.strip()[-TAIL_CAP:]


def _prompt_tail(body: str) -> str:
    return body.strip()[-PROMPT_TAIL:]


def _append(body: str, addition: str) -> str:
    addition = addition.strip()
    if not body.strip():
        return addition
    if not addition:
        return body
    return body.rstrip() + "\n" + addition


def _strip_heading(title: str, text: str) -> str:
    lines = text.strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip().lstrip("#").strip() == title:
        lines = lines[1:]
    return "\n".join(lines).strip()


def _has_done(text: str) -> bool:
    return any(line.strip() == "完" for line in text.splitlines())


def _blocked(text: str) -> bool:
    return bool(has_think(text) or scaffold_hits(text) or _has_done(text))


def _block_reason(text: str, *, allow_done: bool) -> str:
    if has_think(text):
        return "think"
    if scaffold_hits(text):
        return "scaffold"
    if not allow_done and _has_done(text):
        return "early_done"
    if not text.strip():
        return "empty"
    return ""


def _instruction(reason: str) -> str:
    if reason == "think":
        return GUARD_RETRY
    if reason == "scaffold":
        return SCAFFOLD_RETRY
    return DONE_RETRY


def _with_instruction(messages: tuple[Message, ...], instruction: str) -> tuple[Message, ...]:
    items = list(messages)
    last = items[-1]
    items[-1] = Message(last.role, last.content + "\n\n" + instruction)
    return tuple(items)


def _anchor_lost(tail: str, rejected: str, retry: str) -> bool:
    head = retry.strip()[:160]
    anchors: list[str] = []
    for source in (tail, rejected):
        snippet = source.strip()[-12:]
        if len(source.strip()) >= 12 and len(snippet) >= 12:
            anchors.append(snippet)
    if not anchors:
        return False
    return all(anchor not in head for anchor in anchors)
