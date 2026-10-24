"""当前会话的长文记忆只读模型。

三个接口都只看调用方已经定好的工作区根和 session。
查询串里出现的路径、session 名字都不当成磁盘位置。
这里不读 artifact 正文，也不创建、改写任何文件。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from spoolkit.agent.artifacts import ArtifactError, ArtifactRecord, ArtifactStore
from spoolkit.agent.document_checkpoint import CheckpointError, DocumentCheckpointStore
from spoolkit.agent.document_state import SchemaVersionError
from spoolkit.session_state import safe
from spoolkit.agent.session_summary import SCHEMA_VERSION, SessionSummaryError, SessionSummaryStore
from spoolkit.memory.transcript import redact_secrets

#: 一次最多交出去这么多条产物元数据。再大的 limit 也停在这里。
ARTIFACT_LIST_MAX = 50
#: 列表标题只取 goal 的前这么多个字。
CHECKPOINT_TITLE_CAP = 40


def artifact_limit(raw_path: str) -> int | None:
    """只认查询串里的一个十进制 `limit`。别的键，包括 path，直接丢掉。"""
    query = urlsplit(str(raw_path)).query
    if not query:
        return None
    try:
        parsed = parse_qs(query, keep_blank_values=False, strict_parsing=False)
    except ValueError:
        return None
    values = parsed.get("limit")
    if not values or len(values) != 1:
        return None
    text = values[0]
    if not text.isdigit():
        return None
    return int(text)


def read_artifacts(root: Path, session: str, *, limit: int | None = None) -> list[dict]:
    """当前 session 的产物元数据。空 session 是空列表。"""
    scope = str(session or "").strip()
    if not scope:
        return []
    try:
        records = ArtifactStore(root).list(scope, _clamp_limit(limit))
    except (ArtifactError, OSError, UnicodeError, ValueError):
        return []
    return [_artifact_meta(record) for record in records]


def read_session_summary(root: Path, session: str) -> dict:
    """当前 session 的摘要。没有文件、读坏了，都交一份空摘要。"""
    scope = str(session or "").strip()
    if not scope:
        return empty_summary("")
    try:
        summary = SessionSummaryStore(root, scope).load()
    except (SessionSummaryError, OSError, UnicodeError, ValueError):
        return empty_summary("")
    return summary.to_dict()


def read_checkpoints(root: Path, session: str) -> list[dict[str, str]]:
    """当前 session 的 checkpoint 元数据。文本先脱敏，再取标题。坏文件跳过。"""
    scope = str(session or "").strip()
    if not scope:
        return []
    try:
        rows = DocumentCheckpointStore(root).list_meta(scope)
    except (CheckpointError, OSError, UnicodeError, ValueError):
        return []
    found: list[dict[str, str]] = []
    for item in rows:
        redacted = {key: redact_secrets(value) for key, value in item.items()}
        redacted["title"] = redacted.get("goal", "")[:CHECKPOINT_TITLE_CAP]
        found.append(redacted)
    return found


def resume_key(value: object) -> str:
    """checkpoint 文件名。斜杠、盘符和 .. 都不是合法的键。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or text in {".", ".."} or text != Path(text).name:
        return ""
    if any(char in text for char in ("/", "\\", ":")) or ".." in text:
        return ""
    if safe(text) != text:
        return ""
    return text


def read_resume_fields(root: Path, session: str, key: str) -> dict[str, str] | None:
    """只读 goal、path、deliverable。没有这份 checkpoint 时返回 None，不写盘。"""
    scope = str(session or "").strip()
    token = resume_key(key)
    if not scope or not token:
        return None
    try:
        loaded = DocumentCheckpointStore(root).load(scope, token)
    except (CheckpointError, SchemaVersionError, OSError, UnicodeError, ValueError):
        return None
    if loaded is None or loaded.state.status == "done":
        return None
    if not str(loaded.state.goal or "").strip():
        return None
    return {
        "goal": loaded.state.goal,
        "path": loaded.state.path,
        "deliverable": loaded.state.deliverable,
    }


def empty_summary(session: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "session": session,
        "updated_at": "",
        "headline": "",
        "what_done": [],
        "artifacts": [],
        "decisions": [],
        "open_items": [],
        "last_turn_id": "",
    }


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return ARTIFACT_LIST_MAX
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        return ARTIFACT_LIST_MAX
    if limit > ARTIFACT_LIST_MAX:
        return ARTIFACT_LIST_MAX
    return limit


def _artifact_meta(record: ArtifactRecord) -> dict:
    return {
        "id": record.id,
        "title": redact_secrets(record.title),
        "summary": redact_secrets(record.summary),
        "path": redact_secrets(record.path),
        "kind": record.kind,
        "created_at": record.created_at,
        "sections": [
            {
                "index": section.index,
                "title": redact_secrets(section.title),
                "summary": redact_secrets(section.summary),
                "char_count": section.char_count,
            }
            for section in record.sections
        ],
    }
