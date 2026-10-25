"""Context pack 只拼摘要和引用，按预算整节取舍，不读正文。"""

from pathlib import Path

from spoolkit.agent.artifacts import ArtifactHit, ArtifactRecord, ArtifactSection
from spoolkit.agent.session_summary import SCHEMA_VERSION, TEXT_CAP, SessionSummary, SummaryArtifact
from spoolkit.agent.context_pack import ContextPack, build_context_pack

SECRET = "sk-test-secret-value"
BODY = "只有正文里才有的句子甲。不该出现在 context pack 里。"


class LenCounter:
    def count(self, text: str) -> int:
        return len(text)


def _summary(**overrides: object) -> SessionSummary:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "session": "sess-a",
        "updated_at": "2026-10-23T00:00:00Z",
        "headline": "估算进行中",
        "what_done": ("写完大纲",),
        "artifacts": (
            SummaryArtifact(
                id="doc-2",
                title="另一份说明",
                summary="会话里的摘要。",
                path=".agent/artifacts/sess-a/def",
            ),
        ),
        "decisions": ("按三节展开",),
        "open_items": ("还没写结尾",),
        "last_turn_id": "turn-9",
    }
    payload.update(overrides)
    return SessionSummary(**payload)  # type: ignore[arg-type]


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
        "sections": (
            ArtifactSection(index=0, title="窗口", summary=BODY, char_count=len(BODY)),
        ),
    }
    payload.update(overrides)
    return ArtifactRecord(**payload)  # type: ignore[arg-type]


def _hit(record: ArtifactRecord | None = None, *, snippet: str = BODY) -> ArtifactHit:
    return ArtifactHit(
        record=record or _record(),
        score=600,
        matched=("title",),
        snippet=snippet,
    )


def _pack(**overrides: object) -> ContextPack:
    payload: dict[str, object] = {
        "goal": "写一份说明",
        "task_summary": "正在写估算",
        "summary": _summary(),
        "artifacts": (_hit(),),
        "token_budget": 100_000,
        "counter": None,
    }
    payload.update(overrides)
    return build_context_pack(**payload)  # type: ignore[arg-type]


def test_empty_goal_or_empty_summary_returns_empty_pack() -> None:
    blank = SessionSummary(schema_version=SCHEMA_VERSION, session="sess-a", updated_at="")
    for pack in (
        _pack(goal="  "),
        _pack(goal="", task_summary="正在写估算", summary=blank, artifacts=(_hit(),)),
        _pack(summary=blank, artifacts=(), task_summary=""),
    ):
        assert pack == ContextPack(goal="", sections=(), omitted=(), total_tokens=0)
        assert pack.text() == ""


def test_sections_follow_priority_and_repeat() -> None:
    first = _pack()
    second = _pack()
    assert first == second
    assert first.text() == second.text()
    assert [section.kind for section in first.sections] == [
        "task",
        "open",
        "decision",
        "artifact",
        "done",
        "meta",
    ]
    assert [section.title for section in first.sections] == [
        "当前任务",
        "未完成",
        "决定",
        "产物",
        "已完成",
        "最近一轮",
    ]
    text = first.text()
    positions = [text.index(f"## {section.title}") for section in first.sections]
    assert positions == sorted(positions)
    assert first.total_tokens == sum(section.tokens for section in first.sections)
    assert "title: 估算说明" in text
    assert "id: doc-1" in text
    assert "id: doc-2" in text
    assert BODY not in text


def test_budget_skips_whole_sections(tmp_path: Path) -> None:
    summary = _summary(
        headline="",
        open_items=("未" * TEXT_CAP,),
        decisions=("决定乙",),
        what_done=("写完大纲",),
    )
    full = _pack(
        task_summary="任务甲",
        summary=summary,
        artifacts=(_hit(),),
        token_budget=100_000,
        counter=LenCounter(),
    )
    by_kind = {section.kind: section.tokens for section in full.sections}
    assert list(by_kind) == ["task", "open", "decision", "artifact", "done", "meta"]
    budget = by_kind["task"] + by_kind["decision"]
    packed = _pack(
        task_summary="任务甲",
        summary=summary,
        artifacts=(_hit(),),
        token_budget=budget,
        counter=LenCounter(),
    )
    assert [section.kind for section in packed.sections] == ["task", "decision"]
    assert packed.omitted == ("open", "artifact", "done", "meta")
    assert len(packed.omitted) == 4
    assert packed.total_tokens == budget
    assert "未" * TEXT_CAP not in packed.text()
    assert "决定乙" in packed.text()
    assert "任务甲" in packed.text()
    blocked = tmp_path / "body.md"
    blocked.write_text(BODY, encoding="utf-8")
    untouched = blocked.read_text(encoding="utf-8")
    assert untouched == BODY
    assert BODY not in packed.text()


def test_duplicate_artifacts_collapse_to_one_ref() -> None:
    first = _hit()
    later = _hit(_record(title="后来的标题", summary="后来的摘要。"))
    catalog = _summary(
        artifacts=(
            SummaryArtifact(
                id="doc-1",
                title="摘要里的标题",
                summary="摘要里的说明。",
                path=".agent/artifacts/sess-a/other",
            ),
        )
    )
    pack = _pack(summary=catalog, artifacts=(first, later))
    text = pack.text()
    assert text.count("id: doc-1") == 1
    assert "title: 估算说明" in text
    assert "后来的标题" not in text
    assert "摘要里的标题" not in text
    assert BODY not in text


def test_secrets_are_redacted_and_body_stays_out() -> None:
    summary = _summary(open_items=(f"配置 API_KEY={SECRET} 之后继续",))
    record = _record(summary=f"见 API_KEY={SECRET} 。")
    pack = _pack(
        goal=f"目标 API_KEY={SECRET}",
        task_summary=f"任务 API_KEY={SECRET}",
        summary=summary,
        artifacts=(_hit(record),),
    )
    text = pack.text()
    assert SECRET not in text
    assert SECRET not in pack.goal
    assert "API_KEY=***" in text
    assert BODY not in text
    flat = " ".join(line for section in pack.sections for line in section.lines)
    assert SECRET not in flat
    assert "snippet" not in flat
