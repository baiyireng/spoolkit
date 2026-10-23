"""按预算拼一份会话上下文。

只用任务摘要、会话摘要和 artifact 引用。不读正文，不调用模型。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from spoolkit.agent.artifacts import ArtifactHit
from spoolkit.agent.session_summary import SessionSummary
from spoolkit.llm.tokenizer import OfflineTokenCounter, TokenCounter
from spoolkit.memory.transcript import redact_secrets

Kind = Literal["task", "open", "decision", "artifact", "done", "meta"]

_ORDER: tuple[Kind, ...] = ("task", "open", "decision", "artifact", "done", "meta")
_TITLES: dict[Kind, str] = {
    "task": "当前任务",
    "open": "未完成",
    "decision": "决定",
    "artifact": "产物",
    "done": "已完成",
    "meta": "最近一轮",
}


@dataclass(frozen=True, slots=True)
class ContextSection:
    kind: Kind
    title: str
    lines: tuple[str, ...]
    tokens: int


@dataclass(frozen=True, slots=True)
class ContextPack:
    goal: str
    sections: tuple[ContextSection, ...]
    omitted: tuple[str, ...]
    total_tokens: int

    def text(self) -> str:
        if not self.goal and not self.sections:
            return ""
        chunks = [f"# {self.goal}"]
        for section in self.sections:
            body = "\n".join(f"- {line}" for line in section.lines)
            chunks.append(f"## {section.title}\n{body}")
        return "\n\n".join(chunks)


def build_context_pack(
    *,
    goal: str,
    task_summary: str,
    summary: SessionSummary,
    artifacts: Sequence[ArtifactHit],
    token_budget: int,
    counter: TokenCounter | None = None,
) -> ContextPack:
    """按 task、未完成、决定、产物、已完成、最近一轮装入预算。装不下的整节跳过。"""
    cleaned_goal = redact_secrets(str(goal).strip())
    if not cleaned_goal or _summary_blank(summary):
        return ContextPack(goal="", sections=(), omitted=(), total_tokens=0)
    counter = counter or OfflineTokenCounter()
    remaining = token_budget if token_budget > 0 else 0
    seen: set[str] = set()
    included: list[ContextSection] = []
    omitted: list[str] = []
    raw = _raw_sections(task_summary, summary, artifacts)
    for kind in _ORDER:
        lines = _fresh(raw[kind], seen)
        if not lines:
            continue
        rendered = _render(_TITLES[kind], lines)
        cost = counter.count(rendered)
        if cost <= remaining:
            seen.update(lines)
            included.append(
                ContextSection(kind=kind, title=_TITLES[kind], lines=lines, tokens=cost)
            )
            remaining -= cost
        else:
            omitted.append(kind)
    return ContextPack(
        goal=cleaned_goal,
        sections=tuple(included),
        omitted=tuple(omitted),
        total_tokens=sum(section.tokens for section in included),
    )


def _summary_blank(summary: SessionSummary) -> bool:
    return not any(
        (
            summary.headline,
            summary.what_done,
            summary.artifacts,
            summary.decisions,
            summary.open_items,
            summary.last_turn_id,
        )
    )


def _raw_sections(
    task_summary: str,
    summary: SessionSummary,
    artifacts: Sequence[ArtifactHit],
) -> dict[Kind, tuple[str, ...]]:
    return {
        "task": _task_lines(task_summary, summary),
        "open": summary.open_items,
        "decision": summary.decisions,
        "artifact": _artifact_lines(summary, artifacts),
        "done": summary.what_done,
        "meta": (summary.last_turn_id,) if summary.last_turn_id else (),
    }


def _task_lines(task_summary: str, summary: SessionSummary) -> tuple[str, ...]:
    lines: list[str] = []
    task = str(task_summary).strip()
    if task:
        lines.append(task)
    if summary.headline and summary.headline not in lines:
        lines.append(summary.headline)
    return tuple(lines)


def _artifact_lines(summary: SessionSummary, artifacts: Sequence[ArtifactHit]) -> tuple[str, ...]:
    lines: list[str] = []
    seen_ids: set[str] = set()
    ordered: list[tuple[str, str, str, str]] = []
    for hit in artifacts:
        record = hit.record
        if record.id in seen_ids:
            continue
        seen_ids.add(record.id)
        ordered.append((record.title, record.summary, record.path, record.id))
    for item in summary.artifacts:
        if item.id in seen_ids:
            continue
        seen_ids.add(item.id)
        ordered.append((item.title, item.summary, item.path, item.id))
    for title, blurb, path, artifact_id in ordered:
        for label, value in (
            ("title", title),
            ("summary", blurb),
            ("path", path),
            ("id", artifact_id),
        ):
            text = str(value).strip()
            if text:
                lines.append(f"{label}: {text}")
    return tuple(lines)


def _fresh(lines: tuple[str, ...], seen: set[str]) -> tuple[str, ...]:
    kept: list[str] = []
    for line in lines:
        text = redact_secrets(str(line).strip())
        if not text or text in seen or text in kept:
            continue
        kept.append(text)
    return tuple(kept)


def _render(title: str, lines: tuple[str, ...]) -> str:
    body = "\n".join(f"- {line}" for line in lines)
    return f"## {title}\n{body}"
