"""终稿路径先跑 LongFormRunner，失败再回退原来的分节组装。"""

import json
from pathlib import Path

import pytest

from spoolkit.agent.chat_sink import CollectingChatSink
from spoolkit.agent.document_bodies import InMemoryBodyStore
from spoolkit.agent.document_state import DocumentState, OutlineEntry
from spoolkit.agent.longform import LongFormRunner
from spoolkit.agent.longform_bridge import (
    CHARS_PER_TOKEN,
    LongformDraft,
    build_document_state,
    fallback_document_state,
    render_document,
    target_chars_for,
)
from spoolkit.agent.artifacts import ArtifactStore
from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.session_summary import SessionSummaryStore
from spoolkit.agent.protocol import AnswerPlan
from spoolkit.config import Config
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatRequest, ChatResponse, Message
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.registry import ToolRegistry

_TITLES = ("窗口", "估算", "余量")
_MARKER = "标记甲"


def _piece(title: str, chars: int = 1000) -> str:
    unit = f"{title}写完。"
    text = unit * ((chars // len(unit)) + 1)
    if len(text) > chars:
        text = text[: chars - 1] + "。"
    return text


def _body(title: str) -> str:
    return _MARKER + _piece(title, 1000)


def _outline_text() -> str:
    return "\n".join(f"{title} | {title}的要点 | 1000" for title in _TITLES)


def _control(mode: str = "long", *, deliverable: str = "", path: str = "") -> str:
    payload: dict[str, object] = {
        "thought": "这题要分节写",
        "tool_calls": [],
        "done": True,
        "summary": "无需外部信息。按三节展开。",
    }
    if mode:
        plan: dict[str, object] = {"mode": mode, "target_tokens": 4000, "sections": 3}
        if deliverable:
            plan["deliverable"] = deliverable
        if path:
            plan["path"] = path
        payload["answer_plan"] = plan
    return json.dumps(payload, ensure_ascii=False)


class _Gateway:
    def __init__(
        self,
        *,
        outline: str | None = None,
        mode: str = "long",
        deliverable: str = "",
        path: str = "",
        marker: str = _MARKER,
    ) -> None:
        self.requests: list[ChatRequest] = []
        self.outline = outline if outline is not None else _outline_text()
        self.mode = mode
        self.deliverable = deliverable
        self.path = path
        self.marker = marker
        self._tokenizer = OfflineTokenCounter()

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        blob = "\n".join(item.content for item in request.messages)
        if "不是控制层" in blob:
            text = self.outline
        elif "只输出最后一行" in blob:
            text = "完"
        elif "只补完" in blob or "未开始标题" in blob or "重写当前节" in blob:
            title = next((item for item in _TITLES if item in blob), _TITLES[0])
            text = self.marker + _piece(title, 1000)
        elif "进入终稿阶段" in blob:
            text = "一句话回答。"
        else:
            text = _control(self.mode, deliverable=self.deliverable, path=self.path)
        return ChatResponse(
            text=text,
            prompt_tokens=sum(self._tokenizer.count(item.content) for item in request.messages),
            completion_tokens=self._tokenizer.count(text),
        )

    def chat_stream(self, request: ChatRequest, on_delta=None) -> ChatResponse:
        response = self.chat(request)
        if on_delta is not None and response.text:
            on_delta(response.text)
        return response


class _Pending(PendingChanges):
    def __init__(self, root: Path) -> None:
        super().__init__(root, overrides={}, run_id="run-1")
        self.apply_calls = 0

    def apply(self) -> list[str]:
        self.apply_calls += 1
        raise AssertionError("不应落盘")


def _loop(
    tmp_path: Path,
    gateway: _Gateway,
    *,
    v2: bool,
    events: list,
    pending: PendingChanges | None = None,
) -> AgentLoop:
    return AgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        tokenizer=OfflineTokenCounter(),
        registry=ToolRegistry(),
        config=Config(
            project_root=tmp_path,
            context_window=32768,
            max_steps=2,
            supervise=False,
            overrides={"protocol_v2": 1 if v2 else 0},
        ),
        on_event=lambda kind, data: events.append((kind, dict(data))),
        pending=pending,
    )


def test_target_tokens_become_chars_one_for_one() -> None:
    assert CHARS_PER_TOKEN == 1
    assert target_chars_for(800) == 800
    assert target_chars_for(1) == 1


def test_build_document_state_from_outline() -> None:
    gateway = _Gateway()
    state = build_document_state(
        "写一份估算说明",
        AnswerPlan(mode="long", target_tokens=4000, sections=3),
        (Message(role="user", content="写一份估算说明"),),
        gateway,
        token_budget=8000,
    )
    assert state is not None
    assert state.phase == "plan"
    assert state.deliverable == "chat"
    assert state.current_section is None
    assert state.pending_sections == (0, 1, 2)
    assert [item.title for item in state.outline] == list(_TITLES)
    assert [item.points for item in state.outline] == [f"{title}的要点" for title in _TITLES]
    assert [item.target_chars for item in state.outline] == [1000, 1000, 1000]


def test_build_file_state_keeps_path_and_char_rule() -> None:
    gateway = _Gateway()
    state = build_document_state(
        "写一份估算说明",
        AnswerPlan(mode="long", target_tokens=4000, sections=3, deliverable="file", path="docs/out.md"),
        (Message(role="user", content="写一份估算说明"),),
        gateway,
        token_budget=8000,
    )
    assert state is not None
    assert state.deliverable == "file"
    assert state.path == "docs/out.md"
    assert [item.target_chars for item in state.outline] == [1000, 1000, 1000]
    assert build_document_state(
        "写一份估算说明",
        AnswerPlan(mode="long", target_tokens=4000, deliverable="file", path=""),
        (Message(role="user", content="写一份估算说明"),),
        gateway,
        token_budget=8000,
    ) is None
    assert build_document_state(
        "写一份估算说明",
        AnswerPlan(mode="long", target_tokens=4000, deliverable="file", path="../out.md"),
        (Message(role="user", content="写一份估算说明"),),
        gateway,
        token_budget=8000,
    ) is None


def test_both_deliverable_is_not_supported_and_stays_chat() -> None:
    gateway = _Gateway()
    state = build_document_state(
        "写一份估算说明",
        AnswerPlan(mode="long", target_tokens=4000, deliverable="both", path="docs/out.md"),
        (Message(role="user", content="写一份估算说明"),),
        gateway,
        token_budget=8000,
    )
    assert state is not None
    assert state.deliverable == "chat"
    assert state.path == ""


def _seven_outline() -> str:
    return "\n".join(f"节{index} | 要点{index} | 100" for index in range(7))


def test_plan三节时七节大纲重试后仍不进入写作() -> None:
    gateway = _Gateway(outline=_seven_outline())
    state = build_document_state(
        "写一份估算说明",
        AnswerPlan(mode="long", target_tokens=4000, sections=3),
        (Message(role="user", content="写一份估算说明"),),
        gateway,
        token_budget=8000,
    )
    assert state is None
    assert len(gateway.requests) == 2
    blobs = ["\n".join(item.content for item in request.messages) for request in gateway.requests]
    assert all("不是控制层" in blob for blob in blobs)
    assert all("必须正好 3 节" in blob for blob in blobs)
    assert "作废" in blobs[1]
    assert not any("未开始标题" in blob or "只补完" in blob for blob in blobs)


def test_目标写明三节时七节大纲不进入写作() -> None:
    gateway = _Gateway(outline=_seven_outline())
    state = build_document_state(
        "写一份三节中文估算说明",
        AnswerPlan(mode="long", target_tokens=4000),
        (Message(role="user", content="写一份三节中文估算说明"),),
        gateway,
        token_budget=8000,
    )
    assert state is None
    assert len(gateway.requests) == 2
    assert "必须正好 3 节" in "\n".join(item.content for item in gateway.requests[1].messages)


class _RecountOutline(_Gateway):
    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        blob = "\n".join(item.content for item in request.messages)
        text = self.outline if blob.count("必须正好 3 节") == 1 else _outline_text()
        if "作废" in blob:
            text = _outline_text()
        return ChatResponse(text=text, prompt_tokens=10, completion_tokens=4)


def test_七节大纲重规划成三节后才收下() -> None:
    gateway = _RecountOutline(outline=_seven_outline())
    state = build_document_state(
        "写一份说明",
        AnswerPlan(mode="long", target_tokens=4000, sections=3),
        (Message(role="user", content="写一份说明"),),
        gateway,
        token_budget=8000,
    )
    assert state is not None
    assert len(state.outline) == 3
    assert len(gateway.requests) == 2


def test_build_document_state_returns_none_when_outline_fails() -> None:
    gateway = _Gateway(outline="这里没有大纲")
    state = build_document_state(
        "写一份估算说明",
        None,
        (Message(role="user", content="写一份估算说明"),),
        gateway,
        token_budget=8000,
    )
    assert state is None


def test_fallback_outline_uses_the_same_char_rule() -> None:
    state = fallback_document_state("写一份估算说明", 1200)
    assert state is not None
    assert len(state.outline) == 3
    assert [item.target_chars for item in state.outline] == [300, 600, 300]


class _Queue:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    def chat(self, request: ChatRequest) -> ChatResponse:
        text = self._replies.pop(0)
        return ChatResponse(text=text, prompt_tokens=10, completion_tokens=len(text))

    def context_window(self) -> int:
        return 8192


def test_runner_collects_three_sections_in_order() -> None:
    outline = tuple(
        OutlineEntry(index=index, title=title, points=f"{title}的要点", target_chars=1000)
        for index, title in enumerate(_TITLES)
    )
    state = DocumentState(
        goal="写一份估算说明",
        outline=outline,
        deliverable="chat",
        phase="plan",
        pending_sections=(0, 1, 2),
    )
    sink = CollectingChatSink()
    store = InMemoryBodyStore()
    result = LongFormRunner(
        state,
        _Queue([_body(title) for title in _TITLES] + ["完"]),
        store,
        sink,
    ).run()
    assert result.done
    text = sink.text()
    assert text.index("## 窗口") < text.index("## 估算") < text.index("## 余量")
    assert text == render_document(state, store)
    replaced = CollectingChatSink()
    replaced.section_start(0, "窗口")
    replaced.section_commit(0, "旧稿。")
    replaced.section_replace(0, _body("窗口"))
    replaced.section_start(1, "估算")
    replaced.section_commit(1, _body("估算"))
    replaced.complete(result)
    assert replaced.text().index(_body("窗口")) < replaced.text().index(_body("估算"))
    assert "旧稿。" not in replaced.text()


def test_v2_long_mode_uses_longform_runner(tmp_path: Path) -> None:
    events: list = []
    gateway = _Gateway()
    result = _loop(tmp_path, gateway, v2=True, events=events).run("写一份说明")
    assert result.finished is True
    assert result.final.index("## 窗口") < result.final.index("## 估算") < result.final.index("## 余量")
    assert _MARKER in result.final
    blobs = ["\n".join(item.content for item in request.messages) for request in gateway.requests]
    assert any("不是控制层" in blob for blob in blobs)
    assert not any("只写第" in blob for blob in blobs)
    summaries = [
        data.get("summary", "")
        for kind, data in events
        if kind == "task_event" and data.get("phase") == "final"
    ]
    assert summaries == ["长文完成：3 节 / 3009 字"]
    assert all(data.get("source") == "system" and data.get("status") == "ok" for kind, data in events if kind == "task_event" and data.get("phase") == "final")
    assert all(_MARKER not in str(data.get("summary", "")) for _kind, data in events if _kind == "task_event")
    assert not any(kind == "section_start" for kind, _data in events)


def test_run把resume_checkpoint交给长文且不把正文放进事件(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, str] = {}

    def spy(**kwargs):
        seen["key"] = str(kwargs.get("resume_checkpoint") or "")
        return LongformDraft(text="续写完成", sections=2, chars=4)

    monkeypatch.setattr("spoolkit.agent.loop.try_longform_text", spy)
    events: list = []
    pending = _Pending(tmp_path)
    pending.run_session = "sess-a"
    result = _loop(tmp_path, _Gateway(), v2=True, events=events, pending=pending).run(
        "写一份说明",
        resume_checkpoint="ck-abc",
    )
    assert seen["key"] == "ck-abc"
    assert result.final == "续写完成"
    blob = json.dumps(events, ensure_ascii=False)
    assert "bodies" not in blob
    assert "CHECKPOINT-BODY" not in blob


def test_chat_longform_captures_artifact(tmp_path: Path) -> None:
    events: list = []
    pending = _Pending(tmp_path)
    pending.run_id = "run-9"
    pending.run_session = "sess-chat"
    result = _loop(tmp_path, _Gateway(), v2=True, events=events, pending=pending).run("写一份说明")
    assert _MARKER in result.final
    finals = _final_events(events)
    assert finals[0]["summary"] == "长文完成：3 节 / 3009 字"
    assert _MARKER not in finals[0]["summary"]
    artifact_id = finals[0]["artifacts"][0]["ref"]
    assert finals[0]["artifacts"][0]["path"].startswith(".agent/artifacts/sess-chat/")
    root = tmp_path / ".agent" / "artifacts"
    assert (root / "sess-chat").is_dir()
    assert not (root / "run-9").exists()
    metas = list(root.rglob("*.json"))
    assert len(metas) == 1
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    assert meta["id"] == artifact_id
    assert meta["session"] == "sess-chat"
    content = (tmp_path / str(meta["path"])).read_text(encoding="utf-8")
    blob = json.dumps(meta, ensure_ascii=False)
    assert content not in blob
    assert _MARKER in content
    assert all(item["summary"].rstrip("…") in content and item["summary"] != item["title"] for item in meta["sections"])


def _final_events(events: list) -> list[dict]:
    return [data for kind, data in events if kind == "task_event" and data.get("phase") == "final"]


def test_file_deliverable_stages_pending_and_summarizes(tmp_path: Path) -> None:
    events: list = []
    pending = _Pending(tmp_path)
    gateway = _Gateway(deliverable="file", path="docs/out.md")
    result = _loop(tmp_path, gateway, v2=True, events=events, pending=pending).run("写一份说明")
    target = tmp_path / "docs" / "out.md"
    assert result.final == "已生成文档，待确认：docs/out.md"
    assert _MARKER not in result.final
    assert not target.exists()
    assert pending.apply_calls == 0
    assert len(pending.items()) == 1
    assert pending.items()[0].path == "docs/out.md"
    assert _MARKER in pending.items()[0].new_text
    finals = _final_events(events)
    assert finals[0]["summary"] == "已生成文档，待确认：docs/out.md"
    assert _MARKER not in finals[0]["summary"]
    assert finals[0]["artifacts"][0]["path"] == "docs/out.md"
    artifact_id = finals[0]["artifacts"][0]["ref"]
    metas = list((tmp_path / ".agent" / "artifacts").rglob("*.json"))
    assert len(metas) == 1
    assert metas[0].name == f"{artifact_id}.json"
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    blob = json.dumps(meta, ensure_ascii=False)
    content = (tmp_path / str(meta["path"])).read_text(encoding="utf-8")
    assert content not in blob
    assert _MARKER in content
    assert all(
        item["summary"] != item["title"] and item["summary"].rstrip("…") in content
        for item in meta["sections"]
    )


def test_artifact_directory_follows_run_session(tmp_path: Path) -> None:
    from spoolkit.agent.artifacts import ArtifactStore

    first = _Pending(tmp_path)
    first.run_id = "run-9"
    first.run_session = "sess-chat"
    _loop(
        tmp_path,
        _Gateway(deliverable="file", path="docs/out.md"),
        v2=True,
        events=[],
        pending=first,
    ).run("写一份说明")
    second = _Pending(tmp_path)
    second.run_id = "run-10"
    second.run_session = "sess-chat"
    _loop(
        tmp_path,
        _Gateway(deliverable="file", path="docs/other.md", marker="标记乙"),
        v2=True,
        events=[],
        pending=second,
    ).run("写一份说明")
    root = tmp_path / ".agent" / "artifacts"
    assert (root / "sess-chat").is_dir()
    assert not (root / "run-9").exists()
    assert not (root / "run-10").exists()
    listed = ArtifactStore(tmp_path).list("sess-chat", limit=10)
    assert len(listed) == 2
    assert {item.session for item in listed} == {"sess-chat"}
    assert all(item.path.startswith(".agent/artifacts/sess-chat/") for item in listed)


def test_empty_run_session_falls_back_to_session_then_run_id(tmp_path: Path) -> None:
    pending = _Pending(tmp_path)
    pending.run_id = "run-9"
    pending.run_session = ""
    pending.session = "sess-fallback"
    _loop(
        tmp_path,
        _Gateway(deliverable="file", path="docs/out.md"),
        v2=True,
        events=[],
        pending=pending,
    ).run("写一份说明")
    root = tmp_path / ".agent" / "artifacts"
    assert (root / "sess-fallback").is_dir()
    assert not (root / "run-9").exists()

    only_run = _Pending(tmp_path)
    only_run.run_id = "run-11"
    only_run.run_session = "   "
    _loop(
        tmp_path,
        _Gateway(deliverable="file", path="docs/third.md", marker="标记丙"),
        v2=True,
        events=[],
        pending=only_run,
    ).run("写一份说明")
    assert (root / "run-11").is_dir()


def test_missing_or_escaping_path_does_not_pending(tmp_path: Path) -> None:
    for path in ("", "../out.md", "C:/tmp/out.md"):
        events: list = []
        pending = _Pending(tmp_path)
        gateway = _Gateway(deliverable="file", path=path)
        result = _loop(tmp_path, gateway, v2=True, events=events, pending=pending).run("写一份说明")
        assert pending.items() == []
        assert pending.apply_calls == 0
        assert "已生成文档" not in result.final
        assert not (tmp_path / "docs" / "out.md").exists()


def test_both_falls_back_to_chat_without_pending(tmp_path: Path) -> None:
    events: list = []
    pending = _Pending(tmp_path)
    gateway = _Gateway(deliverable="both", path="docs/out.md")
    result = _loop(tmp_path, gateway, v2=True, events=events, pending=pending).run("写一份说明")
    assert result.final.index("## 窗口") < result.final.index("## 估算")
    assert _MARKER in result.final
    assert "已生成文档" not in result.final
    assert pending.items() == []
    assert not (tmp_path / ".agent" / "artifacts").exists()
    assert _final_events(events)[0]["summary"] == "长文完成：3 节 / 3009 字"
    assert _final_events(events)[0]["artifacts"] == []


def test_outline_failure_falls_back_to_compose(tmp_path: Path) -> None:
    events: list = []
    gateway = _Gateway(outline="这里没有大纲")
    result = _loop(tmp_path, gateway, v2=True, events=events).run("写一份说明")
    assert "背景与目标" in result.final
    assert [kind for kind, _data in events].count("section_start") == 3
    assert _MARKER not in result.final
    assert not (tmp_path / ".agent" / "artifacts").exists()


def test_short_answer_skips_longform(tmp_path: Path) -> None:
    events: list = []
    gateway = _Gateway(mode="")
    result = _loop(tmp_path, gateway, v2=True, events=events).run("今天天气如何")
    blobs = ["\n".join(item.content for item in request.messages) for request in gateway.requests]
    assert not any("不是控制层" in blob for blob in blobs)
    assert "一句话回答。" in result.final
    assert not (tmp_path / ".agent" / "artifacts").exists()


def test_旧协议不从目标推断文件(tmp_path: Path) -> None:
    pending = _Pending(tmp_path)
    gateway = _Gateway(mode="")

    def chat(request: ChatRequest) -> ChatResponse:
        gateway.requests.append(request)
        text = json.dumps(
            {
                "thought": "短答即可",
                "tool_calls": [],
                "state": None,
                "done": True,
                "final": "写完了",
            },
            ensure_ascii=False,
        )
        return ChatResponse(text=text, prompt_tokens=10, completion_tokens=4)

    gateway.chat = chat  # type: ignore[method-assign]
    result = _loop(tmp_path, gateway, v2=False, events=[], pending=pending).run("把说明写到 docs/out.md")
    assert pending.items() == []
    assert "写完了" in result.final
    assert not (tmp_path / "docs" / "out.md").exists()


def test_legacy_protocol_skips_longform(tmp_path: Path) -> None:
    events: list = []
    gateway = _Gateway()

    def chat(request: ChatRequest) -> ChatResponse:
        gateway.requests.append(request)
        text = json.dumps(
            {
                "thought": "短答即可",
                "tool_calls": [],
                "state": None,
                "done": True,
                "final": "写完了",
            },
            ensure_ascii=False,
        )
        return ChatResponse(text=text, prompt_tokens=10, completion_tokens=4)

    gateway.chat = chat  # type: ignore[method-assign]
    result = _loop(tmp_path, gateway, v2=False, events=events).run("写一份详细报告")
    assert len(gateway.requests) == 1
    assert "写完了" in result.final
    assert not any("不是控制层" in "\n".join(item.content for item in request.messages) for request in gateway.requests)


def _summary_text(tmp_path: Path, session: str) -> str:
    path = tmp_path / ".agent" / "sessions" / session / "summary.json"
    return path.read_text(encoding="utf-8")


def test_chat_success_records_summary_for_the_next_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reads = {"n": 0}
    original = ArtifactStore.get

    def counted(self, *args, **kwargs):
        reads["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ArtifactStore, "get", counted)
    events: list = []
    pending = _Pending(tmp_path)
    pending.run_session = "sess-chat"
    loop = _loop(tmp_path, _Gateway(), v2=True, events=events, pending=pending)
    result = loop.run("写一份说明")
    summary = SessionSummaryStore(tmp_path, "sess-chat").load()
    assert summary.headline == "写一份说明"
    assert summary.what_done
    assert summary.what_done[0].startswith("## 窗口")
    assert summary.open_items == ()
    assert len(summary.artifacts) == 1
    assert summary.artifacts[0].id == _final_events(events)[0]["artifacts"][0]["ref"]
    assert summary.artifacts[0].title == "窗口"
    blob = _summary_text(tmp_path, "sess-chat")
    content = (tmp_path / summary.artifacts[0].path).read_text(encoding="utf-8")
    assert content not in blob
    assert reads["n"] == 0
    assert _final_events(events)[0]["summary"] == "长文完成：3 节 / 3009 字"
    assert "【会话记忆】" not in json.dumps(events, ensure_ascii=False)
    loop._record_session_outcome(result, "写一份说明")
    again = SessionSummaryStore(tmp_path, "sess-chat").load()
    assert len(again.what_done) == 1
    assert len(again.artifacts) == 1
    follow = _loop(tmp_path, _Gateway(mode=""), v2=True, events=[], pending=pending)
    follow.run("今天天气如何")
    prompt = "\n".join(item.content for item in follow.gateway.requests[0].messages)
    assert "【会话记忆】" in prompt
    assert "窗口" in prompt


def test_file_success_records_artifact_without_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reads = {"n": 0}
    original = ArtifactStore.get

    def counted(self, *args, **kwargs):
        reads["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ArtifactStore, "get", counted)
    events: list = []
    pending = _Pending(tmp_path)
    pending.run_session = "sess-file"
    _loop(
        tmp_path,
        _Gateway(deliverable="file", path="docs/out.md"),
        v2=True,
        events=events,
        pending=pending,
    ).run("写一份说明")
    summary = SessionSummaryStore(tmp_path, "sess-file").load()
    assert summary.what_done == ("已生成文档，待确认：docs/out.md",)
    assert summary.open_items == ()
    assert summary.artifacts[0].id == _final_events(events)[0]["artifacts"][0]["ref"]
    blob = _summary_text(tmp_path, "sess-file")
    assert _MARKER not in blob
    assert reads["n"] == 0
    follow = _loop(tmp_path, _Gateway(mode=""), v2=True, events=[], pending=pending)
    follow.run("今天天气如何")
    prompt = "\n".join(item.content for item in follow.gateway.requests[0].messages)
    assert "【会话记忆】" in prompt
    assert "已生成文档，待确认：docs/out.md" in prompt


def _run_goal(tmp_path: Path, goal: str, gateway: _Gateway | None = None) -> tuple[object, _Pending]:
    pending = _Pending(tmp_path)
    result = _loop(tmp_path, gateway or _Gateway(mode=""), v2=True, events=[], pending=pending).run(goal)
    return result, pending


def test_目标写到相对路径时登记待确认(tmp_path: Path) -> None:
    result, pending = _run_goal(tmp_path, "把说明写到 docs/out.md")
    assert result.final == "已生成文档，待确认：docs/out.md"
    assert len(pending.items()) == 1
    assert pending.items()[0].path == "docs/out.md"
    assert not (tmp_path / "docs" / "out.md").exists()


def test_生成报告的相对路径也走文件(tmp_path: Path) -> None:
    result, pending = _run_goal(tmp_path, "生成 docs/a.md 的报告")
    assert result.final == "已生成文档，待确认：docs/a.md"
    assert [item.path for item in pending.items()] == ["docs/a.md"]
    assert not (tmp_path / "docs" / "a.md").exists()


def test_没有路径的继续写仍是聊天(tmp_path: Path) -> None:
    gateway = _Gateway(mode="")
    result, pending = _run_goal(tmp_path, "继续写", gateway)
    blobs = ["\n".join(item.content for item in request.messages) for request in gateway.requests]
    assert pending.items() == []
    assert "已生成文档" not in (result.final or "")
    assert not any("不是控制层" in blob for blob in blobs)


def test_绝对路径和上级目录按聊天写(tmp_path: Path) -> None:
    for goal in ("把报告写到 C:/outside/out.md", "把报告写到 ../out.md", "把报告写到 /tmp/out.md"):
        result, pending = _run_goal(tmp_path, goal)
        assert pending.items() == []
        assert "已生成文档，待确认" not in (result.final or "")
        assert "## 窗口" in (result.final or "")


def test_已有计划时不用目标里的路径覆盖(tmp_path: Path) -> None:
    result, pending = _run_goal(
        tmp_path,
        "把说明写到 docs/other.md",
        _Gateway(deliverable="file", path="docs/kept.md"),
    )
    assert result.final == "已生成文档，待确认：docs/kept.md"
    assert [item.path for item in pending.items()] == ["docs/kept.md"]


def test_已有聊天计划时不改成文件(tmp_path: Path) -> None:
    result, pending = _run_goal(tmp_path, "把报告写到 docs/out.md", _Gateway(mode="long"))
    assert pending.items() == []
    assert "## 窗口" in (result.final or "")
    assert "已生成文档，待确认" not in (result.final or "")


def test_工作区拒绝的路径不提取(tmp_path: Path) -> None:
    from spoolkit.agent.longform_bridge import explicit_file_path
    from spoolkit.errors import PathOutsideProjectError

    pending = _Pending(tmp_path)

    def deny(candidate: str) -> str:
        raise PathOutsideProjectError(candidate)

    pending.path_of = deny  # type: ignore[method-assign]
    assert explicit_file_path("把说明写到 docs/out.md", pending) is None
    assert explicit_file_path("把说明写到 docs/out.md", _Pending(tmp_path)) == "docs/out.md"
    assert explicit_file_path("继续写", _Pending(tmp_path)) is None
    assert explicit_file_path("阅读 docs/out.md", _Pending(tmp_path)) is None
