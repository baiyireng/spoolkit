"""artifact 检索只读元数据，摘要先脱敏，不返回正文。"""

from pathlib import Path

from spoolkit.agent.artifacts import (
    ArtifactHit,
    ArtifactRecord,
    ArtifactSection,
    ArtifactStore,
)

SECRET = "sk-test-secret-value"
BODY = "只有正文文件里的句子甲。不该出现在检索结果里。"


def _section(title: str, summary: str) -> ArtifactSection:
    return ArtifactSection(index=0, title=title, summary=summary, char_count=12)


def _put(
    store: ArtifactStore,
    *,
    artifact_id: str,
    session: str = "sess-a",
    title: str,
    summary: str,
    created_at: str,
    sections: tuple[ArtifactSection, ...] = (),
    content: str | None = None,
) -> ArtifactRecord:
    if content is None:
        content = f"{BODY}\n{artifact_id}"
    if not sections:
        sections = (_section("无关节", "无关节的摘要。"),)
    return store.put(
        ArtifactRecord(
            id=artifact_id,
            session=session,
            task_id="task-1",
            turn_id="turn-1",
            kind="document",
            title=title,
            summary=summary,
            created_at=created_at,
            sections=sections,
        ),
        content,
    )


def test_search_matches_title_summary_section_path_and_id(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    title_hit = _put(
        store,
        artifact_id="doc-title",
        title="Estimate Notes",
        summary="别的摘要。",
        created_at="2026-10-23T00:00:00Z",
    )
    summary_hit = _put(
        store,
        artifact_id="doc-summary",
        title="其他标题",
        summary="这里写了 rollout 的范围。",
        created_at="2026-10-23T01:00:00Z",
    )
    section_hit = _put(
        store,
        artifact_id="doc-section",
        title="另一份",
        summary="没有关键词。",
        created_at="2026-10-23T02:00:00Z",
        sections=(_section("窗口设计", "节内没有别的。"),),
    )
    by_title = store.search("sess-a", "estimate")
    assert [item.record.id for item in by_title] == ["doc-title"]
    assert by_title[0].matched == ("title",)
    assert by_title[0].snippet == "Estimate Notes"
    by_summary = store.search("sess-a", "rollout")
    assert [item.record.id for item in by_summary] == ["doc-summary"]
    assert by_summary[0].matched == ("summary",)
    by_section = store.search("sess-a", "窗口")
    assert [item.record.id for item in by_section] == ["doc-section"]
    assert by_section[0].matched == ("section.title",)
    by_id = store.search("sess-a", "doc-title")
    assert [item.record.id for item in by_id] == ["doc-title"]
    assert "id" in by_id[0].matched
    by_path = store.search("sess-a", title_hit.path.rsplit("/", 1)[-1])
    assert [item.record.id for item in by_path] == ["doc-title"]
    assert by_path[0].matched == ("path",)
    assert all(isinstance(item, ArtifactHit) for item in by_title)
    assert BODY not in by_title[0].snippet
    assert BODY not in by_title[0].record.summary


def test_search_ranks_title_then_summary_then_section(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    _put(store, artifact_id="doc-exact", title="预算", summary="无关。", created_at="2026-10-23T00:00:00Z")
    _put(store, artifact_id="doc-prefix", title="预算附录", summary="无关。", created_at="2026-10-23T03:00:00Z")
    _put(store, artifact_id="doc-sum", title="其他", summary="这里提到预算两字。", created_at="2026-10-23T02:00:00Z")
    _put(
        store,
        artifact_id="doc-sec",
        title="别的",
        summary="没有。",
        created_at="2026-10-23T04:00:00Z",
        sections=(_section("章节", "节内写了预算。"),),
    )
    _put(store, artifact_id="doc-new", title="预算甲", summary="无关。", created_at="2026-10-23T01:00:00Z")
    _put(store, artifact_id="doc-old", title="预算乙", summary="无关。", created_at="2026-10-23T00:30:00Z")
    ranked = store.search("sess-a", "预算")
    assert [item.record.id for item in ranked][:4] == ["doc-exact", "doc-prefix", "doc-new", "doc-old"]
    assert [item.record.id for item in ranked][4:] == ["doc-sum", "doc-sec"]
    assert ranked[0].score > ranked[1].score > ranked[4].score > ranked[5].score
    assert store.search("sess-a", "预算", limit=1) == (ranked[0],)
    assert store.search("sess-a", "没有这个词") == ()
    assert store.search("sess-a", "  ") == ()


def test_search_stays_in_session_and_hides_body_and_secrets(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    stored = _put(
        store,
        artifact_id="doc-secret",
        title="密钥说明",
        summary=f"配置 API_KEY={SECRET} 之后继续。",
        created_at="2026-10-23T00:00:00Z",
        content=f"{BODY} API_KEY={SECRET}",
    )
    _put(
        store,
        artifact_id="doc-other",
        session="sess-b",
        title="密钥说明",
        summary="另一会话。",
        created_at="2026-10-23T03:00:00Z",
        content="会话乙的正文。",
    )
    hits = store.search("sess-a", SECRET)
    assert len(hits) == 1
    assert hits[0].record.id == "doc-secret"
    assert hits[0].record.session == "sess-a"
    blob = " ".join(
        [
            hits[0].snippet,
            hits[0].record.title,
            hits[0].record.summary,
            hits[0].record.path,
            *(item.title + item.summary for item in hits[0].record.sections),
        ]
    )
    assert SECRET not in blob
    assert "API_KEY=***" in hits[0].snippet
    assert BODY not in blob
    assert store.search("sess-a", BODY) == ()
    assert store.search("sess-b", "密钥说明")[0].record.id == "doc-other"
    content = (tmp_path / stored.path).read_text(encoding="utf-8")
    assert SECRET in content
    assert BODY in content
