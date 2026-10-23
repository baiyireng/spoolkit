"""FileSink 只把全文放进 artifact，并登记一处待确认修改。"""

import json
from pathlib import Path

import pytest

from spoolkit.agent.artifacts import ArtifactStore
from spoolkit.agent.document_state import DocumentState, OutlineEntry
from spoolkit.agent.file_sink import FileSink, FileSinkError
from spoolkit.tools.edit import PendingChanges

MARKER = "只有待确认稿里才有的正文甲"
TARGET = "docs/out.md"


def _state(path: str = TARGET) -> DocumentState:
    titles = ("窗口", "估算", "余量")
    return DocumentState(
        goal="写一份估算说明",
        outline=tuple(
            OutlineEntry(index=index, title=title, points=f"{title}的要点", target_chars=100)
            for index, title in enumerate(titles)
        ),
        deliverable="file",
        path=path,
        phase="plan",
        pending_sections=(0, 1, 2),
    )


def _body(title: str) -> str:
    return f"{MARKER}{title}写完。"


class _Pending(PendingChanges):
    def __init__(self, root: Path) -> None:
        super().__init__(root, overrides={}, run_id="run-1")
        self.propose_calls = 0
        self.apply_calls = 0

    def propose(self, path: str, new_text: str, declared: str = ""):  # type: ignore[override]
        self.propose_calls += 1
        return super().propose(path, new_text, declared)

    def apply(self) -> list[str]:
        self.apply_calls += 1
        raise AssertionError("不应落盘")


def _sink(tmp_path: Path, *, path: str = TARGET, state: DocumentState | None = None) -> tuple[FileSink, _Pending, Path]:
    pending = _Pending(tmp_path)
    target = tmp_path / path if path and not Path(path).is_absolute() and ".." not in Path(path).parts else None
    sink = FileSink(
        state=state or _state(path if path.strip() else TARGET),
        path=path,
        session="sess-a",
        task_id="task-1",
        turn_id="turn-1",
        artifacts=ArtifactStore(tmp_path),
        pending=pending,
        artifact_id="doc-1",
    )
    return sink, pending, tmp_path / TARGET


def _commit_all(sink: FileSink, bodies: dict[int, str] | None = None) -> None:
    state = sink.state
    chosen = bodies or {entry.index: _body(entry.title) for entry in state.outline}
    for entry in reversed(state.outline):
        sink.section_start(entry.index, entry.title)
        sink.section_commit(entry.index, chosen[entry.index])


def test_complete_stages_artifact_without_writing_target(tmp_path: Path) -> None:
    target = tmp_path / TARGET
    target.parent.mkdir(parents=True)
    target.write_text("旧稿。\n", encoding="utf-8")
    before = target.read_bytes()
    sink, pending, _ = _sink(tmp_path)
    sink.start(sink.state)
    _commit_all(sink)
    sink.progress("finalize", -1)
    sink.complete(object())
    assert target.read_bytes() == before
    assert pending.apply_calls == 0
    assert pending.propose_calls == 1
    assert len(pending.items()) == 1
    change = pending.items()[0]
    assert change.path == TARGET
    assert change.old_text == "旧稿。\n"
    assert change.new_text.index("## 窗口") < change.new_text.index("## 估算") < change.new_text.index("## 余量")
    assert sink.pending is change
    assert sink.artifact_id == "doc-1"
    assert sink.path == TARGET
    record, content = sink.artifacts.get("sess-a", "doc-1")
    assert content == change.new_text
    assert record.path.startswith(".agent/artifacts/sess-a/")
    assert record.path != TARGET
    refs = sink.section_refs()
    assert [item.artifact_id for item in refs] == ["doc-1", "doc-1", "doc-1"]
    assert {item.path for item in refs} == {record.path}
    viewed = DocumentState(
        goal=sink.state.goal,
        outline=sink.state.outline,
        deliverable="file",
        path=sink.path,
        phase="plan",
        completed_sections=refs,
    )
    blob = viewed.to_json()
    assert MARKER not in blob
    assert viewed.path == TARGET
    assert all(item.path != viewed.path for item in viewed.completed_sections)


def test_section_replace_updates_pending_text(tmp_path: Path) -> None:
    sink, pending, target = _sink(tmp_path)
    _commit_all(sink)
    sink.section_replace(1, f"{MARKER}估算已替换。")
    sink.complete(object())
    assert not target.exists()
    text = pending.items()[0].new_text
    assert f"{MARKER}估算已替换。" in text
    assert f"{MARKER}估算写完。" not in text
    assert sink.artifacts.get("sess-a", "doc-1")[1] == text
    assert pending.apply_calls == 0


def test_fail_leaves_no_pending_and_keeps_target(tmp_path: Path) -> None:
    target = tmp_path / TARGET
    target.parent.mkdir(parents=True)
    target.write_text("保持不变。\n", encoding="utf-8")
    sink, pending, _ = _sink(tmp_path)
    _commit_all(sink)
    sink.fail(("short_doc",))
    assert target.read_text(encoding="utf-8") == "保持不变。\n"
    assert pending.items() == []
    assert pending.propose_calls == 0
    assert pending.apply_calls == 0
    assert sink.pending is None
    assert sink.artifacts.exists("sess-a", "doc-1") is False


def test_unsafe_paths_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "escaped-out.md"
    for bad in ("", "   ", "/tmp/out.md", "C:/tmp/out.md", "../out.md", "docs/../../out.md"):
        with pytest.raises(FileSinkError):
            _sink(tmp_path, path=bad)
    assert not outside.exists()
    assert _Pending(tmp_path).items() == []


def test_second_complete_does_not_propose_again(tmp_path: Path) -> None:
    sink, pending, target = _sink(tmp_path)
    _commit_all(sink)
    sink.complete(object())
    sink.complete(object())
    assert pending.propose_calls == 1
    assert len(pending.items()) == 1
    assert not target.exists()
    assert pending.apply_calls == 0


def test_artifact_metadata_does_not_contain_body(tmp_path: Path) -> None:
    sink, pending, _ = _sink(tmp_path)
    _commit_all(sink)
    sink.complete(object())
    record = sink.artifacts.get("sess-a", "doc-1")[0]
    meta_path = tmp_path / ".agent" / "artifacts" / "sess-a" / f"{record.id}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    blob = json.dumps(meta, ensure_ascii=False)
    assert MARKER not in blob
    assert pending.items()[0].new_text not in blob
    assert meta["path"].startswith(".agent/artifacts/sess-a/")
