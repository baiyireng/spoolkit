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
from spoolkit.agent.session_summary import SCHEMA_VERSION, SessionSummaryError, SessionSummaryStore
from spoolkit.memory.transcript import redact_secrets

#: 一次最多交出去这么多条产物元数据。再大的 limit 也停在这里。
ARTIFACT_LIST_MAX = 50


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
    """当前 session 的 checkpoint 元数据。坏文件跳过。"""
    scope = str(session or "").strip()
    if not scope:
        return []
    try:
        return [dict(item) for item in DocumentCheckpointStore(root).list_meta(scope)]
    except (CheckpointError, OSError, UnicodeError, ValueError):
        return []


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
        "path": record.path,
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
