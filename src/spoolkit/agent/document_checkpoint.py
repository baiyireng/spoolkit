"""长文 checkpoint。

写在 `<root>/.agent/checkpoints/<session>/<checkpoint_key>.json`。
checkpoint_key 由 session、task、path、deliverable 决定，不含 turn。
空 path 的 chat 再混入 goal 的摘要，避免两份聊天稿共用一份进度。
文件里的 artifact_id 只记录当前这一轮，恢复后会换成新的产物编号。
state 仍是进度和引用。bodies 按节下标保留正文，供中断后继续写。
不写 TaskEvent，不写工作区目标文件。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from spoolkit.agent.document_state import SCHEMA_VERSION, DocumentState, DocumentStateError, SchemaVersionError
from spoolkit.session_state import safe

_DIR = "checkpoints"


class CheckpointError(ValueError):
    """路径或 checkpoint 不成立。"""


def _segment(value: str, label: str) -> str:
    raw = str(value).strip()
    if not raw or raw in {".", ".."} or raw != Path(raw).name:
        raise CheckpointError(f"{label} 路径越界")
    if any(char in raw for char in ("/", "\\", ":")) or ".." in raw:
        raise CheckpointError(f"{label} 路径越界")
    if safe(raw) != raw:
        raise CheckpointError(f"{label} 路径越界")
    return raw


_META_FIELDS = (
    "checkpoint_key",
    "artifact_id",
    "task_id",
    "turn_id",
    "phase",
    "status",
    "updated_at",
)


def _checkpoint_meta(directory: Path, path: Path, session: str) -> dict[str, str] | None:
    """一个普通 json。符号链接、子目录、坏文件和越界文件都不当记录。"""
    name = path.name
    if not name.endswith(".json") or name.startswith("."):
        return None
    key = name[: -len(".json")]
    if not key or key != safe(key) or ".." in key or "/" in key or "\\" in key:
        return None
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.parent.resolve() != directory:
            return None
        if not path.resolve().is_relative_to(directory):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return None
    if payload.get("session") != session or payload.get("checkpoint_key") != key:
        return None
    meta: dict[str, str] = {}
    for field in _META_FIELDS:
        value = payload.get(field, "")
        if not isinstance(value, str):
            return None
        meta[field] = value
    if meta["checkpoint_key"] != key:
        return None
    state = payload.get("state")
    source = state if isinstance(state, dict) else {}
    for field in ("goal", "path", "deliverable"):
        value = source.get(field, "")
        meta[field] = value if isinstance(value, str) else ""
    return meta


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def checkpoint_key(
    *,
    session: str,
    task_id: str,
    path: str = "",
    deliverable: str = "chat",
    goal: str = "",
) -> str:
    """同一份文稿跨 turn 的恢复键。不含 turn。"""
    relative = str(path).strip()
    kind = str(deliverable).strip() or "chat"
    task = str(task_id).strip()
    scope = str(session).strip()
    if relative:
        material = f"{scope}|{task}|{relative}|{kind}"
    else:
        goal_hash = hashlib.sha256(str(goal).strip().encode("utf-8")).hexdigest()
        material = f"{scope}|{task}|{kind}|{goal_hash}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"ck-{digest}"


@dataclass(frozen=True, slots=True)
class DocumentCheckpoint:
    """一次可恢复的长文进度。"""

    schema_version: int
    session: str
    checkpoint_key: str
    artifact_id: str
    task_id: str
    turn_id: str
    state: DocumentState
    bodies: tuple[tuple[int, str], ...]
    phase: str
    status: str
    updated_at: str

    def body_map(self) -> dict[int, str]:
        return dict(self.bodies)


class DocumentCheckpointStore:
    """一个 session、一个 checkpoint_key 一份进度。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def path_for(self, session: str, checkpoint_key: str) -> Path:
        folder = _segment(session, "session")
        name = _segment(checkpoint_key, "checkpoint_key")
        directory = (self._root / ".agent" / _DIR / folder).resolve()
        base = (self._root / ".agent" / _DIR).resolve()
        if not directory.is_relative_to(base):
            raise CheckpointError("路径越界")
        return directory / f"{name}.json"

    def save(self, checkpoint: DocumentCheckpoint) -> None:
        if checkpoint.schema_version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"schema_version {checkpoint.schema_version} 与 {SCHEMA_VERSION} 不一致"
            )
        if checkpoint.state.schema_version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"schema_version {checkpoint.state.schema_version} 与 {SCHEMA_VERSION} 不一致"
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "session": checkpoint.session,
            "checkpoint_key": checkpoint.checkpoint_key,
            "artifact_id": checkpoint.artifact_id,
            "task_id": checkpoint.task_id,
            "turn_id": checkpoint.turn_id,
            "state": checkpoint.state.to_dict(),
            "bodies": {str(index): text for index, text in checkpoint.bodies},
            "phase": checkpoint.phase or checkpoint.state.phase,
            "status": checkpoint.status or checkpoint.state.status,
            "updated_at": checkpoint.updated_at or _stamp(),
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._atomic_write(self.path_for(checkpoint.session, checkpoint.checkpoint_key), encoded)

    def load(self, session: str, checkpoint_key: str) -> DocumentCheckpoint | None:
        """坏文件返回 None。schema 不一致则拒绝。"""
        path = self.path_for(session, checkpoint_key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"schema_version {payload.get('schema_version')} 与 {SCHEMA_VERSION} 不一致"
            )
        if payload.get("session") != session or payload.get("checkpoint_key") != checkpoint_key:
            return None
        state_raw = payload.get("state")
        if not isinstance(state_raw, dict):
            return None
        try:
            state = DocumentState.from_dict(state_raw)
        except SchemaVersionError:
            raise
        except (DocumentStateError, KeyError, TypeError, ValueError):
            return None
        bodies_raw = payload.get("bodies", {})
        if not isinstance(bodies_raw, dict):
            return None
        bodies: list[tuple[int, str]] = []
        try:
            for key, text in bodies_raw.items():
                if not isinstance(text, str):
                    return None
                bodies.append((int(key), text))
        except (TypeError, ValueError):
            return None
        bodies.sort(key=lambda item: item[0])
        phase = payload.get("phase")
        if not isinstance(phase, str) or not phase:
            phase = state.phase
        updated = payload.get("updated_at")
        if not isinstance(updated, str):
            updated = ""
        task_id = payload.get("task_id")
        turn_id = payload.get("turn_id")
        artifact_id = payload.get("artifact_id")
        status = payload.get("status")
        if not isinstance(status, str) or not status:
            status = state.status
        return DocumentCheckpoint(
            schema_version=SCHEMA_VERSION,
            session=session,
            checkpoint_key=checkpoint_key,
            artifact_id=artifact_id if isinstance(artifact_id, str) else "",
            task_id=task_id if isinstance(task_id, str) else "",
            turn_id=turn_id if isinstance(turn_id, str) else "",
            state=state,
            bodies=tuple(bodies),
            phase=phase,
            status=status,
            updated_at=updated,
        )

    def list_meta(self, session: str) -> tuple[dict[str, str], ...]:
        """只读这一层的元数据。带上 goal、path、deliverable。坏文件跳过，不返回 state 和 bodies，也不写盘。"""
        try:
            folder = _segment(session, "session")
        except CheckpointError:
            return ()
        base = (self._root / ".agent" / _DIR).resolve()
        directory = (self._root / ".agent" / _DIR / folder).resolve()
        if not directory.is_relative_to(base) or not directory.is_dir():
            return ()
        try:
            entries = list(directory.iterdir())
        except OSError:
            return ()
        found: list[dict[str, str]] = []
        for path in entries:
            item = _checkpoint_meta(directory, path, folder)
            if item is not None:
                found.append(item)
        found.sort(key=lambda item: (item["updated_at"], item["checkpoint_key"]), reverse=True)
        return tuple(found)

    def delete(self, session: str, checkpoint_key: str) -> None:
        path = self.path_for(session, checkpoint_key)
        with contextlib.suppress(OSError):
            path.unlink()

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        except OSError as exc:
            raise CheckpointError(f"写不了 {path.name}") from exc
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
            raise CheckpointError(f"写不了 {path.name}") from exc
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
