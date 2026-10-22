"""DocumentState 只存进度和引用，完成线在正文参数上校验。"""

import pytest

from spoolkit.agent.document_state import (
    DOC_FLOOR,
    SCHEMA_VERSION,
    SECTION_FLOOR,
    TAIL_CAP,
    CompletedSection,
    CurrentSection,
    DocumentState,
    DocumentStateError,
    OutlineEntry,
    SchemaVersionError,
    has_think,
    repeats_existing,
    scaffold_hits,
    validate_completion,
)


def _outline() -> tuple[OutlineEntry, ...]:
    return tuple(
        OutlineEntry(index=index, title=title, points=f"{title}的要点", target_chars=500)
        for index, title in enumerate(("窗口", "估算", "余量"))
    )


def _closed(title: str, chars: int = 500) -> str:
    unit = f"{title}写完。"
    text = unit * ((chars // len(unit)) + 1)
    if len(text) > chars:
        text = text[: chars - 1] + "。"
    return text


def _state(**overrides: object) -> DocumentState:
    payload: dict[str, object] = {
        "goal": "写一份估算说明",
        "outline": _outline(),
        "deliverable": "file",
        "path": "docs/estimate.md",
        "source_artifacts": ("art-old",),
        "pending_sections": (1, 2),
        "current_section": CurrentSection(
            index=0,
            title="窗口",
            summary="窗口占 token。",
            tail="总需求为",
            char_count=171,
            status="open",
        ),
        "phase": "write",
        "repair_attempts": (0, 0, 0),
        "rewrite_attempts": (0, 0, 0),
    }
    payload.update(overrides)
    return DocumentState(**payload)  # type: ignore[arg-type]


def test_legal_state_roundtrip() -> None:
    state = _state()
    restored = DocumentState.from_json(state.to_json())
    assert restored == state
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.length_floor == SECTION_FLOOR
    blob = state.to_json()
    assert "总需求为" in blob
    assert "整节正文" not in blob


def test_state_does_not_keep_section_body() -> None:
    state = _state()
    encoded = state.to_dict()
    assert "body" not in encoded
    assert "text" not in encoded
    current = encoded["current_section"]
    assert isinstance(current, dict)
    assert set(current) == {"index", "title", "summary", "tail", "char_count", "status"}
    assert len(str(current["tail"])) <= TAIL_CAP
    completed = encoded["completed_sections"]
    assert completed == []


def test_tail_and_summary_caps() -> None:
    with pytest.raises(DocumentStateError, match="尾部"):
        CurrentSection(
            index=0,
            title="窗口",
            summary="",
            tail="甲" * (TAIL_CAP + 1),
            char_count=TAIL_CAP + 1,
            status="open",
        )
    with pytest.raises(DocumentStateError, match="摘要"):
        CompletedSection(
            index=0,
            title="窗口",
            summary="甲" * 241,
            artifact_id="art-1",
            path="docs/a.md",
            char_count=500,
        )


def test_illegal_references_and_deliverable() -> None:
    with pytest.raises(DocumentStateError, match="deliverable"):
        _state(deliverable="mail")
    with pytest.raises(DocumentStateError, match="重叠"):
        _state(
            pending_sections=(0, 1),
            completed_sections=(
                CompletedSection(0, "窗口", "摘要", "art-1", "docs/a.md", 500),
            ),
            current_section=None,
        )
    with pytest.raises(DocumentStateError, match="重复 id"):
        _state(source_artifacts=("art-old", "art-old"))


def test_extra_body_field_is_rejected() -> None:
    payload = _state().to_dict()
    assert isinstance(payload["current_section"], dict)
    payload["current_section"]["body"] = "甲" * 800
    with pytest.raises(DocumentStateError, match="不允许的字段"):
        DocumentState.from_dict(payload)


def test_schema_version_mismatch_is_refused() -> None:
    payload = _state().to_dict()
    payload["schema_version"] = 2
    with pytest.raises(SchemaVersionError):
        DocumentState.from_dict(payload)
    del payload["schema_version"]
    with pytest.raises(SchemaVersionError, match="缺少"):
        DocumentState.from_dict(payload)
    with pytest.raises(SchemaVersionError):
        DocumentState(
            goal="写一份估算说明",
            outline=_outline(),
            schema_version=0,
        )


def test_length_checks() -> None:
    short = (_closed("窗口", 449), _closed("估算", 500), _closed("余量", 500))
    reasons = validate_completion(short)
    assert "short_section" in reasons
    assert "short_doc" in reasons
    long = tuple(_closed(title, 500) for title in ("窗口", "估算", "余量", "截断", "核对", "做法"))
    assert sum(len(item.strip()) for item in long) >= DOC_FLOOR
    assert validate_completion(long) == ()
    state = _state()
    assert state.check(long) == ()


def test_sentence_scaffold_think_and_repetition() -> None:
    open_body = _closed("窗口", 500) + "还没写完"
    assert "open_section" in validate_completion((open_body,) * 6)
    labeled = _closed("窗口", 500) + "\n要点：把提示再写一遍。\n"
    assert "scaffold" in validate_completion((labeled,) * 6)
    prose = _closed("窗口", 400) + "系统提示词通常包含固定的指令。抄大纲再加一个完字并不能说明写完了。"
    assert scaffold_hits(prose) == ()
    leaked = _closed("窗口", 500) + "\n<think>先分析一下</think>\n"
    assert has_think(leaked)
    assert "think" in validate_completion((leaked,) * 6)
    ending = "确保推理过程不因溢出而失败。"
    padded = _closed("余量", 450) + ending + ending
    assert "repeated" in validate_completion((padded,) * 6)
    existing = "建议预留缓冲。" + ending
    assert repeats_existing(existing, ending)
    assert not repeats_existing(existing, "另外把安全余量写成检查项，每次改 prompt 后都重算一次。")


def test_unresolved_anchor_blocks_completion() -> None:
    long = tuple(_closed(title, 500) for title in ("窗口", "估算", "余量", "截断", "核对", "做法"))
    assert validate_completion(long, anchor_lost_unresolved=True) == ("anchor_lost",)
    assert validate_completion(long, anchor_lost_unresolved=False) == ()
