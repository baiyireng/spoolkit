"""旧文档只提供摘录。重组写出新 artifact，不覆盖旧文件。"""

from pathlib import Path

import pytest

from spoolkit.agent.artifacts import ArtifactRecord, ArtifactStore
from spoolkit.agent.document_bodies import InMemoryBodyStore
from spoolkit.agent.document_source import (
    EXCERPT_CAP,
    SourceContext,
    load_document_source,
    recompose_document,
    source_context,
)
from spoolkit.agent.document_state import DocumentState, OutlineEntry
from spoolkit.agent.longform import (
    LongFormRunner,
    finish_messages,
    repair_messages,
    rewrite_messages,
    write_pending_messages,
)
from spoolkit.llm.types import ChatRequest, ChatResponse
from spoolkit.tools.edit import PendingChanges

TAIL = "尾巴标记甲"
TITLES = ("窗口", "估算", "余量")


class ScriptedGateway:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        text = self._replies.pop(0)
        return ChatResponse(text=text, prompt_tokens=1, completion_tokens=len(text))

    def context_window(self) -> int:
        return 8192


def _piece(title: str, chars: int = 1000) -> str:
    unit = f"{title}写完。"
    text = unit * ((chars // len(unit)) + 1)
    if len(text) > chars:
        text = text[: chars - 1] + "。"
    return text


def _old_body(title: str) -> str:
    return ("甲" * 420) + f"{TAIL}{title}"


def _markdown() -> str:
    return "\n\n".join(f"## {title}\n\n{_old_body(title)}" for title in TITLES)


def _store_old(tmp_path: Path) -> tuple[ArtifactStore, str]:
    store = ArtifactStore(tmp_path)
    content = _markdown()
    record = store.put(
        ArtifactRecord(
            id="doc-old",
            session="sess-a",
            task_id="task-old",
            turn_id="turn-old",
            kind="document",
            title="旧估算",
            summary="旧文档摘要。",
            created_at="2026-10-23T00:00:00Z",
        ),
        content,
    )
    return store, record.path


def _blob(request: ChatRequest) -> str:
    return "\n".join(item.content for item in request.messages)


def test_source_context_clips_excerpt_and_misses_unknown_index(tmp_path: Path) -> None:
    store, _path = _store_old(tmp_path)
    source = load_document_source(store, "sess-a", "doc-old")
    found = source_context(source, 1)
    assert isinstance(found, SourceContext)
    assert found is not None
    assert found.index == 1
    assert found.title == "估算"
    assert found.summary == source.sections[1].summary
    assert len(found.excerpt) <= EXCERPT_CAP
    assert found.excerpt == source.sections[1].body[:EXCERPT_CAP]
    assert TAIL not in found.excerpt
    assert found.excerpt != source.sections[1].body
    assert not hasattr(found, "body")
    assert source_context(source, 9) is None


def test_prompts_include_source_material_only_when_present() -> None:
    material = SourceContext(index=0, title="窗口", summary="窗口摘要。", excerpt="窗口摘录。")
    for messages in (
        finish_messages("窗口", 1, "总需求为", material),
        repair_messages(title="窗口", points="要点", target_chars=500, body="半截", current=True, source=material),
        rewrite_messages(title="窗口", points="要点", previous_summary="上一节", target_chars=500, source=material),
        write_pending_messages(title="窗口", points="要点", target_chars=500, summaries="（还没有）", source=material),
    ):
        text = "\n".join(item.content for item in messages)
        assert "旧节标题：窗口" in text
        assert "旧节摘要：窗口摘要。" in text
        assert "旧节摘录：窗口摘录。" in text
        assert "只作为素材，不要照抄整段" in text
    plain = "\n".join(item.content for item in finish_messages("窗口", 1, "总需求为"))
    assert "旧节" not in plain
    assert "不要写「完」" in plain


def test_recompose_three_sections_keeps_old_artifact(tmp_path: Path) -> None:
    store, relative = _store_old(tmp_path)
    content_path = tmp_path / relative
    meta_path = tmp_path / ".agent" / "artifacts" / "sess-a" / "doc-old.json"
    before_body = content_path.read_bytes()
    before_meta = meta_path.read_bytes()
    source = load_document_source(store, "sess-a", "doc-old")
    gateway = ScriptedGateway([_piece(title) for title in TITLES] + ["完"])
    draft = recompose_document(
        source,
        new_goal="整理成新说明",
        deliverable="chat",
        gateway=gateway,
        root=tmp_path,
        session="sess-a",
        task_id="task-new",
        turn_id="turn-new",
    )
    assert draft.state.source_artifacts == ("doc-old",)
    assert draft.artifact_id != "doc-old"
    assert "窗口写完。" in draft.text
    assert TAIL not in draft.text
    blobs = [_blob(request) for request in gateway.requests[:3]]
    assert [title for title in TITLES] == ["窗口", "估算", "余量"]
    for title, blob in zip(TITLES, blobs, strict=True):
        assert f"旧节标题：{title}" in blob
        context = source_context(source, TITLES.index(title))
        assert context is not None
        assert context.summary in blob
        assert context.excerpt in blob
        assert f"{TAIL}{title}" not in blob
        assert "只作为素材，不要照抄整段" in blob
    assert all(f"{TAIL}{title}" not in _blob(request) for request in gateway.requests for title in TITLES)
    assert content_path.read_bytes() == before_body
    assert meta_path.read_bytes() == before_meta
    _record, stored = store.get("sess-a", draft.artifact_id)
    assert stored == draft.text
    assert store.get("sess-a", "doc-old")[1] == _markdown()


def test_file_recompose_proposes_a_new_artifact(tmp_path: Path) -> None:
    store, relative = _store_old(tmp_path)
    before_body = (tmp_path / relative).read_bytes()
    target = tmp_path / "docs" / "next.md"
    target.parent.mkdir(parents=True)
    target.write_text("旧稿。\n", encoding="utf-8")
    before_target = target.read_bytes()
    source = load_document_source(store, "sess-a", "doc-old")
    pending = PendingChanges(tmp_path, overrides={}, run_id="run-1")
    gateway = ScriptedGateway([_piece(title) for title in TITLES] + ["完"])
    draft = recompose_document(
        source,
        new_goal="整理成文件",
        deliverable="file",
        path="docs/next.md",
        gateway=gateway,
        root=tmp_path,
        pending=pending,
        session="sess-a",
        task_id="task-file",
        turn_id="turn-file",
    )
    assert draft.artifact_id != "doc-old"
    assert draft.state.source_artifacts == ("doc-old",)
    assert draft.path == "docs/next.md"
    assert target.read_bytes() == before_target
    assert len(pending.items()) == 1
    change = pending.items()[0]
    assert change.path == "docs/next.md"
    assert TAIL not in change.new_text
    _record, stored = store.get("sess-a", draft.artifact_id)
    assert stored == change.new_text
    assert (tmp_path / relative).read_bytes() == before_body
    assert store.get("sess-a", "doc-old")[1] == _markdown()


def test_runner_without_source_omits_old_section_material() -> None:
    outline = tuple(
        OutlineEntry(index=index, title=title, points=f"{title}的要点", target_chars=500)
        for index, title in enumerate(TITLES)
    )
    state = DocumentState(
        goal="写一份估算说明",
        outline=outline,
        deliverable="chat",
        phase="plan",
        pending_sections=(0, 1, 2),
    )
    gateway = ScriptedGateway([_piece(title) for title in TITLES] + ["完"])
    result = LongFormRunner(state, gateway, InMemoryBodyStore()).run()
    assert result.done
    assert result.ending == "完"
    assert all("旧节" not in _blob(request) for request in gateway.requests)
