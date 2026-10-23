"""长文 checkpoint。

写在 `<root>/.agent/checkpoints/<session>/<artifact_id>.json`。
state 仍是进度和引用。bodies 按节下标保留正文，供中断后继续写。
不写 TaskEvent，不写工作区目标文件。
"""

from __future__ import annotations

import contextlib
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


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class DocumentCheckpoint:
    """一次可恢复的长文进度。"""

    schema_version: int
    session: str
    artifact_id: str
    task_id: str
    turn_id: str
    state: DocumentState
    bodies: tuple[tuple[int, str], ...]
    phase: str
    updated_at: str

    def body_map(self) -> dict[int, str]:
        return dict(self.bodies)


class DocumentCheckpointStore:
    """一个 session、一个 artifact 一份 checkpoint。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def path_for(self, session: str, artifact_id: str) -> Path:
        folder = _segment(session, "session")
        name = _segment(artifact_id, "artifact_id")
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
            "artifact_id": checkpoint.artifact_id,
            "task_id": checkpoint.task_id,
            "turn_id": checkpoint.turn_id,
            "state": checkpoint.state.to_dict(),
            "bodies": {str(index): text for index, text in checkpoint.bodies},
            "phase": checkpoint.phase or checkpoint.state.phase,
            "updated_at": checkpoint.updated_at or _stamp(),
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._atomic_write(self.path_for(checkpoint.session, checkpoint.artifact_id), encoded)

    def load(self, session: str, artifact_id: str) -> DocumentCheckpoint | None:
        """坏文件返回 None。schema 不一致则拒绝。"""
        path = self.path_for(session, artifact_id)
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
        if payload.get("session") != session or payload.get("artifact_id") != artifact_id:
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
        return DocumentCheckpoint(
            schema_version=SCHEMA_VERSION,
            session=session,
            artifact_id=artifact_id,
            task_id=task_id if isinstance(task_id, str) else "",
            turn_id=turn_id if isinstance(turn_id, str) else "",
            state=state,
            bodies=tuple(bodies),
            phase=phase,
            updated_at=updated,
        )

    def delete(self, session: str, artifact_id: str) -> None:
        path = self.path_for(session, artifact_id)
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
