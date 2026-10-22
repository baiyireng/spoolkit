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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from spoolkit.agent.document_state import SUMMARY_CAP
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


def as_task_artifact(record: ArtifactRecord) -> Artifact:
    """事件里只放 kind、路径和 id。"""
    return Artifact(type=record.kind, path=record.path, ref=record.id)


class ArtifactStore:
    """按 session 落盘。id 由调用方给出，仓库不生成随机 id。"""

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
            current = self._read_id(record.id)
            if current is not None:
                if current.content_hash == digest:
                    return current
                raise ArtifactError("同一 id 已有不同正文")
            by_hash = self._read_hash(digest)
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

    def get(self, artifact_id: str) -> tuple[ArtifactRecord, str]:
        _segment(artifact_id)
        record = self._read_id(artifact_id)
        if record is None:
            raise ArtifactNotFound(f"找不到 artifact：{artifact_id}")
        path = self._inside(record.path)
        if not path.is_file():
            raise ArtifactNotFound(f"正文文件缺失：{artifact_id}")
        return record, path.read_text(encoding="utf-8")

    def exists(self, artifact_id: str) -> bool:
        try:
            _segment(artifact_id)
        except ArtifactError:
            return False
        return self._read_id(artifact_id) is not None

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

    def _iter_metas(self) -> Iterator[Path]:
        base = self._root / ".agent" / "artifacts"
        if not base.is_dir():
            return
        for session_dir in base.iterdir():
            if session_dir.is_dir():
                yield from session_dir.glob("*.json")

    def _load_meta(self, path: Path) -> ArtifactRecord:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ArtifactRecord.from_dict(payload)

    def _read_id(self, artifact_id: str) -> ArtifactRecord | None:
        found: ArtifactRecord | None = None
        for path in self._iter_metas():
            if path.stem != artifact_id:
                continue
            record = self._load_meta(path)
            if found is not None:
                raise ArtifactError(f"artifact id 重复：{artifact_id}")
            found = record
        return found

    def _read_hash(self, digest: str) -> ArtifactRecord | None:
        for path in self._iter_metas():
            record = self._load_meta(path)
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
