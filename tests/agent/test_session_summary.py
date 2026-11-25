"""会话摘要只存进度和引用，字符串先脱敏，不进正文。"""

import json
from pathlib import Path

import pytest

from spoolkit.agent.artifacts import ArtifactRecord
from spoolkit.agent.session_summary import (
    LIST_CAP,
    SCHEMA_VERSION,
    TEXT_CAP,
    SessionSummary,
    SessionSummaryError,
    SessionSummaryStore,
    SummaryArtifact,
)

SECRET = "sk-test-secret-value"
BODY = "只有正文里才有的句子甲。不该出现在会话摘要里。"


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
        "path": ".agent/artifacts/sess-a/abc",
    }
    payload.update(overrides)
    return ArtifactRecord(**payload)  # type: ignore[arg-type]


def _summary(**overrides: object) -> SessionSummary:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "session": "sess-a",
        "updated_at": "2026-10-23T00:00:00Z",
        "headline": "估算进行中",
        "what_done": ("写完大纲",),
        "artifacts": (
            SummaryArtifact(
                id="doc-1",
                title="估算说明",
                summary="三节估算。",
                path=".agent/artifacts/sess-a/abc",
            ),
        ),
        "decisions": ("按三节展开",),
        "open_items": ("还没写结尾",),
        "last_turn_id": "turn-1",
    }
    payload.update(overrides)
    return SessionSummary(**payload)  # type: ignore[arg-type]


def test_missing_file_loads_empty_summary(tmp_path: Path) -> None:
    store = SessionSummaryStore(tmp_path, "sess-a")
    loaded = store.load()
    assert loaded == SessionSummary(
        schema_version=SCHEMA_VERSION,
        session="sess-a",
        updated_at="",
        headline="",
        what_done=(),
        artifacts=(),
        decisions=(),
        open_items=(),
        last_turn_id="",
    )
    assert store.problems == ()
    assert not (tmp_path / ".agent" / "sessions" / "sess-a" / "summary.json").exists()


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    store = SessionSummaryStore(tmp_path, "sess-a")
    original = _summary()
    store.save(original)
    assert store.load() == original
    path = tmp_path / ".agent" / "sessions" / "sess-a" / "summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert set(payload["artifacts"][0]) == {"id", "title", "summary", "path"}
    assert BODY not in path.read_text(encoding="utf-8")


def test_failed_replace_keeps_previous_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionSummaryStore(tmp_path, "sess-a")
    store.save(_summary())
    path = tmp_path / ".agent" / "sessions" / "sess-a" / "summary.json"
    before = path.read_bytes()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("spoolkit.agent.session_summary.os.replace", boom)
    with pytest.raises(SessionSummaryError):
        store.save(_summary(headline="后来的标题", updated_at="2026-10-23T01:00:00Z"))
    assert path.read_bytes() == before
    leftovers = [item for item in path.parent.glob("*.tmp") if item.is_file()]
    assert leftovers == []
    assert store.load().headline == "估算进行中"


def test_schema_mismatch_is_rejected(tmp_path: Path) -> None:
    store = SessionSummaryStore(tmp_path, "sess-a")
    path = tmp_path / ".agent" / "sessions" / "sess-a" / "summary.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION + 1, "session": "sess-a"}),
        encoding="utf-8",
    )
    with pytest.raises(SessionSummaryError, match="schema_version"):
        store.load()


def test_broken_json_returns_empty_summary_and_records_why(tmp_path: Path) -> None:
    store = SessionSummaryStore(tmp_path, "sess-a")
    path = tmp_path / ".agent" / "sessions" / "sess-a" / "summary.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    loaded = store.load()
    assert loaded.session == "sess-a"
    assert loaded.what_done == ()
    assert loaded.artifacts == ()
    assert store.problems
    assert "summary.json" in store.problems[0]


def test_session_path_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "summary.json"
    for bad in ("../outside", "sess-a/../../outside", "..", "", "a/b", "C:/tmp"):
        with pytest.raises(SessionSummaryError, match="路径越界"):
            SessionSummaryStore(tmp_path, bad)
    assert not outside.exists()
    assert list(tmp_path.rglob("summary.json")) == []


def test_updates_dedupe_and_drop_oldest(tmp_path: Path) -> None:
    store = SessionSummaryStore(tmp_path, "sess-a")
    first = store.record_done("写完大纲", updated_at="2026-10-23T00:00:00Z", turn_id="turn-1")
    again = store.record_done("写完大纲", updated_at="2026-10-23T00:01:00Z", turn_id="turn-2")
    assert again == first
    assert store.load().what_done == ("写完大纲",)
    assert store.load().updated_at == "2026-10-23T00:00:00Z"

    store.record_decision("按三节展开", updated_at="2026-10-23T00:02:00Z", turn_id="turn-3")
    store.record_decision("按三节展开", updated_at="2026-10-23T00:03:00Z")
    store.record_open_item("还没写结尾", updated_at="2026-10-23T00:04:00Z", turn_id="turn-4")
    store.record_open_item("还没写结尾", updated_at="2026-10-23T00:05:00Z")
    loaded = store.load()
    assert loaded.decisions == ("按三节展开",)
    assert loaded.open_items == ("还没写结尾",)
    assert loaded.last_turn_id == "turn-4"

    record = _record()
    store.record_artifact(record, updated_at="2026-10-23T00:06:00Z", turn_id="turn-5")
    store.record_artifact(record, updated_at="2026-10-23T00:07:00Z")
    revised = _record(summary="三节估算，含窗口。")
    stored = store.record_artifact(revised, updated_at="2026-10-23T00:08:00Z", turn_id="turn-6")
    assert [item.id for item in stored.artifacts] == ["doc-1"]
    assert stored.artifacts[0].summary == "三节估算，含窗口。"
    assert stored.artifacts[0].path == record.path
    assert not hasattr(stored.artifacts[0], "content")

    for index in range(LIST_CAP + 2):
        store.record_done(f"事项{index:02d}", updated_at=f"2026-10-23T01:{index:02d}:00Z", turn_id=f"turn-d{index}")
    done = store.load().what_done
    assert len(done) == LIST_CAP
    assert done[0] == "事项02"
    assert done[-1] == f"事项{LIST_CAP + 1:02d}"
    assert "写完大纲" not in done

    with pytest.raises(SessionSummaryError):
        store.record_done("甲" * (TEXT_CAP + 1), updated_at="2026-10-23T03:00:00Z")


def test_secrets_are_redacted_and_body_stays_out(tmp_path: Path) -> None:
    store = SessionSummaryStore(tmp_path, "sess-a")
    store.record_done(f"配置 API_KEY={SECRET} 之后继续", updated_at="2026-10-23T00:00:00Z", turn_id="turn-1")
    store.record_done(
        "配置 API_KEY=sk-other-secret-value 之后继续",
        updated_at="2026-10-23T00:01:00Z",
    )
    record = _record(summary=f"见 API_KEY={SECRET} 。")
    store.record_artifact(record, updated_at="2026-10-23T00:02:00Z", turn_id="turn-2")
    path = tmp_path / ".agent" / "sessions" / "sess-a" / "summary.json"
    text = path.read_text(encoding="utf-8")
    assert SECRET not in text
    assert "sk-other-secret-value" not in text
    assert BODY not in text
    assert "API_KEY=***" in text
    loaded = store.load()
    assert loaded.what_done == ("配置 API_KEY=*** 之后继续",)
    assert SECRET not in loaded.artifacts[0].summary
    assert BODY not in loaded.artifacts[0].summary
    assert loaded.artifacts[0].title == "估算说明"
