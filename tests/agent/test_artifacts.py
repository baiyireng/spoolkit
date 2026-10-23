"""Artifact 正文落在 .agent/artifacts，状态和事件只留引用。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from spoolkit.agent.artifacts import (
    SUMMARY_CAP,
    ArtifactError,
    ArtifactNotFound,
    ArtifactRecord,
    ArtifactSection,
    ArtifactStore,
    as_task_artifact,
)
from spoolkit.agent.contract import TaskEvent
from spoolkit.agent.document_state import CompletedSection, DocumentState, OutlineEntry

BODY = "只有文件里才有的正文甲。这一段不该出现在状态或事件里。"


def _section(**overrides: object) -> ArtifactSection:
    payload: dict[str, object] = {
        "index": 0,
        "title": "窗口",
        "summary": "窗口占预算。",
        "char_count": 12,
    }
    payload.update(overrides)
    return ArtifactSection(**payload)  # type: ignore[arg-type]


def _record(**overrides: object) -> ArtifactRecord:
    payload: dict[str, object] = {
        "id": "doc-1",
        "session": "sess-a",
        "task_id": "task-1",
        "turn_id": "turn-1",
        "kind": "document",
        "title": "估算说明",
        "summary": "三节估算。",
        "created_at": "2026-10-23T00:00:00Z",
        "sections": (_section(),),
    }
    payload.update(overrides)
    return ArtifactRecord(**payload)  # type: ignore[arg-type]


def test_put_get_list_exists(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    stored = store.put(_record(), BODY)
    assert stored.id == "doc-1"
    assert stored.content_hash
    assert stored.size == len(BODY.encode("utf-8"))
    assert stored.path == f".agent/artifacts/sess-a/{stored.content_hash}"
    assert store.exists("sess-a", "doc-1")
    assert store.exists("sess-a", "missing") is False
    record, content = store.get("sess-a", "doc-1")
    assert record == stored
    assert content == BODY
    assert store.list("sess-a", limit=10) == (stored,)
    assert store.list("other", limit=10) == ()


def test_list_limit_returns_newest(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    older = store.put(_record(id="doc-old", created_at="2026-10-23T00:00:00Z"), "旧正文。")
    newer = store.put(_record(id="doc-new", created_at="2026-10-23T01:00:00Z"), "新正文。")
    assert store.list("sess-a", limit=1) == (newer,)
    assert older in store.list("sess-a", limit=2)


def test_same_content_hash_returns_existing(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    first = store.put(_record(), BODY)
    second = store.put(_record(id="doc-2", title="另一份"), BODY)
    assert second == first
    assert store.exists("sess-a", "doc-2") is False
    files = [path for path in (tmp_path / ".agent" / "artifacts").rglob("*") if path.is_file()]
    bodies = [path for path in files if path.name == first.content_hash]
    assert len(bodies) == 1


def test_same_id_does_not_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ArtifactStore(tmp_path)
    first = store.put(_record(), BODY)
    content_path = tmp_path / first.path
    before = content_path.read_bytes()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("同一 id 不应再写文件")

    monkeypatch.setattr("spoolkit.agent.artifacts.os.replace", boom)
    again = store.put(_record(summary="后来的摘要"), BODY)
    assert again == first
    assert content_path.read_bytes() == before


def test_same_id_rejects_different_content(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    store.put(_record(), BODY)
    with pytest.raises(ArtifactError):
        store.put(_record(), "另一份正文。")
    _record_again, content = store.get("sess-a", "doc-1")
    assert content == BODY


def test_failed_replace_leaves_no_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ArtifactStore(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("spoolkit.agent.artifacts.os.replace", boom)
    with pytest.raises(ArtifactError):
        store.put(_record(), BODY)
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert files == []
    assert store.exists("sess-a", "doc-1") is False


def test_path_escape_is_rejected(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    outside = tmp_path.parent / "escaped-artifact.txt"
    for bad in (
        _record(session="../outside"),
        _record(session="sess-a/../../outside"),
        _record(id="../doc"),
        _record(path="../../escaped-artifact.txt"),
    ):
        with pytest.raises(ArtifactError):
            store.put(bad, BODY)
    assert not outside.exists()
    assert store.exists("sess-a", "doc-1") is False
    assert list(tmp_path.rglob("*")) == []


def test_summary_and_section_caps() -> None:
    with pytest.raises(ArtifactError):
        _record(summary="甲" * (SUMMARY_CAP + 1))
    with pytest.raises(ArtifactError):
        _section(summary="乙" * (SUMMARY_CAP + 1))
    with pytest.raises(ArtifactError):
        _record(kind="note")
    with pytest.raises(ArtifactError):
        _section(index=-1)


def test_body_stays_out_of_state_and_task_event(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    stored = store.put(_record(), BODY)
    state = DocumentState(
        goal="写一份说明",
        outline=(OutlineEntry(index=0, title="窗口", points="窗口的要点", target_chars=100),),
        deliverable="file",
        path="docs/out.md",
        phase="plan",
        completed_sections=(
            CompletedSection(
                index=0,
                title="窗口",
                summary=stored.summary,
                artifact_id=stored.id,
                path=stored.path,
                char_count=stored.sections[0].char_count,
            ),
        ),
    )
    state_blob = state.to_json()
    assert stored.id in state_blob
    assert stored.path in state_blob
    assert BODY not in state_blob
    event = TaskEvent(
        task_id="task-1",
        run_id="run-1",
        turn_id="turn-1",
        seq=1,
        step=1,
        phase="final",
        source="system",
        summary="长文完成：1 节",
        artifacts=(as_task_artifact(stored),),
    )
    event_blob = event.to_json()
    assert stored.id in event_blob
    assert stored.path in event_blob
    assert BODY not in event_blob
    meta = json.loads((tmp_path / ".agent" / "artifacts" / "sess-a" / f"{stored.id}.json").read_text(encoding="utf-8"))
    assert BODY not in json.dumps(meta, ensure_ascii=False)


def test_missing_content_file_is_not_invented(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    stored = store.put(_record(), BODY)
    (tmp_path / stored.path).unlink()
    with pytest.raises(ArtifactNotFound):
        store.get("sess-a", "doc-1")


def _completed(record: ArtifactRecord) -> CompletedSection:
    return CompletedSection(
        index=0,
        title=record.sections[0].title,
        summary=record.summary,
        artifact_id=record.id,
        path=record.path,
        char_count=record.sections[0].char_count,
    )


def test_same_body_in_another_session_keeps_its_own_record(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    first = store.put(_record(), BODY)
    second = store.put(_record(id="doc-b", session="sess-b", title="另一会话"), BODY)
    assert second.session == "sess-b"
    assert second.id == "doc-b"
    assert second.path == f".agent/artifacts/sess-b/{second.content_hash}"
    assert second.content_hash == first.content_hash
    assert second != first
    assert store.list("sess-a", limit=10) == (first,)
    assert store.list("sess-b", limit=10) == (second,)
    got_a, content_a = store.get("sess-a", "doc-1")
    got_b, content_b = store.get("sess-b", "doc-b")
    assert (got_a, content_a) == (first, BODY)
    assert (got_b, content_b) == (second, BODY)
    assert store.exists("sess-a", "doc-b") is False
    assert store.exists("sess-b", "doc-1") is False
    bodies = [
        path
        for path in (tmp_path / ".agent" / "artifacts").rglob("*")
        if path.is_file() and path.name == first.content_hash
    ]
    assert {path.parent.name for path in bodies} == {"sess-a", "sess-b"}
    section_a = _completed(first)
    section_b = _completed(second)
    assert section_a.artifact_id == "doc-1"
    assert section_b.artifact_id == "doc-b"
    assert section_a.path.startswith(".agent/artifacts/sess-a/")
    assert section_b.path.startswith(".agent/artifacts/sess-b/")
    assert BODY not in DocumentState(
        goal="写一份说明",
        outline=(OutlineEntry(index=0, title="窗口", points="窗口的要点", target_chars=100),),
        deliverable="file",
        path="docs/out.md",
        phase="plan",
        completed_sections=(section_a,),
    ).to_json()


def test_same_id_in_another_session_does_not_cross(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    first = store.put(_record(), BODY)
    other = "会话乙自己的正文。"
    second = store.put(_record(session="sess-b", title="乙的说明"), other)
    assert second.session == "sess-b"
    assert second.id == "doc-1"
    assert store.get("sess-a", "doc-1") == (first, BODY)
    assert store.get("sess-b", "doc-1") == (second, other)
    assert store.list("sess-a", limit=10) == (first,)
    assert store.list("sess-b", limit=10) == (second,)
    third = store.put(_record(session="sess-c"), BODY)
    assert third.session == "sess-c"
    assert third.id == "doc-1"
    assert third.path.startswith(".agent/artifacts/sess-c/")
    assert store.get("sess-c", "doc-1") == (third, BODY)
    assert store.get("sess-a", "doc-1")[0] == first
    assert store.exists("sess-a", "doc-1") is True
    assert store.exists("sess-b", "missing") is False


def test_returned_id_is_the_caller_id(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    stored = store.put(_record(), BODY)
    assert stored.id == "doc-1"
    again = ArtifactStore(tmp_path).get("sess-a", "doc-1")[0]
    assert again.id == stored.id
    assert again.path == stored.path
    assert replace(stored, summary="x") != stored
