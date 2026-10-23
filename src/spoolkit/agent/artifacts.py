"""长文 artifact 仓库。

正文写在 `<root>/.agent/artifacts/<session>/<content_hash>`。
同目录的 `<id>.json` 只放元数据。DocumentState 和 TaskEvent 不进正文。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from spoolkit.agent.document_state import SUMMARY_CAP
from spoolkit.memory.transcript import redact_secrets
from spoolkit.tools.types import Artifact

KINDS = ("document", "report", "code", "output")
_LAYOUT = ".agent/artifacts"


class ArtifactError(ValueError):
    """记录、路径或写入不成立。"""


class ArtifactNotFound(ArtifactError):
    """id 没有登记，或正文文件已经不在。"""


def _nonempty(value: object, label: str) -> str:
    text = str(value).strip()
    if not text:
        raise ArtifactError(f"{label} 为空")
    return text


def _bounded(value: object, label: str) -> str:
    text = str(value)
    if len(text) > SUMMARY_CAP:
        raise ArtifactError(f"{label} 超过 {SUMMARY_CAP} 字")
    return text


def _segment(value: str) -> str:
    """目录名只允许一层安全片段。斜杠和 .. 都算越界。"""
    if not value or value in (".", "..") or value != Path(value).name:
        raise ArtifactError("路径越界")
    if any(char in value for char in ("/", "\\", ":")):
        raise ArtifactError("路径越界")
    return value


def _reject_relative(path: str) -> None:
    if not path:
        return
    raw = Path(path)
    if raw.is_absolute() or ".." in raw.parts or ".." in path.replace("\\", "/").split("/"):
        raise ArtifactError("路径越界")


@dataclass(frozen=True, slots=True)
class ArtifactSection:
    index: int
    title: str
    summary: str
    char_count: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ArtifactError("节下标为负")
        _nonempty(self.title, "节标题")
        _bounded(self.summary, "节摘要")
        if self.char_count < 0:
            raise ArtifactError("字数为负")

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "title": self.title,
            "summary": self.summary,
            "char_count": self.char_count,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ArtifactSection:
        if not isinstance(payload, dict):
            raise ArtifactError("章节必须是对象")
        return cls(
            index=int(payload["index"]),
            title=str(payload["title"]),
            summary=str(payload["summary"]),
            char_count=int(payload["char_count"]),
        )


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    session: str
    task_id: str
    turn_id: str
    kind: str
    title: str
    summary: str
    created_at: str
    sections: tuple[ArtifactSection, ...] = ()
    path: str = ""
    content_hash: str = ""
    size: int = 0

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ArtifactError(f"kind 无效：{self.kind}")
        _nonempty(self.title, "标题")
        _bounded(self.summary, "摘要")
        _nonempty(self.created_at, "created_at")
        object.__setattr__(self, "sections", tuple(self.sections))
        indexes = [item.index for item in self.sections]
        if len(indexes) != len(set(indexes)):
            raise ArtifactError("章节下标重复")
        if self.size < 0:
            raise ArtifactError("size 为负")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "session": self.session,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "path": self.path,
            "content_hash": self.content_hash,
            "size": self.size,
            "created_at": self.created_at,
            "sections": [item.to_dict() for item in self.sections],
        }

    @classmethod
    def from_dict(cls, payload: object) -> ArtifactRecord:
        if not isinstance(payload, dict):
            raise ArtifactError("artifact 记录必须是对象")
        raw_sections = payload.get("sections", ())
        if not isinstance(raw_sections, list):
            raise ArtifactError("章节必须是列表")
        return cls(
            id=str(payload["id"]),
            session=str(payload["session"]),
            task_id=str(payload["task_id"]),
            turn_id=str(payload["turn_id"]),
            kind=str(payload["kind"]),
            title=str(payload["title"]),
            summary=str(payload["summary"]),
            created_at=str(payload["created_at"]),
            sections=tuple(ArtifactSection.from_dict(item) for item in raw_sections),
            path=str(payload.get("path", "")),
            content_hash=str(payload.get("content_hash", "")),
            size=int(payload.get("size", 0)),
        )


@dataclass(frozen=True, slots=True)
class ArtifactHit:
    """一次元数据命中。不含正文。"""

    record: ArtifactRecord
    score: int
    matched: tuple[str, ...]
    snippet: str


_MATCH_FIELDS = ("title", "summary", "section.title", "section.summary", "path", "id")
_SCORE = {
    "title": (0, 400, 500, 600),
    "summary": (0, 100, 200, 300),
    "section.title": (0, 40, 50, 60),
    "section.summary": (0, 40, 50, 60),
    "path": (0, 20, 20, 20),
    "id": (0, 20, 20, 20),
}


def _quality(text: str, needle: str) -> int:
    """0 未命中，1 包含，2 前缀，3 整段相等。比较前已 casefold。"""
    if needle not in text:
        return 0
    if text == needle:
        return 3
    if text.startswith(needle):
        return 2
    return 1


def _best_section(sections: tuple[ArtifactSection, ...], needle: str, attr: str) -> tuple[int, str]:
    quality = 0
    source = ""
    for section in sections:
        text = getattr(section, attr)
        rank = _quality(text.casefold(), needle)
        if rank > quality:
            quality = rank
            source = text
    return quality, source


def _match(record: ArtifactRecord, needle: str) -> ArtifactHit | None:
    folded = needle.casefold()
    sources = {
        "title": record.title,
        "summary": record.summary,
        "path": record.path,
        "id": record.id,
    }
    ranks = {name: _quality(text.casefold(), folded) for name, text in sources.items()}
    section_title, section_title_text = _best_section(record.sections, folded, "title")
    section_summary, section_summary_text = _best_section(record.sections, folded, "summary")
    ranks["section.title"] = section_title
    ranks["section.summary"] = section_summary
    sources["section.title"] = section_title_text
    sources["section.summary"] = section_summary_text
    matched = tuple(name for name in _MATCH_FIELDS if ranks[name])
    if not matched:
        return None
    best = max(matched, key=lambda name: _SCORE[name][ranks[name]])
    snippet = redact_secrets(sources[best])[:SUMMARY_CAP]
    return ArtifactHit(
        record=_redacted(record),
        score=_SCORE[best][ranks[best]],
        matched=matched,
        snippet=snippet,
    )


def _redacted(record: ArtifactRecord) -> ArtifactRecord:
    """返回给调用方的标题和摘要先脱敏。磁盘上的记录不动。"""
    return ArtifactRecord(
        id=record.id,
        session=record.session,
        task_id=record.task_id,
        turn_id=record.turn_id,
        kind=record.kind,
        title=redact_secrets(record.title),
        summary=redact_secrets(record.summary),
        created_at=record.created_at,
        sections=tuple(
            ArtifactSection(
                index=section.index,
                title=redact_secrets(section.title),
                summary=redact_secrets(section.summary),
                char_count=section.char_count,
            )
            for section in record.sections
        ),
        path=record.path,
        content_hash=record.content_hash,
        size=record.size,
    )


def as_task_artifact(record: ArtifactRecord) -> Artifact:
    """事件里只放 kind、路径和 id。"""
    return Artifact(type=record.kind, path=record.path, ref=record.id)


class ArtifactStore:
    """按 session 落盘。id 在一个 session 内唯一，仓库不生成随机 id。

    相同 hash 只在同一 session 内复用记录。另一个 session 的相同正文另写一份，
    不返回别的 session 的记录。
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def put(self, record: ArtifactRecord, content: str) -> ArtifactRecord:
        if not isinstance(content, str):
            raise ArtifactError("正文必须是字符串")
        _segment(record.id)
        _segment(record.session)
        _reject_relative(record.path)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        size = len(content.encode("utf-8"))
        canonical = f"{_LAYOUT}/{record.session}/{digest}"
        if record.path and record.path != canonical:
            raise ArtifactError("路径越界")
        if record.content_hash and record.content_hash != digest:
            raise ArtifactError("content_hash 与正文不一致")
        filled = ArtifactRecord(
            id=record.id,
            session=record.session,
            task_id=record.task_id,
            turn_id=record.turn_id,
            kind=record.kind,
            title=record.title,
            summary=record.summary,
            created_at=record.created_at,
            sections=record.sections,
            path=canonical,
            content_hash=digest,
            size=size,
        )
        with self._lock:
            current = self._read_id(record.session, record.id)
            if current is not None:
                if current.content_hash == digest:
                    return current
                raise ArtifactError("同一 session 内该 id 已有不同正文")
            by_hash = self._read_hash(record.session, digest)
            if by_hash is not None:
                return by_hash
            content_path = self._inside(canonical)
            meta_path = self._meta_path(record.session, record.id)
            self._atomic_write(content_path, content.encode("utf-8"))
            try:
                self._atomic_write(
                    meta_path,
                    json.dumps(filled.to_dict(), ensure_ascii=False).encode("utf-8"),
                )
            except Exception:
                with contextlib.suppress(OSError):
                    content_path.unlink()
                with contextlib.suppress(OSError):
                    meta_path.unlink()
                raise
        return filled

    def get(self, session: str, artifact_id: str) -> tuple[ArtifactRecord, str]:
        _segment(session)
        _segment(artifact_id)
        record = self._read_id(session, artifact_id)
        if record is None:
            raise ArtifactNotFound(f"找不到 artifact：{session}/{artifact_id}")
        path = self._inside(record.path)
        if not path.is_file():
            raise ArtifactNotFound(f"正文文件缺失：{session}/{artifact_id}")
        return record, path.read_text(encoding="utf-8")

    def exists(self, session: str, artifact_id: str) -> bool:
        try:
            _segment(session)
            _segment(artifact_id)
        except ArtifactError:
            return False
        return self._read_id(session, artifact_id) is not None

    def list(self, session: str, limit: int) -> tuple[ArtifactRecord, ...]:
        _segment(session)
        if limit < 0:
            raise ArtifactError("limit 为负")
        directory = self._root / ".agent" / "artifacts" / session
        if not directory.is_dir():
            return ()
        records = [self._load_meta(path) for path in directory.glob("*.json")]
        records.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return tuple(records[:limit])

    def search(self, session: str, query: str, limit: int = 10) -> tuple[ArtifactHit, ...]:
        """只查当前 session 的元数据。不打开正文文件。"""
        _segment(session)
        if limit < 0:
            raise ArtifactError("limit 为负")
        needle = query.strip()
        if not needle or limit == 0:
            return ()
        directory = self._root / ".agent" / "artifacts" / session
        if not directory.is_dir():
            return ()
        hits: list[ArtifactHit] = []
        for path in directory.glob("*.json"):
            record = self._load_meta(path)
            if record.session != session:
                raise ArtifactError("记录与 session 不一致")
            hit = _match(record, needle)
            if hit is not None:
                hits.append(hit)
        hits.sort(key=lambda item: (item.score, item.record.created_at, item.record.id), reverse=True)
        return tuple(hits[:limit])

    def _meta_path(self, session: str, artifact_id: str) -> Path:
        return self._root / ".agent" / "artifacts" / session / f"{artifact_id}.json"

    def _inside(self, relative: str) -> Path:
        _reject_relative(relative)
        parts = Path(relative).parts
        if not parts or parts[0] != ".agent":
            raise ArtifactError("路径越界")
        target = self._root.joinpath(*parts)
        root = self._root.resolve()
        resolved = target.resolve()
        if not resolved.is_relative_to(root):
            raise ArtifactError("路径越界")
        return target

    def _load_meta(self, path: Path) -> ArtifactRecord:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ArtifactRecord.from_dict(payload)

    def _read_id(self, session: str, artifact_id: str) -> ArtifactRecord | None:
        path = self._meta_path(session, artifact_id)
        if not path.is_file():
            return None
        record = self._load_meta(path)
        if record.session != session or record.id != artifact_id:
            raise ArtifactError("记录与 session 不一致")
        if record.path and not record.path.startswith(f"{_LAYOUT}/{session}/"):
            raise ArtifactError("记录与 session 不一致")
        return record

    def _read_hash(self, session: str, digest: str) -> ArtifactRecord | None:
        directory = self._root / ".agent" / "artifacts" / session
        if not directory.is_dir():
            return None
        for path in directory.glob("*.json"):
            record = self._load_meta(path)
            if record.session != session:
                raise ArtifactError("记录与 session 不一致")
            if record.content_hash == digest:
                return record
        return None

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        except OSError as exc:
            raise ArtifactError(f"写不了 {path.name}") from exc
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise ArtifactError(f"写不了 {path.name}") from exc
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
