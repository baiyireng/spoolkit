"""终稿纯文本流式：直接上屏，截断后续写只追加。"""

from spoolkit.agent.final_turn import request_final
from spoolkit.llm.types import ChatRequest, ChatResponse, Message


class _Stream:
    def __init__(self, rounds: list[tuple[list[str], bool]]) -> None:
        self._rounds = list(rounds)
        self.calls = 0

    def chat_stream(
        self, request: ChatRequest, on_delta=None
    ) -> ChatResponse:
        chunks, truncated = self._rounds.pop(0)
        self.calls += 1
        if on_delta is not None:
            for chunk in chunks:
                on_delta(chunk)
        return ChatResponse(
            text="".join(chunks),
            prompt_tokens=1000 + self.calls,
            completion_tokens=len(chunks),
            truncated=truncated,
        )


def test_纯文本换行原样进事件且不是字面转义() -> None:
    events: list[str] = []
    gateway = _Stream([(["# 标题", "\n", "正文"], False)])
    draft = request_final(
        gateway,
        (Message(role="user", content="写"),),
        max_tokens=200,
        on_delta=events.append,
    )
    assert events == ["# 标题", "\n", "正文"]
    assert "".join(events) == "# 标题\n正文"
    assert "\\n" not in "".join(events)
    assert draft.text == "# 标题\n正文"


def test_字面反斜杠n不被当成JSON转义() -> None:
    events: list[str] = []
    gateway = _Stream([(["行\\n列"], False)])
    request_final(
        gateway,
        (Message(role="user", content="写"),),
        max_tokens=200,
        on_delta=events.append,
    )
    assert events == ["行\\n列"]


def test_截断后续写只追加新片段() -> None:
    events: list[str] = []
    gateway = _Stream([(["半截"], True), (["续上"], False)])
    draft = request_final(
        gateway,
        (Message(role="user", content="写"),),
        max_tokens=200,
        n_ctx=8192,
        n_past=1000,
        on_delta=events.append,
    )
    assert events == ["半截", "续上"]
    assert draft.text == "半截续上"
    assert gateway.calls == 2
