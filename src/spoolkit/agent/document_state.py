"""长文 DocumentState。

运行时持有进度和引用。节正文在 artifact 或文件里，不进这个对象。
`current_section.tail` 最多 200 字，只留衔接。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

SCHEMA_VERSION = 1
TAIL_CAP = 200
SUMMARY_CAP = 240
SECTION_FLOOR = 450
DOC_FLOOR = 3000
NGRAM = 12
CLOSERS = "。！？"
SCAFFOLD_LABELS = ("要点", "目标剩余字数", "本节目标", "提示词", "指令", "大纲")
THINK_TAG = re.compile(r"</?think>", re.IGNORECASE)

Deliverable = Literal["chat", "file", "both"]
Phase = Literal[
    "plan",
    "write",
    "continue",
    "repair",
    "rewrite",
    "commit",
    "finalize",
    "stopped",
    "error",
]
Status = Literal["running", "paused", "done", "failed", "cancelled"]
SectionStatus = Literal["open", "repairing", "rewriting", "closed"]

DELIVERABLES = ("chat", "file", "both")
PHASES = (
    "plan",
    "write",
    "continue",
    "repair",
    "rewrite",
    "commit",
    "finalize",
    "stopped",
    "error",
)
STATUSES = ("running", "paused", "done", "failed", "cancelled")
SECTION_STATUSES = ("open", "repairing", "rewriting", "closed")

_OUTLINE_KEYS = frozenset({"index", "title", "points", "target_chars"})
_COMPLETED_KEYS = frozenset(
    {"index", "title", "summary", "artifact_id", "path", "char_count"}
)
_CURRENT_KEYS = frozenset(
    {"index", "title", "summary", "tail", "char_count", "status"}
)
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "goal",
        "source_artifacts",
        "deliverable",
        "path",
        "outline",
        "completed_sections",
        "current_section",
        "pending_sections",
        "phase",
        "status",
        "error",
        "length_floor",
        "repair_attempts",
        "rewrite_attempts",
    }
)


class DocumentStateError(ValueError):
    """字段、引用或正文边界不成立。"""


class SchemaVersionError(DocumentStateError):
    """checkpoint 的 schema_version 与当前代码不一致。"""


def _reject_unknown(payload: Mapping[str, object], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DocumentStateError(f"{label} 含有不允许的字段：{', '.join(unknown)}")


def _nonempty(value: object, label: str) -> str:
    text = str(value).strip()
    if not text:
        raise DocumentStateError(f"{label} 为空")
    return text


def _bounded(value: object, limit: int, label: str) -> str:
    text = str(value)
    if len(text) > limit:
        raise DocumentStateError(f"{label} 超过 {limit} 字")
    return text


def _count(value: object, label: str) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise DocumentStateError(f"{label} 不是整数") from exc
    if number < 0:
        raise DocumentStateError(f"{label} 为负")
    return number


@dataclass(frozen=True, slots=True)
class OutlineEntry:
    index: int
    title: str
    points: str
    target_chars: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise DocumentStateError("大纲下标为负")
        _nonempty(self.title, "节标题")
        _nonempty(self.points, "要点")
        if self.target_chars <= 0:
            raise DocumentStateError("目标字数必须大于 0")

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "title": self.title,
            "points": self.points,
            "target_chars": self.target_chars,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> OutlineEntry:
        _reject_unknown(payload, _OUTLINE_KEYS, "大纲项")
        return cls(
            index=_count(payload["index"], "大纲下标"),
            title=_nonempty(payload["title"], "节标题"),
            points=_nonempty(payload["points"], "要点"),
            target_chars=_count(payload["target_chars"], "目标字数"),
        )


@dataclass(frozen=True, slots=True)
class CompletedSection:
    index: int
    title: str
    summary: str
    artifact_id: str
    path: str
    char_count: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise DocumentStateError("已完成节下标为负")
        _nonempty(self.title, "节标题")
        _bounded(self.summary, SUMMARY_CAP, "摘要")
        _nonempty(self.artifact_id, "artifact_id")
        if self.char_count < 0:
            raise DocumentStateError("字数为负")

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "title": self.title,
            "summary": self.summary,
            "artifact_id": self.artifact_id,
            "path": self.path,
            "char_count": self.char_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CompletedSection:
        _reject_unknown(payload, _COMPLETED_KEYS, "已完成节")
        return cls(
            index=_count(payload["index"], "已完成节下标"),
            title=_nonempty(payload["title"], "节标题"),
            summary=_bounded(str(payload.get("summary", "")), SUMMARY_CAP, "摘要"),
            artifact_id=_nonempty(payload["artifact_id"], "artifact_id"),
            path=str(payload.get("path", "")),
            char_count=_count(payload["char_count"], "字数"),
        )


@dataclass(frozen=True, slots=True)
class CurrentSection:
    index: int
    title: str
    summary: str
    tail: str
    char_count: int
    status: SectionStatus

    def __post_init__(self) -> None:
        if self.index < 0:
            raise DocumentStateError("当前节下标为负")
        _nonempty(self.title, "节标题")
        _bounded(self.summary, SUMMARY_CAP, "摘要")
        _bounded(self.tail, TAIL_CAP, "当前节尾部")
        if self.char_count < len(self.tail):
            raise DocumentStateError("字数小于尾部长度")
        if self.status not in SECTION_STATUSES:
            raise DocumentStateError(f"当前节状态无效：{self.status}")

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "title": self.title,
            "summary": self.summary,
            "tail": self.tail,
            "char_count": self.char_count,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CurrentSection:
        _reject_unknown(payload, _CURRENT_KEYS, "当前节")
        status = str(payload["status"])
        if status not in SECTION_STATUSES:
            raise DocumentStateError(f"当前节状态无效：{status}")
        return cls(
            index=_count(payload["index"], "当前节下标"),
            title=_nonempty(payload["title"], "节标题"),
            summary=_bounded(str(payload.get("summary", "")), SUMMARY_CAP, "摘要"),
            tail=_bounded(str(payload.get("tail", "")), TAIL_CAP, "当前节尾部"),
            char_count=_count(payload["char_count"], "字数"),
            status=status,  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class DocumentState:
    goal: str
    outline: tuple[OutlineEntry, ...]
    source_artifacts: tuple[str, ...] = ()
    deliverable: Deliverable = "chat"
    path: str = ""
    completed_sections: tuple[CompletedSection, ...] = ()
    current_section: CurrentSection | None = None
    pending_sections: tuple[int, ...] = ()
    phase: Phase = "plan"
    status: Status = "running"
    error: str = ""
    length_floor: int = SECTION_FLOOR
    repair_attempts: tuple[int, ...] = ()
    rewrite_attempts: tuple[int, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"schema_version {self.schema_version} 与 {SCHEMA_VERSION} 不一致"
            )
        _nonempty(self.goal, "goal")
        if self.deliverable not in DELIVERABLES:
            raise DocumentStateError(f"deliverable 无效：{self.deliverable}")
        if self.phase not in PHASES:
            raise DocumentStateError(f"phase 无效：{self.phase}")
        if self.status not in STATUSES:
            raise DocumentStateError(f"status 无效：{self.status}")
        if self.length_floor <= 0:
            raise DocumentStateError("length_floor 必须大于 0")
        if len(set(self.source_artifacts)) != len(self.source_artifacts):
            raise DocumentStateError("source_artifacts 有重复 id")
        indexes = tuple(item.index for item in self.outline)
        if indexes != tuple(range(len(self.outline))):
            raise DocumentStateError("大纲下标必须从 0 连续编号")
        object.__setattr__(self, "repair_attempts", _align_attempts(self.repair_attempts, len(self.outline), "repair_attempts"))
        object.__setattr__(self, "rewrite_attempts", _align_attempts(self.rewrite_attempts, len(self.outline), "rewrite_attempts"))
        completed = tuple(item.index for item in self.completed_sections)
        if len(set(completed)) != len(completed):
            raise DocumentStateError("已完成节下标重复")
        known = set(indexes)
        if not set(completed) <= known:
            raise DocumentStateError("已完成节不在大纲里")
        if not set(self.pending_sections) <= known:
            raise DocumentStateError("未开始节不在大纲里")
        if len(set(self.pending_sections)) != len(self.pending_sections):
            raise DocumentStateError("未开始节下标重复")
        if set(self.pending_sections) & set(completed):
            raise DocumentStateError("未开始节与已完成节重叠")
        if self.current_section is not None:
            if self.current_section.index not in known:
                raise DocumentStateError("当前节不在大纲里")
            if self.current_section.index in self.pending_sections:
                raise DocumentStateError("当前节不应同时列在未开始节里")
            if self.current_section.index in completed:
                raise DocumentStateError("当前节不应同时列在已完成节里")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "source_artifacts": list(self.source_artifacts),
            "deliverable": self.deliverable,
            "path": self.path,
            "outline": [item.to_dict() for item in self.outline],
            "completed_sections": [item.to_dict() for item in self.completed_sections],
            "current_section": None if self.current_section is None else self.current_section.to_dict(),
            "pending_sections": list(self.pending_sections),
            "phase": self.phase,
            "status": self.status,
            "error": self.error,
            "length_floor": self.length_floor,
            "repair_attempts": list(self.repair_attempts),
            "rewrite_attempts": list(self.rewrite_attempts),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DocumentState:
        if "schema_version" not in payload:
            raise SchemaVersionError("缺少 schema_version")
        _reject_unknown(payload, _STATE_KEYS, "DocumentState")
        version = _count(payload["schema_version"], "schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(f"schema_version {version} 与 {SCHEMA_VERSION} 不一致")
        outline_raw = payload["outline"]
        if not isinstance(outline_raw, list):
            raise DocumentStateError("outline 必须是列表")
        current_raw = payload.get("current_section")
        current = None
        if current_raw is not None:
            if not isinstance(current_raw, Mapping):
                raise DocumentStateError("current_section 必须是对象")
            current = CurrentSection.from_dict(current_raw)
        completed_raw = payload.get("completed_sections", [])
        if not isinstance(completed_raw, list):
            raise DocumentStateError("completed_sections 必须是列表")
        outline: list[OutlineEntry] = []
        for item in outline_raw:
            if not isinstance(item, Mapping):
                raise DocumentStateError("大纲项必须是对象")
            outline.append(OutlineEntry.from_dict(item))
        completed: list[CompletedSection] = []
        for item in completed_raw:
            if not isinstance(item, Mapping):
                raise DocumentStateError("已完成节必须是对象")
            completed.append(CompletedSection.from_dict(item))
        return cls(
            schema_version=version,
            goal=_nonempty(payload["goal"], "goal"),
            source_artifacts=tuple(str(item) for item in payload.get("source_artifacts", ())),
            deliverable=str(payload.get("deliverable", "chat")),  # type: ignore[arg-type]
            path=str(payload.get("path", "")),
            outline=tuple(outline),
            completed_sections=tuple(completed),
            current_section=current,
            pending_sections=tuple(int(item) for item in payload.get("pending_sections", ())),
            phase=str(payload.get("phase", "plan")),  # type: ignore[arg-type]
            status=str(payload.get("status", "running")),  # type: ignore[arg-type]
            error=str(payload.get("error", "")),
            length_floor=_count(payload.get("length_floor", SECTION_FLOOR), "length_floor"),
            repair_attempts=tuple(int(item) for item in payload.get("repair_attempts", ())),
            rewrite_attempts=tuple(int(item) for item in payload.get("rewrite_attempts", ())),
        )

    @classmethod
    def from_json(cls, text: str) -> DocumentState:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise DocumentStateError("状态 JSON 必须是对象")
        return cls.from_dict(payload)

    def check(self, bodies: Sequence[str], *, anchor_lost_unresolved: bool = False) -> tuple[str, ...]:
        return validate_completion(
            bodies,
            length_floor=self.length_floor,
            anchor_lost_unresolved=anchor_lost_unresolved,
        )


def _align_attempts(values: tuple[int, ...], size: int, label: str) -> tuple[int, ...]:
    if not values:
        return tuple(0 for _ in range(size))
    if len(values) != size:
        raise DocumentStateError(f"{label} 长度与大纲不一致")
    if any(item < 0 for item in values):
        raise DocumentStateError(f"{label} 含有负数")
    return values


def scaffold_hits(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        for label in SCAFFOLD_LABELS:
            if stripped == label or stripped.startswith(label + "：") or stripped.startswith(label + ":"):
                if label not in found:
                    found.append(label)
    return tuple(found)


def has_think(text: str) -> bool:
    return THINK_TAG.search(text) is not None


def sentence_closed(body: str) -> bool:
    lines = [line.strip() for line in body.splitlines() if line.strip() and line.strip() != "完"]
    return bool(lines) and lines[-1][-1] in CLOSERS


def _ngrams(text: str) -> list[str]:
    if len(text) < NGRAM:
        return []
    return [text[index : index + NGRAM] for index in range(len(text) - NGRAM + 1)]


def overlap_ratio(left: str, right: str) -> float:
    right_grams = _ngrams(right)
    if not right_grams:
        return 0.0
    left_grams = set(_ngrams(left))
    return sum(1 for gram in right_grams if gram in left_grams) / len(right_grams)


def last_sentence(body: str) -> str:
    compact = "".join(
        line.strip() for line in body.splitlines() if line.strip() and line.strip() != "完"
    )
    parts: list[str] = []
    buf = ""
    for char in compact:
        buf += char
        if char in CLOSERS:
            parts.append(buf.strip())
            buf = ""
    return parts[-1] if parts else ""


def repeats_existing(existing: str, addition: str) -> bool:
    """补写是否在用已有正文凑字数。重叠达到 0.5，或同一结尾句再次出现。"""
    addition = addition.strip()
    if not addition:
        return True
    ending = last_sentence(existing)
    repeated = bool(ending) and len(ending) >= 8 and ending in addition
    return overlap_ratio(existing, addition) >= 0.5 or repeated


def section_repeats(body: str) -> bool:
    ending = last_sentence(body)
    if len(ending) < 8:
        return False
    return body.count(ending) >= 2


def validate_completion(
    bodies: Sequence[str],
    *,
    length_floor: int = SECTION_FLOOR,
    doc_floor: int = DOC_FLOOR,
    anchor_lost_unresolved: bool = False,
) -> tuple[str, ...]:
    """返回失败原因。空元组表示这批正文通过完成线。正文不写入 state。"""
    reasons: list[str] = []
    if any(len(body.strip()) < length_floor for body in bodies):
        reasons.append("short_section")
    if sum(len(body.strip()) for body in bodies) < doc_floor:
        reasons.append("short_doc")
    if any(not sentence_closed(body) for body in bodies):
        reasons.append("open_section")
    if any(scaffold_hits(body) for body in bodies):
        reasons.append("scaffold")
    if any(has_think(body) for body in bodies):
        reasons.append("think")
    if any(section_repeats(body) for body in bodies):
        reasons.append("repeated")
    if anchor_lost_unresolved:
        reasons.append("anchor_lost")
    return tuple(reasons)
