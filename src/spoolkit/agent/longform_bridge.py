"""把现有大纲接到 DocumentState，再在终稿路径上跑 LongFormRunner。

不另写规划器。大纲仍来自 sections.request_outline；没收下就返回 None，
由 compose_long_answer 走原来的 fallback_outline。
target_chars 按中文离线口径估算：1 token 约等于 1 字。这是写作目标，
不是 450 字的通过线。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spoolkit.agent.artifacts import ArtifactRecord, ArtifactSection, ArtifactStore
from spoolkit.agent.chat_sink import CollectingChatSink
from spoolkit.agent.document_bodies import InMemoryBodyStore, section_ref
from spoolkit.agent.document_checkpoint import CheckpointError, DocumentCheckpointStore
from spoolkit.agent.document_state import (
    SUMMARY_CAP,
    DocumentState,
    OutlineEntry,
    SchemaVersionError,
)
from spoolkit.agent.file_sink import FileSink, FileSinkError
from spoolkit.agent.longform import LongFormRunner
from spoolkit.agent.protocol import AnswerPlan
from spoolkit.agent.sections import fallback_outline, request_outline
from spoolkit.agent.writing_state import OutlineFailure, WritingState
from spoolkit.errors import PathOutsideProjectError
from spoolkit.llm.types import Message

# 与 OfflineTokenCounter 的非 ASCII 规则一致：一个汉字计 1 token，也计 1 字。
CHARS_PER_TOKEN = 1
_WRITE_ACTIONS = ("写", "保存", "输出", "生成", "导出", "存成", "存到")
_GOAL_PATH = re.compile(
    r"(?<![A-Za-z0-9_./:\\-])"
    r"((?:[A-Za-z0-9_\-\u4e00-\u9fff]+/)*)"
    r"([A-Za-z0-9_\-\u4e00-\u9fff]+)"
    r"\.(md|markdown|txt|rst|text)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class LongformDraft:
    text: str
    sections: int
    chars: int
    deliverable: str = "chat"
    artifact_id: str = ""
    path: str = ""


def target_chars_for(target_tokens: int) -> int:
    """把大纲上的目标 tokens 换成目标字数。至少 1 字。"""
    return max(1, target_tokens * CHARS_PER_TOKEN)


def document_state_from_writing(
    state: WritingState,
    *,
    deliverable: str = "chat",
    path: str = "",
) -> DocumentState | None:
    """同一套字段转换。request_outline 和 fallback_outline 都走这里。"""
    if not state.outline:
        return None
    if deliverable not in ("chat", "file"):
        return None
    relative = ""
    if deliverable == "file":
        relative = _safe_file_path(path) or ""
        if not relative:
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
        deliverable=deliverable,  # type: ignore[arg-type]
        path=relative,
        phase="plan",
        pending_sections=tuple(range(len(outline))),
    )


_CN_SECTION = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SECTION_COUNT = re.compile(r"([一二两三四五六七八九十]|\d{1,2})\s*节")


def sections_required(goal: str, plan: AnswerPlan | None) -> int | None:
    """计划里的节数优先。没有计划时，只认目标里唯一的「三节」这类说法。"""
    if plan is not None and isinstance(plan.sections, int) and plan.sections > 0:
        return plan.sections
    found: list[int] = []
    for match in _SECTION_COUNT.finditer(str(goal or "")):
        raw = match.group(1)
        count = _CN_SECTION[raw] if raw in _CN_SECTION else int(raw)
        if count not in found:
            found.append(count)
    if len(found) == 1:
        return found[0]
    return None


def _outline_matches(written: object, expected: int | None) -> bool:
    if expected is None:
        return isinstance(written, WritingState)
    return isinstance(written, WritingState) and len(written.outline) == expected


def build_document_state(
    goal: str,
    plan: AnswerPlan | None,
    prefix: Sequence[Message],
    gateway: Any,
    *,
    token_budget: int,
) -> DocumentState | None:
    """要一份大纲并收成 plan 阶段的 DocumentState。失败时返回 None。"""
    if token_budget < 1 or not goal.strip():
        return None
    delivery = _delivery(plan)
    if delivery is None:
        return None
    deliverable, path = delivery
    expected = sections_required(goal, plan)
    written = request_outline(
        gateway,
        prefix,
        goal=goal,
        token_budget=token_budget,
        sections=expected,
    )
    if expected is not None and not _outline_matches(written, expected):
        written = request_outline(
            gateway,
            prefix,
            goal=goal,
            token_budget=token_budget,
            sections=expected,
            correction="上一份大纲节数不符，作废。按这个节数重新输出。",
        )
    if isinstance(written, OutlineFailure) or not isinstance(written, WritingState):
        return None
    if expected is not None and len(written.outline) != expected:
        return None
    return document_state_from_writing(written, deliverable=deliverable, path=path)


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


def artifact_session(pending: Any) -> str:
    """artifact 按会话分目录，不按单次 run。

    优先 `pending.run_session`。它为空时回退 `pending.session`，
    仍为空再用 `pending.run_id`。同一会话里的多次 run 因此落在
    `.agent/artifacts/<session>/`。三者都空时，目录名再由调用方收成 `run`。
    """
    if pending is None:
        return ""
    for name in ("run_session", "session", "run_id"):
        value = str(getattr(pending, name, "") or "").strip()
        if value:
            return value
    return ""


def try_longform_text(
    *,
    gateway: Any,
    prefix: Sequence[Message],
    goal: str,
    plan: AnswerPlan | None,
    token_budget: int,
    pending: Any = None,
    artifact_root: Path | None = None,
    session: str = "",
    task_id: str = "",
    turn_id: str = "",
    resume_checkpoint: str = "",
) -> LongformDraft | None:
    """跑完并校验通过才返回。file 成功时 text 只是待确认摘要，不是全文。

    给了 resume_checkpoint 就只读那一份进度。找不到就不规划大纲，也不启动写作。
    """
    resume_key = str(resume_checkpoint or "").strip()
    if plan is None and not resume_key:
        inferred = explicit_file_path(goal, pending)
        if inferred:
            plan = AnswerPlan(
                mode="long",
                target_tokens=max(1, token_budget),
                deliverable="file",
                path=inferred,
            )
    if resume_key:
        restored = _restore_checkpoint(
            artifact_root,
            session or artifact_session(pending),
            resume_key,
        )
        if restored is None:
            return None
        state, bodies = restored
    else:
        state = build_document_state(goal, plan, prefix, gateway, token_budget=token_budget)
        if state is None:
            return None
        bodies = InMemoryBodyStore()
    task = task_id or "task"
    turn = turn_id or "turn"
    if state.deliverable == "file":
        checkpoint_session = _session_segment(session)
        checkpoint_artifact = _artifact_id(session, task, turn, state.path)
    else:
        checkpoint_session = _session_segment(artifact_session(pending) or session)
        checkpoint_artifact = _artifact_id(checkpoint_session, task, turn, "")
    sink: Any
    if state.deliverable == "file":
        if pending is None or artifact_root is None:
            return None
        try:
            sink = FileSink(
                state=state,
                path=state.path,
                session=checkpoint_session,
                task_id=task,
                turn_id=turn,
                artifacts=ArtifactStore(artifact_root),
                pending=pending,
                artifact_id=checkpoint_artifact,
            )
        except FileSinkError:
            return None
    else:
        sink = CollectingChatSink()
    result = LongFormRunner(
        state,
        gateway,
        bodies,
        sink,
        checkpoint=DocumentCheckpointStore(artifact_root) if artifact_root is not None else None,
        session=checkpoint_session,
        artifact_id=checkpoint_artifact,
        task_id=task,
        turn_id=turn,
        checkpoint_key_value=resume_key,
    ).run()
    if not result.done:
        return None
    written = result.state
    chars = 0
    for entry in written.outline:
        chars += len(bodies.get(section_ref(entry.index)).strip())
    if state.deliverable == "file":
        if sink.pending is None or sink.record is None:
            return None
        return LongformDraft(
            text=f"已生成文档，待确认：{sink.path}",
            sections=len(written.outline),
            chars=chars,
            deliverable="file",
            artifact_id=sink.artifact_id,
            path=sink.path,
        )
    text = render_document(written, bodies)
    if not text:
        return None
    artifact_id = ""
    stored_path = ""
    if artifact_root is not None and (plan is None or plan.deliverable != "both"):
        artifact_id, stored_path = _store_chat(
            written,
            bodies,
            text,
            root=artifact_root,
            pending=pending,
            session=session,
            task_id=task_id,
            turn_id=turn_id,
        )
    return LongformDraft(
        text=text,
        sections=len(written.outline),
        chars=chars,
        artifact_id=artifact_id,
        path=stored_path,
    )


def _restore_checkpoint(
    root: Path | None,
    session: str,
    key: str,
) -> tuple[DocumentState, InMemoryBodyStore] | None:
    """把这份 checkpoint 的进度和已写节装回来。正文只进内存，不交给调用方的响应。"""
    if root is None:
        return None
    scope = _session_segment(session)
    try:
        loaded = DocumentCheckpointStore(root).load(scope, key)
    except (CheckpointError, SchemaVersionError, OSError, UnicodeError, ValueError):
        return None
    if loaded is None or loaded.state.status == "done":
        return None
    bodies = InMemoryBodyStore()
    for index, text in loaded.bodies:
        bodies.put(index, text)
    return loaded.state, bodies


def _store_chat(
    state: DocumentState,
    bodies: InMemoryBodyStore,
    text: str,
    *,
    root: Path,
    pending: Any,
    session: str,
    task_id: str,
    turn_id: str,
) -> tuple[str, str]:
    """chat 成功后把全文写入 artifact。元数据只留摘要。"""
    resolved = _session_segment(artifact_session(pending) or session)
    task = task_id or "task"
    turn = turn_id or "turn"
    artifact_id = _artifact_id(resolved, task, turn, "")
    sections = []
    for entry in state.outline:
        body = bodies.get(section_ref(entry.index)).strip()
        sections.append(
            ArtifactSection(
                index=entry.index,
                title=entry.title,
                summary=_excerpt(body),
                char_count=len(body),
            )
        )
    record = ArtifactStore(root).put(
        ArtifactRecord(
            id=artifact_id,
            session=resolved,
            task_id=task,
            turn_id=turn,
            kind="document",
            title=_excerpt(state.outline[0].title),
            summary=_excerpt(state.goal),
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            sections=tuple(sections),
        ),
        text,
    )
    return record.id, record.path


def _excerpt(text: str) -> str:
    compact = "".join(line.strip() for line in text.splitlines() if line.strip())
    if len(compact) <= SUMMARY_CAP:
        return compact
    return compact[: SUMMARY_CAP - 1] + "…"


def explicit_file_path(goal: str, pending: Any) -> str | None:
    """计划缺失时，从目标里取出唯一的工作区文本路径。

    必须同时有写、保存、输出或生成这类动作，以及一个相对文本路径。
    绝对路径、上级目录和多个路径都不取。工作区校验不通过就当没有。
    """
    text = str(goal or "")
    if pending is None or not any(word in text for word in _WRITE_ACTIONS):
        return None
    found: list[str] = []
    for match in _GOAL_PATH.finditer(text):
        relative = f"{match.group(1)}{match.group(2)}.{match.group(3).lower()}"
        if relative not in found:
            found.append(relative)
    if len(found) != 1:
        return None
    candidate = found[0]
    if _safe_file_path(candidate) != candidate:
        return None
    try:
        stored = pending.path_of(candidate)
    except PathOutsideProjectError:
        return None
    if not isinstance(stored, str) or Path(stored).is_absolute() or ".." in Path(stored).parts:
        return None
    if stored.replace("\\", "/") != candidate:
        return None
    return candidate


def _delivery(plan: AnswerPlan | None) -> tuple[str, str] | None:
    """file 路径不成立时返回 None。both 暂不支持，按 chat 写，不带路径。"""
    deliverable = "chat" if plan is None else plan.deliverable
    if deliverable == "both":
        return ("chat", "")
    if deliverable == "chat":
        return ("chat", "")
    if deliverable != "file":
        return None
    relative = _safe_file_path("" if plan is None else plan.path)
    if relative is None:
        return None
    return ("file", relative)


def _safe_file_path(path: str) -> str | None:
    text = str(path).strip()
    if not text:
        return None
    raw = Path(text)
    if raw.is_absolute() or text.startswith(("/", "\\")) or ":" in text:
        return None
    parts = [part for part in raw.parts if part != "."]
    if not parts or any(part in ("..", "") for part in parts):
        return None
    return "/".join(parts)


def _session_segment(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "-" for char in value.strip())
    cleaned = cleaned.strip(".-")[:80]
    return cleaned or "run"


def _artifact_id(session: str, task_id: str, turn_id: str, path: str) -> str:
    digest = hashlib.sha256(f"{session}|{task_id}|{turn_id}|{path}".encode("utf-8")).hexdigest()[:16]
    return f"doc-{digest}"


def fallback_document_state(goal: str, budget: int) -> DocumentState | None:
    """固定三节也用同一套字数换算。终稿失败回退不走这里，仍由 compose 调用。"""
    return document_state_from_writing(fallback_outline(goal, budget))
