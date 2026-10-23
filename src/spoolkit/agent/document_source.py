"""把已有 artifact 收成一份新的 DocumentState。

正文只在 DocumentSource 里用来切节。写进状态的是大纲、摘要和 artifact id。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from spoolkit.agent.artifacts import ArtifactRecord, ArtifactSection, ArtifactStore
from spoolkit.agent.document_state import SUMMARY_CAP, DocumentState, OutlineEntry

_HEADING = re.compile(r"^##(?!#)\s+(\S.*?)\s*$")
EXCERPT_CAP = 400


class DocumentSourceError(ValueError):
    """旧文档读不出来，或新的交付方式不成立。"""


@dataclass(frozen=True, slots=True)
class SourceSection:
    index: int
    title: str
    body: str
    summary: str
    char_count: int


@dataclass(frozen=True, slots=True)
class SourceContext:
    """一节旧文的素材。只有标题、摘要和不超过 400 字的摘录。"""

    index: int
    title: str
    summary: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class RecomposeResult:
    text: str
    state: DocumentState
    artifact_id: str
    path: str


@dataclass(frozen=True, slots=True)
class DocumentSource:
    artifact_id: str
    title: str
    summary: str
    content: str
    sections: tuple[SourceSection, ...]


def load_document_source(store: ArtifactStore, session: str, artifact_id: str) -> DocumentSource:
    """读一份已有 artifact，并按正文里的二级标题切节。"""
    record, content = store.get(session, artifact_id)
    if not content.strip():
        raise DocumentSourceError("正文为空")
    return DocumentSource(
        artifact_id=record.id,
        title=record.title,
        summary=record.summary,
        content=content,
        sections=_split_sections(content, record.title),
    )


def source_context(source: DocumentSource, index: int) -> SourceContext | None:
    """按节号取素材。没有这一节时返回空。摘录最多 400 字。"""
    for section in source.sections:
        if section.index != index:
            continue
        return SourceContext(
            index=section.index,
            title=section.title,
            summary=section.summary,
            excerpt=section.body.strip()[:EXCERPT_CAP],
        )
    return None


def recompose_document(
    source: DocumentSource,
    *,
    new_goal: str,
    deliverable: str,
    gateway: object,
    path: str = "",
    existing_outline: Sequence[OutlineEntry] | None = None,
    root: Path | None = None,
    pending: object = None,
    session: str = "run",
    task_id: str = "task",
    turn_id: str = "turn",
) -> RecomposeResult:
    """用旧节素材跑长文。新 artifact 用新 id，source_artifacts 仍只记旧 id。"""
    from spoolkit.agent.chat_sink import CollectingChatSink
    from spoolkit.agent.document_bodies import InMemoryBodyStore
    from spoolkit.agent.file_sink import FileSink
    from spoolkit.agent.longform import LongFormRunner

    state = build_document_state_from_source(
        source,
        new_goal=new_goal,
        deliverable=deliverable,
        path=path,
        existing_outline=existing_outline,
    )
    contexts = tuple(
        item
        for index in range(len(state.outline))
        if (item := source_context(source, index)) is not None
    )
    bodies = InMemoryBodyStore()
    fresh_id = _fresh_id(source.artifact_id, session, task_id, turn_id, state.path)
    chat: CollectingChatSink | None = None
    file_sink: FileSink | None = None
    if state.deliverable == "file":
        if root is None or pending is None:
            raise DocumentSourceError("file 需要 artifact 根目录和待确认队列")
        file_sink = FileSink(
            state=state,
            path=state.path,
            session=session,
            task_id=task_id,
            turn_id=turn_id,
            artifacts=ArtifactStore(root),
            pending=pending,  # type: ignore[arg-type]
            artifact_id=fresh_id,
        )
        sink = file_sink
    else:
        chat = CollectingChatSink()
        sink = chat
    result = LongFormRunner(state, gateway, bodies, sink, source=contexts).run()  # type: ignore[arg-type]
    if not result.done:
        raise DocumentSourceError("重组没有完成")
    if file_sink is not None:
        if file_sink.record is None or file_sink.pending is None:
            raise DocumentSourceError("重组没有完成")
        return RecomposeResult(
            text=file_sink.pending.new_text,
            state=result.state,
            artifact_id=file_sink.record.id,
            path=file_sink.path,
        )
    text = "" if chat is None else chat.text()
    stored_id = ""
    stored_path = ""
    if root is not None and text:
        record = _store_new_chat(
            state,
            text,
            bodies=bodies,
            root=root,
            session=session,
            task_id=task_id,
            turn_id=turn_id,
            artifact_id=fresh_id,
        )
        stored_id = record.id
        stored_path = record.path
    return RecomposeResult(text=text, state=result.state, artifact_id=stored_id, path=stored_path)


def build_document_state_from_source(
    source: DocumentSource,
    *,
    new_goal: str,
    deliverable: str,
    path: str = "",
    existing_outline: Sequence[OutlineEntry] | None = None,
) -> DocumentState:
    """用旧章节或调用方大纲生成 plan 阶段的状态。不写 artifact。"""
    if deliverable not in ("chat", "file"):
        raise DocumentSourceError(f"deliverable 无效：{deliverable}")
    relative = ""
    if deliverable == "file":
        relative = _safe_path(path)
    outline = tuple(existing_outline) if existing_outline else _outline_from(source)
    return DocumentState(
        goal=new_goal.strip(),
        source_artifacts=(source.artifact_id,),
        outline=outline,
        deliverable=deliverable,  # type: ignore[arg-type]
        path=relative,
        phase="plan",
        current_section=None,
        pending_sections=tuple(range(len(outline))),
    )


def _split_sections(content: str, fallback_title: str) -> tuple[SourceSection, ...]:
    found: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current: list[str] = []
    preamble: list[str] = []
    seen = False
    for line in content.splitlines():
        match = _HEADING.match(line.strip())
        if match is None:
            if seen:
                current.append(line)
            else:
                preamble.append(line)
            continue
        if seen and current_title is not None:
            found.append((current_title, current))
        elif any(item.strip() for item in preamble):
            found.append((fallback_title, preamble))
        seen = True
        current_title = match.group(1).strip()
        current = []
    if seen and current_title is not None:
        found.append((current_title, current))
    elif not found:
        found.append((fallback_title, content.splitlines()))
    return tuple(_section(index, title, lines) for index, (title, lines) in enumerate(found))


def _section(index: int, title: str, lines: list[str]) -> SourceSection:
    body = "\n".join(lines).strip()
    return SourceSection(
        index=index,
        title=title,
        body=body,
        summary=_excerpt(body),
        char_count=len(body),
    )


def _excerpt(text: str) -> str:
    compact = "".join(line.strip() for line in text.splitlines() if line.strip())
    if len(compact) <= SUMMARY_CAP:
        return compact
    return compact[: SUMMARY_CAP - 1] + "…"


def _outline_from(source: DocumentSource) -> tuple[OutlineEntry, ...]:
    return tuple(
        OutlineEntry(
            index=section.index,
            title=section.title,
            points=section.summary or section.title,
            target_chars=max(1, section.char_count),
        )
        for section in source.sections
    )


def _safe_path(path: str) -> str:
    text = str(path).strip()
    if not text:
        raise DocumentSourceError("file 需要文档路径")
    raw = Path(text)
    if raw.is_absolute() or text.startswith(("/", "\\")) or ":" in text:
        raise DocumentSourceError("文档路径不安全")
    parts = [part for part in raw.parts if part != "."]
    if not parts or any(part in ("..", "") for part in parts):
        raise DocumentSourceError("文档路径不安全")
    return "/".join(parts)


def _fresh_id(old: str, session: str, task: str, turn: str, path: str) -> str:
    digest = hashlib.sha256(f"recompose|{old}|{session}|{task}|{turn}|{path}".encode()).hexdigest()[:16]
    new_id = f"doc-{digest}"
    if new_id == old:
        new_id = f"doc-n{digest[:15]}"
    return new_id


def _store_new_chat(
    state: DocumentState,
    text: str,
    *,
    bodies: object,
    root: Path,
    session: str,
    task_id: str,
    turn_id: str,
    artifact_id: str,
) -> ArtifactRecord:
    from spoolkit.agent.document_bodies import section_ref

    sections = []
    for entry in state.outline:
        body = str(bodies.get(section_ref(entry.index))).strip()  # type: ignore[attr-defined]
        sections.append(
            ArtifactSection(
                index=entry.index,
                title=entry.title,
                summary=_excerpt(body) or entry.title,
                char_count=len(body),
            )
        )
    return ArtifactStore(root).put(
        ArtifactRecord(
            id=artifact_id,
            session=session,
            task_id=task_id,
            turn_id=turn_id,
            kind="document",
            title=_excerpt(state.outline[0].title if state.outline else state.goal) or state.goal,
            summary=_excerpt(state.goal) or state.goal,
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            sections=tuple(sections),
        ),
        text,
    )
