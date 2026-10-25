"""旧 artifact 只提供标题和摘要，DocumentState 不收正文。"""

import json
from pathlib import Path

import pytest

from spoolkit.agent.artifacts import ArtifactRecord, ArtifactSection, ArtifactStore
from spoolkit.agent.document_source import (
    DocumentSourceError,
    build_document_state_from_source,
    load_document_source,
)
from spoolkit.agent.document_state import SUMMARY_CAP, DocumentState, OutlineEntry

TAIL = "尾巴标记甲"


def _record(**overrides: object) -> ArtifactRecord:
    payload: dict[str, object] = {
        "id": "doc-1",
        "session": "sess-a",
        "task_id": "task-1",
        "turn_id": "turn-1",
        "kind": "document",
        "title": "估算说明",
        "summary": "元数据摘要。",
        "created_at": "2026-10-23T00:00:00Z",
        "sections": (
            ArtifactSection(index=0, title="元数据节", summary="元数据摘要。", char_count=4),
        ),
    }
    payload.update(overrides)
    return ArtifactRecord(**payload)  # type: ignore[arg-type]


def _markdown(*parts: tuple[str, str]) -> str:
    return "\n\n".join(f"## {title}\n\n{body}" for title, body in parts)


def _put(tmp_path: Path, content: str, **overrides: object) -> ArtifactStore:
    store = ArtifactStore(tmp_path)
    store.put(_record(**overrides), content)
    return store


def test_parses_two_and_three_sections(tmp_path: Path) -> None:
    two = _markdown(("窗口", "窗口正文甲。"), ("预算", "预算正文乙。"))
    source = load_document_source(_put(tmp_path, two), "sess-a", "doc-1")
    assert source.artifact_id == "doc-1"
    assert source.title == "估算说明"
    assert source.summary == "元数据摘要。"
    assert source.content == two
    assert [(item.index, item.title, item.body, item.summary, item.char_count) for item in source.sections] == [
        (0, "窗口", "窗口正文甲。", "窗口正文甲。", len("窗口正文甲。")),
        (1, "预算", "预算正文乙。", "预算正文乙。", len("预算正文乙。")),
    ]
    three = _markdown(("甲", "甲的正文。"), ("乙", "乙的正文。"), ("丙", "丙的正文。"))
    loaded = load_document_source(_put(tmp_path / "other", three, id="doc-3"), "sess-a", "doc-3")
    assert [item.title for item in loaded.sections] == ["甲", "乙", "丙"]
    assert [item.index for item in loaded.sections] == [0, 1, 2]


def test_missing_heading_is_one_section(tmp_path: Path) -> None:
    content = "没有标题的整篇正文。"
    source = load_document_source(_put(tmp_path, content), "sess-a", "doc-1")
    assert len(source.sections) == 1
    section = source.sections[0]
    assert section.index == 0
    assert section.title == "估算说明"
    assert section.body == content
    assert section.summary == content
    assert section.char_count == len(content)
    assert section.title != "元数据节"


def test_empty_content_is_rejected(tmp_path: Path) -> None:
    store = _put(tmp_path, "  \n")
    with pytest.raises(DocumentSourceError, match="正文为空"):
        load_document_source(store, "sess-a", "doc-1")


def test_state_keeps_outline_and_drops_the_body(tmp_path: Path) -> None:
    long_body = "甲" * 300 + TAIL
    content = _markdown(("窗口", "窗口正文甲。"), ("预算", long_body))
    source = load_document_source(_put(tmp_path, content), "sess-a", "doc-1")
    assert source.sections[1].summary == "甲" * (SUMMARY_CAP - 1) + "…"
    assert len(source.sections[1].summary) <= SUMMARY_CAP
    assert TAIL not in source.sections[1].summary
    state = build_document_state_from_source(
        source,
        new_goal="整理成新说明",
        deliverable="chat",
        path="docs/ignored.md",
    )
    assert state.goal == "整理成新说明"
    assert state.source_artifacts == ("doc-1",)
    assert state.phase == "plan"
    assert state.current_section is None
    assert state.pending_sections == (0, 1)
    assert state.completed_sections == ()
    assert state.deliverable == "chat"
    assert state.path == ""
    assert [item.title for item in state.outline] == ["窗口", "预算"]
    assert [item.points for item in state.outline] == [item.summary for item in source.sections]
    assert [item.index for item in state.outline] == [0, 1]
    blob = state.to_json()
    assert TAIL not in blob
    assert long_body not in blob
    assert content not in blob
    assert "元数据节" not in blob
    file_state = build_document_state_from_source(
        source,
        new_goal="整理成新说明",
        deliverable="file",
        path="docs/out.md",
    )
    assert file_state.deliverable == "file"
    assert file_state.path == "docs/out.md"
    assert long_body not in file_state.to_json()


def test_existing_outline_is_kept_and_old_state_stays(tmp_path: Path) -> None:
    content = _markdown(("窗口", "窗口正文甲。"), ("预算", "预算正文乙。"))
    source = load_document_source(_put(tmp_path, content), "sess-a", "doc-1")
    outline = (
        OutlineEntry(index=0, title="新窗口", points="调用方给的要点。", target_chars=800),
    )
    old = DocumentState(goal="旧目标", outline=outline, phase="plan", pending_sections=(0,))
    before = old.to_json()
    built = build_document_state_from_source(
        source,
        new_goal="新目标",
        deliverable="chat",
        path="",
        existing_outline=old.outline,
    )
    assert old.to_json() == before
    assert built.outline == outline
    assert built.goal == "新目标"
    assert built.source_artifacts == ("doc-1",)
    assert built.pending_sections == (0,)
    assert built.phase == "plan"
    assert built.current_section is None
    assert "窗口正文甲" not in built.to_json()


def test_file_without_path_is_rejected_and_put_is_not_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _markdown(("窗口", "窗口正文甲。"), ("预算", "预算正文乙。"))
    store = _put(tmp_path, content)
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file())

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("不应写入 artifact")

    monkeypatch.setattr(ArtifactStore, "put", boom)
    source = load_document_source(store, "sess-a", "doc-1")
    with pytest.raises(DocumentSourceError, match="路径"):
        build_document_state_from_source(source, new_goal="新目标", deliverable="file", path="")
    with pytest.raises(DocumentSourceError, match="路径"):
        build_document_state_from_source(source, new_goal="新目标", deliverable="file", path="../out.md")
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file())
    assert after == before
    json.loads((tmp_path / ".agent" / "artifacts" / "sess-a" / "doc-1.json").read_text(encoding="utf-8"))
