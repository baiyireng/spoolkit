"""终稿请求复用控制回合前缀，只在末尾追加一条纯文本指令。"""

from spoolkit.agent.final_turn import (
    FINAL_INSTRUCTION,
    PREFIX_GROWTH_LIMIT,
    final_messages,
    prefix_growth,
    request_final,
)
from spoolkit.llm.types import ChatRequest, ChatResponse, Message


class _Script:
    def __init__(self, text: str, prompt_tokens: int) -> None:
        self.request: ChatRequest | None = None
        self._text = text
        self._prompt_tokens = prompt_tokens

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.request = request
        return ChatResponse(
            text=self._text,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=12,
        )


def test_终稿消息只在控制前缀后追加一条指令() -> None:
    prefix = (
        Message(role="system", content="系统"),
        Message(role="user", content="把方案写清楚"),
        Message(role="tool", content="工具结果", name="read_file"),
    )
    messages = final_messages(prefix)
    assert messages[:-1] == prefix
    assert messages[-1].role == "user"
    assert messages[-1].content == FINAL_INSTRUCTION
    assert "JSON" in FINAL_INSTRUCTION


def test_终稿请求不带schema并打开前缀缓存() -> None:
    prefix = (Message(role="user", content="写一段说明"),)
    gateway = _Script("# 说明\n正文", prompt_tokens=1180)
    draft = request_final(gateway, prefix, max_tokens=800)
    assert draft.text == "# 说明\n正文"
    assert draft.prompt_tokens == 1180
    request = gateway.request
    assert request is not None
    assert request.response_schema is None
    assert request.cache_prompt is True
    assert request.max_tokens == 800
    assert request.messages[:-1] == prefix


def test_第二次提示词增量低于200才算命中前缀() -> None:
    assert prefix_growth(1000, 1150) == 150
    assert prefix_growth(1000, 1150) < PREFIX_GROWTH_LIMIT
    assert prefix_growth(1000, 1300) >= PREFIX_GROWTH_LIMIT
