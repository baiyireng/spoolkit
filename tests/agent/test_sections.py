"""逐节只带大纲和事实，组装时标题齐全并去掉重复段。"""

from spoolkit.agent.sections import (
    AssemblyFailure,
    assemble,
    generate_section,
    write_sections,
)
from spoolkit.agent.writing_state import OutlineSection, WritingState
from spoolkit.llm.types import ChatRequest, ChatResponse

_TITLES = ("背景", "问题", "方案", "步骤", "风险")
_POINTS = ("说明现状", "指出缺口", "给出做法", "列出顺序", "写明代价")
_FACTS = "工具结果：窗口是 8192。"
_REPEAT = "窗口是 8192，余量要留给输出。"


def _state() -> WritingState:
    return WritingState(
        goal="把方案写清楚",
        outline=tuple(
            OutlineSection(title, points, 800)
            for title, points in zip(_TITLES, _POINTS, strict=True)
        ),
        decisions=("术语用「窗口」",),
        next_section=0,
    )


class _Script:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        text = self.texts[len(self.requests) - 1]
        used = sum(len(item.content) for item in request.messages)
        return ChatResponse(text=text, prompt_tokens=used, completion_tokens=20)


def test_本节请求只带大纲约束和事实() -> None:
    state = _state()
    state = WritingState(
        goal=state.goal,
        outline=state.outline,
        decisions=state.decisions,
        next_section=2,
    )
    previous = "第一节已经写完的正文。" * 40
    gateway = _Script(["本节正文"])
    draft = generate_section(gateway, state, facts=_FACTS)
    assert draft.title == "方案"
    assert draft.index == 2
    assert draft.text == "本节正文"
    request = gateway.requests[0]
    assert request.response_schema is None
    assert request.cache_prompt is True
    assert request.max_tokens == 800
    assert [item.role for item in request.messages] == ["system", "user"]
    assert request.messages[1].content == _FACTS
    system = request.messages[0].content
    assert "只写第 3 节「方案」" in system
    assert "给出做法" in system
    assert "术语用「窗口」" in system
    for title in _TITLES:
        assert title in system
    assert previous not in system
    assert previous not in request.messages[1].content


def test_五节提示词不随已写正文变长() -> None:
    bodies = [f"第{i}节正文。" + ("甲" * 800) for i in range(5)]
    gateway = _Script(bodies)
    drafts = write_sections(gateway, _state(), facts=_FACTS)
    assert len(drafts) == 5
    assert [item.text for item in drafts] == bodies
    lengths = [
        sum(len(message.content) for message in request.messages)
        for request in gateway.requests
    ]
    assert max(lengths) - min(lengths) < 200
    for index, request in enumerate(gateway.requests):
        blob = "\n".join(message.content for message in request.messages)
        assert request.messages[-1].content == _FACTS
        for earlier in bodies[:index]:
            assert earlier not in blob
        assert "甲" * 40 not in blob


def test_组装按顺序带上标题并去掉重复段() -> None:
    drafts = []
    for index, title in enumerate(_TITLES):
        body = f"这一节讲{title}。"
        if index == 0:
            body = f"{_REPEAT}\n\n{body}"
        if index == 3:
            body = f"{_REPEAT}\n\n{body}"
        drafts.append(_draft(index, title, body))
    # 打乱传入，组装仍按大纲顺序。
    shuffled = [drafts[4], drafts[0], drafts[2], drafts[1], drafts[3]]
    text = assemble(_state(), shuffled)
    assert isinstance(text, str)
    positions = [text.index(f"## {title}") for title in _TITLES]
    assert positions == sorted(positions)
    assert text.count(_REPEAT) == 1


def test_缺节时组装失败() -> None:
    drafts = [_draft(index, title, f"正文{index}") for index, title in enumerate(_TITLES[:-1])]
    failed = assemble(_state(), drafts)
    assert isinstance(failed, AssemblyFailure)
    assert "标题" in failed.reason


def _draft(index: int, title: str, text: str):
    from spoolkit.agent.sections import SectionDraft

    return SectionDraft(
        index=index,
        title=title,
        text=text,
        prompt_tokens=100,
        truncated=False,
    )
