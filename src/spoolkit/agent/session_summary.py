"""会话摘要。

写在 `<root>/.agent/sessions/<session>/summary.json`。
只记进度、决定、未完成事项和 artifact 引用。不进正文，不调用模型。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from spoolkit.agent.artifacts import ArtifactRecord
from spoolkit.agent.document_state import SUMMARY_CAP
from spoolkit.memory.transcript import redact_secrets
from spoolkit.session_state import dir_for, safe

SCHEMA_VERSION = 1
TEXT_CAP = SUMMARY_CAP
LIST_CAP = 20
_FILE = "summary.json"

logger = logging.getLogger(__name__)


class SessionSummaryError(ValueError):
    """摘要、路径或写入不成立。"""


def _checked_session(session: str) -> str:
    """目录名必须已经是一层安全片段。改写或跳出 sessions 都拒绝。"""
    raw = str(session).strip()
    if not raw or raw in {".", ".."} or raw != Path(raw).name:
        raise SessionSummaryError("路径越界")
    if any(char in raw for char in ("/", "\\", ":")) or ".." in raw.replace("\\", "/").split("/"):
        raise SessionSummaryError("路径越界")
    if safe(raw) != raw:
        raise SessionSummaryError("路径越界")
    return raw


def _plain(value: object, label: str, *, required: bool) -> str:
    text = redact_secrets(str(value).strip())
    if required and not text:
        raise SessionSummaryError(f"{label} 为空")
    if len(text) > TEXT_CAP:
        raise SessionSummaryError(f"{label} 超过 {TEXT_CAP} 字")
    return text


def _entries(items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise SessionSummaryError("条目必须是字符串")
        text = _plain(item, "条目", required=False)
        if text and text not in seen:
            seen.append(text)
    return tuple(seen[-LIST_CAP:])


@dataclass(frozen=True, slots=True)
class SummaryArtifact:
    """摘要里的一份产物。只有引用和摘要，没有正文。"""

    id: str
    title: str
    summary: str
    path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _plain(self.id, "artifact id", required=True))
        object.__setattr__(self, "title", _plain(self.title, "标题", required=True))
        object.__setattr__(self, "summary", _plain(self.summary, "摘要", required=False))
        object.__setattr__(self, "path", _plain(self.path, "路径", required=False))

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, payload: object) -> SummaryArtifact:
        if not isinstance(payload, dict):
            raise SessionSummaryError("artifact 必须是对象")
        return cls(
            id=str(payload["id"]),
            title=str(payload["title"]),
            summary=str(payload.get("summary", "")),
            path=str(payload.get("path", "")),
        )


def _artifacts(items: tuple[SummaryArtifact, ...]) -> tuple[SummaryArtifact, ...]:
    order: list[str] = []
    latest: dict[str, SummaryArtifact] = {}
    for item in items:
        if not isinstance(item, SummaryArtifact):
            raise SessionSummaryError("artifact 必须是 SummaryArtifact")
        if item.id not in latest:
            order.append(item.id)
        latest[item.id] = item
    return tuple(latest[key] for key in order[-LIST_CAP:])


@dataclass(frozen=True, slots=True)
class SessionSummary:
    schema_version: int
    session: str
    updated_at: str
    headline: str = ""
    what_done: tuple[str, ...] = ()
    artifacts: tuple[SummaryArtifact, ...] = ()
    decisions: tuple[str, ...] = ()
    open_items: tuple[str, ...] = ()
    last_turn_id: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SessionSummaryError(f"schema_version 不匹配：{self.schema_version}")
        object.__setattr__(self, "session", _checked_session(self.session))
        object.__setattr__(self, "updated_at", _plain(self.updated_at, "updated_at", required=False))
        object.__setattr__(self, "headline", _plain(self.headline, "headline", required=False))
        object.__setattr__(self, "what_done", _entries(tuple(self.what_done)))
        object.__setattr__(self, "artifacts", _artifacts(tuple(self.artifacts)))
        object.__setattr__(self, "decisions", _entries(tuple(self.decisions)))
        object.__setattr__(self, "open_items", _entries(tuple(self.open_items)))
        object.__setattr__(self, "last_turn_id", _plain(self.last_turn_id, "last_turn_id", required=False))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session": self.session,
            "updated_at": self.updated_at,
            "headline": self.headline,
            "what_done": list(self.what_done),
            "artifacts": [item.to_dict() for item in self.artifacts],
            "decisions": list(self.decisions),
            "open_items": list(self.open_items),
            "last_turn_id": self.last_turn_id,
        }

    @classmethod
    def from_dict(cls, payload: object) -> SessionSummary:
        if not isinstance(payload, dict):
            raise SessionSummaryError("summary.json 不是对象")
        raw_artifacts = payload.get("artifacts", [])
        if not isinstance(raw_artifacts, list):
            raise SessionSummaryError("artifacts 必须是列表")
        return cls(
            schema_version=int(payload["schema_version"]),
            session=str(payload["session"]),
            updated_at=str(payload.get("updated_at", "")),
            headline=str(payload.get("headline", "")),
            what_done=_string_tuple(payload.get("what_done", []), "what_done"),
            artifacts=tuple(SummaryArtifact.from_dict(item) for item in raw_artifacts),
            decisions=_string_tuple(payload.get("decisions", []), "decisions"),
            open_items=_string_tuple(payload.get("open_items", []), "open_items"),
            last_turn_id=str(payload.get("last_turn_id", "")),
        )


def _string_tuple(raw: object, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise SessionSummaryError(f"{label} 必须是列表")
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise SessionSummaryError(f"{label} 必须是字符串")
        items.append(item)
    return tuple(items)


def _empty(session: str) -> SessionSummary:
    return SessionSummary(
        schema_version=SCHEMA_VERSION,
        session=session,
        updated_at="",
        headline="",
        what_done=(),
        artifacts=(),
        decisions=(),
        open_items=(),
        last_turn_id="",
    )


class SessionSummaryStore:
    """一个 session 一份 summary.json。更新是去重和截断，不调用模型。"""

    def __init__(self, root: Path, session: str) -> None:
        self._root = Path(root)
        self._session = _checked_session(session)
        self._lock = threading.Lock()
        self.problems: tuple[str, ...] = ()

    def load(self) -> SessionSummary:
        with self._lock:
            return self._read()

    def save(self, summary: SessionSummary) -> None:
        with self._lock:
            self._write(summary)

    def record_done(self, text: str, *, updated_at: str, turn_id: str = "") -> SessionSummary:
        return self._record_text("what_done", text, updated_at=updated_at, turn_id=turn_id)

    def record_decision(self, text: str, *, updated_at: str, turn_id: str = "") -> SessionSummary:
        return self._record_text("decisions", text, updated_at=updated_at, turn_id=turn_id)

    def record_open_item(self, text: str, *, updated_at: str, turn_id: str = "") -> SessionSummary:
        return self._record_text("open_items", text, updated_at=updated_at, turn_id=turn_id)

    def record_artifact(
        self,
        record: ArtifactRecord,
        *,
        updated_at: str,
        turn_id: str = "",
    ) -> SessionSummary:
        if record.session != self._session:
            raise SessionSummaryError("session 不一致")
        item = SummaryArtifact(
            id=record.id,
            title=record.title,
            summary=record.summary,
            path=record.path,
        )
        stamp = _plain(updated_at, "updated_at", required=True)
        turn = _plain(turn_id, "last_turn_id", required=False)
        with self._lock:
            current = self._read()
            if any(existing == item for existing in current.artifacts):
                return current
            merged = _artifacts(current.artifacts + (item,))
            updated = replace(
                current,
                artifacts=merged,
                updated_at=stamp,
                last_turn_id=turn or current.last_turn_id,
            )
            self._write(updated)
            return updated

    def _record_text(self, field: str, text: str, *, updated_at: str, turn_id: str) -> SessionSummary:
        cleaned = _plain(text, "条目", required=True)
        stamp = _plain(updated_at, "updated_at", required=True)
        turn = _plain(turn_id, "last_turn_id", required=False)
        with self._lock:
            current = self._read()
            items: tuple[str, ...] = getattr(current, field)
            if cleaned in items:
                return current
            updated = replace(
                current,
                updated_at=stamp,
                last_turn_id=turn or current.last_turn_id,
                **{field: items + (cleaned,)},
            )
            self._write(updated)
            return updated

    def _path(self) -> Path:
        directory = dir_for(self._root, self._session)
        base = (self._root / ".agent" / "sessions").resolve()
        resolved = directory.resolve()
        if not resolved.is_relative_to(base):
            raise SessionSummaryError("路径越界")
        return directory / _FILE

    def _read(self) -> SessionSummary:
        path = self._path()
        if not path.is_file():
            self.problems = ()
            return _empty(self._session)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return self._broken(exc)
        if not isinstance(payload, dict):
            return self._broken("summary.json 不是对象")
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SessionSummaryError(f"schema_version 不匹配：{version}")
        session = payload.get("session")
        if session != self._session:
            raise SessionSummaryError("记录与 session 不一致")
        try:
            summary = SessionSummary.from_dict(payload)
        except (SessionSummaryError, KeyError, TypeError, ValueError) as exc:
            return self._broken(exc)
        self.problems = ()
        return summary

    def _broken(self, reason: object) -> SessionSummary:
        text = str(reason)
        if "summary.json" not in text:
            text = f"summary.json 无法解析：{text}"
        self.problems = (text,)
        logger.warning("%s", text)
        return _empty(self._session)

    def _write(self, summary: SessionSummary) -> None:
        if summary.schema_version != SCHEMA_VERSION:
            raise SessionSummaryError(f"schema_version 不匹配：{summary.schema_version}")
        if summary.session != self._session:
            raise SessionSummaryError("记录与 session 不一致")
        payload = json.dumps(summary.to_dict(), ensure_ascii=False).encode("utf-8")
        self._atomic_write(self._path(), payload)
        self.problems = ()

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        except OSError as exc:
            raise SessionSummaryError(f"写不了 {path.name}") from exc
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
            raise SessionSummaryError(f"写不了 {path.name}") from exc
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise


def _clip(text: str) -> str:
    """收成摘要长度。超长时留下省略号，不再调用模型。"""
    cleaned = redact_secrets(str(text).strip())
    if len(cleaned) <= TEXT_CAP:
        return cleaned
    return cleaned[: TEXT_CAP - 1] + "…"


def record_session_outcome(
    store: SessionSummaryStore,
    *,
    session: str,
    updated_at: str,
    task_summary: str,
    artifacts: Sequence[ArtifactRecord],
    finished: bool,
    final: str,
    failure_reason: str,
    turn_id: str = "",
) -> None:
    """把这一轮的产物和结果写进会话摘要。

    空会话什么都不写。写入失败留在原地，调用方的任务结果不变。
    同一轮同样的内容再写一次，不追加重复条目。
    """
    if not str(session).strip() or str(session).strip() != store._session:
        return
    try:
        turn = _clip(turn_id)
        for record in artifacts:
            if record.session != store._session:
                continue
            store.record_artifact(record, updated_at=updated_at, turn_id=turn)
        done = _clip(final)
        reason = _clip(failure_reason) or done or "未完成"
        if finished and done:
            store.record_done(done, updated_at=updated_at, turn_id=turn)
        else:
            store.record_open_item(reason, updated_at=updated_at, turn_id=turn)
        current = store.load()
        headline = _clip(task_summary)
        updates: dict[str, str] = {}
        if not current.headline and headline:
            updates["headline"] = headline
        if turn and current.last_turn_id != turn:
            updates["last_turn_id"] = turn
        if updates:
            store.save(replace(current, updated_at=_plain(updated_at, "updated_at", required=True), **updates))
    except Exception:
        return
