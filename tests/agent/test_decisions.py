"""每节只收下 1–3 条短结论，去重后总数不超过 20。"""

from spoolkit.agent.sections import write_sections
from spoolkit.agent.writing_state import (
    OutlineSection,
    WritingState,
    merge_decisions,
    split_section_decisions,
)
from spoolkit.llm.types import ChatRequest, ChatResponse


def test_结论去重并卡住40字和20条() -> None:
    body, found = split_section_decisions(
        "这一节正文很长，不该进约束。\n\n决定：用窗口\n决定：余量留给输出\n决定：第三条\n决定：第四条丢掉"
    )
    assert body == "这一节正文很长，不该进约束。"
    assert found == ("用窗口", "余量留给输出", "第三条")
    merged = merge_decisions(("用窗口",), found)
    assert merged == ("用窗口", "余量留给输出", "第三条")
    long_line = "决" * 41
    assert merge_decisions((), (long_line, "短结论")) == ("短结论",)
    full = tuple(f"结论{i}" for i in range(20))
    assert merge_decisions(full, ("新结论",)) == full


def test_下一节能看见上一节的结论且正文不进提示() -> None:
    outline = tuple(
        OutlineSection(title, "要点", 400)
        for title in ("背景", "问题", "方案")
    )
    state = WritingState(goal="写清楚", outline=outline, decisions=("术语用窗口",))
    prose = "正文" * 80
    gateway = _Script(
        [
            f"{prose}\n决定：口径按 8192",
            "第二节正文",
            "第三节正文",
        ]
    )
    drafts = write_sections(gateway, state, facts="事实")
    assert drafts[0].text == prose
    assert "口径按 8192" in drafts[0].decisions
    assert prose not in drafts[0].decisions
    second = gateway.requests[1].messages[0].content
    assert "口径按 8192" in second
    assert prose not in second
    instruction = next(
        message.content
        for message in gateway.requests[0].messages
        if message.role == "user"
    )
    assert "决定：" in instruction


class _Script:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        text = self.texts[len(self.requests) - 1]
        return ChatResponse(text=text, prompt_tokens=10, completion_tokens=5)
