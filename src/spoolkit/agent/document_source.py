"""把已有 artifact 收成一份新的 DocumentState。

正文只在 DocumentSource 里用来切节。写进状态的是大纲、摘要和 artifact id。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from spoolkit.agent.artifacts import ArtifactStore
from spoolkit.agent.document_state import SUMMARY_CAP, DocumentState, OutlineEntry

_HEADING = re.compile(r"^##(?!#)\s+(\S.*?)\s*$")


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
