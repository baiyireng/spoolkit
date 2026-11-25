"""章节事件：一节一条，重写只换这一节。"""

from spoolkit.agent.sections import apply_section_event, write_sections
from spoolkit.agent.writing_state import OutlineSection, WritingState
from spoolkit.llm.types import ChatRequest, ChatResponse

_FACTS = "工具结果：窗口是 8192。"


def _state() -> WritingState:
    return WritingState(
        goal="写清楚",
        outline=(
            OutlineSection("背景", "说明现状", 400),
            OutlineSection("方案", "给出做法", 400),
        ),
    )


class _Script:
    def __init__(self, texts: list[tuple[str, bool]]) -> None:
        self.requests: list[ChatRequest] = []
        self._texts = list(texts)

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        text, truncated = self._texts.pop(0)
        return ChatResponse(
            text=text, prompt_tokens=20, completion_tokens=10, truncated=truncated
        )


def test_逐节只发本节的开始和完成() -> None:
    gateway = _Script([("第一节正文", False), ("第二节正文", False)])
    events: list[tuple[str, dict]] = []
    drafts = write_sections(
        gateway, _state(), facts=_FACTS, on_event=lambda kind, data: events.append((kind, data))
    )
    assert [item.text for item in drafts] == ["第一节正文", "第二节正文"]
    assert [kind for kind, _ in events] == [
        "section_start",
        "section_done",
        "section_start",
        "section_done",
    ]
    assert events[1][1]["text"] == "第一节正文"
    assert events[1][1]["state"] == "final"
    assert events[0][1]["state"] == "draft"
    assert events[2][1]["index"] == 1
    assert "第一节正文" not in str(events[2][1])
    assert "第一节正文" not in str(events[3][1])


def test_重写本节只替换这一节的正文() -> None:
    parts: dict[int, str] = {}
    parts = apply_section_event(
        parts, "section_done", {"index": 0, "text": "第一节正文", "state": "final"}
    )
    parts = apply_section_event(
        parts, "section_done", {"index": 1, "text": "半截", "state": "final"}
    )
    parts = apply_section_event(
        parts, "section_done", {"index": 1, "text": "重写后的第二节", "state": "final"}
    )
    assert parts[0] == "第一节正文"
    assert parts[1] == "重写后的第二节"
    assert list(parts) == [0, 1]
