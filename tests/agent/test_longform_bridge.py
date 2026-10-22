"""终稿路径先跑 LongFormRunner，失败再回退原来的分节组装。"""

import json
from pathlib import Path

from spoolkit.agent.chat_sink import CollectingChatSink
from spoolkit.agent.document_bodies import InMemoryBodyStore
from spoolkit.agent.document_state import DocumentState, OutlineEntry
from spoolkit.agent.longform import LongFormRunner
from spoolkit.agent.longform_bridge import (
    CHARS_PER_TOKEN,
    build_document_state,
    fallback_document_state,
    render_document,
    target_chars_for,
)
from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.protocol import AnswerPlan
from spoolkit.config import Config
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatRequest, ChatResponse, Message
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


def _control(mode: str = "long") -> str:
    payload: dict[str, object] = {
        "thought": "这题要分节写",
        "tool_calls": [],
        "done": True,
        "summary": "无需外部信息。按三节展开。",
    }
    if mode:
        payload["answer_plan"] = {"mode": mode, "target_tokens": 4000, "sections": 3}
    return json.dumps(payload, ensure_ascii=False)


class _Gateway:
    def __init__(self, *, outline: str | None = None, mode: str = "long") -> None:
        self.requests: list[ChatRequest] = []
        self.outline = outline if outline is not None else _outline_text()
        self.mode = mode
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
            text = _body(title)
        elif "进入终稿阶段" in blob:
            text = "一句话回答。"
        else:
            text = _control(self.mode)
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


def _loop(tmp_path: Path, gateway: _Gateway, *, v2: bool, events: list) -> AgentLoop:
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


def test_outline_failure_falls_back_to_compose(tmp_path: Path) -> None:
    events: list = []
    gateway = _Gateway(outline="这里没有大纲")
    result = _loop(tmp_path, gateway, v2=True, events=events).run("写一份说明")
    assert "背景与目标" in result.final
    assert [kind for kind, _data in events].count("section_start") == 3
    assert _MARKER not in result.final


def test_short_answer_skips_longform(tmp_path: Path) -> None:
    events: list = []
    gateway = _Gateway(mode="")
    result = _loop(tmp_path, gateway, v2=True, events=events).run("今天天气如何")
    blobs = ["\n".join(item.content for item in request.messages) for request in gateway.requests]
    assert not any("不是控制层" in blob for blob in blobs)
    assert "一句话回答。" in result.final


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
