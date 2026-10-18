"""终稿纯文本请求。

前缀必须与控制回合逐条相同，只在末尾追加一条 user 指令，
这样 llama.cpp 的 `cache_prompt` 才能复用已经算过的 KV。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from spoolkit.llm.types import ChatRequest, ChatResponse, Message

FINAL_INSTRUCTION = (
    "（进入终稿阶段：只输出最终 Markdown 正文，先给 150–250 字 TL;DR，"
    "再按大纲展开；不要调用工具，不要输出 JSON。）"
)

# 终稿指令本身不该把提示词涨过这么多；再多就说明前缀被改过。
PREFIX_GROWTH_LIMIT = 200


def final_messages(prefix: Sequence[Message]) -> tuple[Message, ...]:
    """复制控制回合的消息，末尾追加终稿指令。不改入参。"""
    return tuple(prefix) + (Message(role="user", content=FINAL_INSTRUCTION),)


def prefix_growth(control_prompt_tokens: int, final_prompt_tokens: int) -> int:
    """第二次请求比控制回合多出来的 prompt token。"""
    return final_prompt_tokens - control_prompt_tokens


def final_request(prefix: Sequence[Message], *, max_tokens: int) -> ChatRequest:
    return ChatRequest(
        messages=final_messages(prefix),
        max_tokens=max_tokens,
        response_schema=None,
        cache_prompt=True,
    )


@dataclass(frozen=True)
class FinalDraft:
    text: str
    prompt_tokens: int
    completion_tokens: int
    truncated: bool


def request_final(
    gateway: Any,
    prefix: Sequence[Message],
    *,
    max_tokens: int,
) -> FinalDraft:
    """一次纯文本终稿。不走截断翻倍重试。"""
    response: ChatResponse = gateway.chat(final_request(prefix, max_tokens=max_tokens))
    return FinalDraft(
        text=response.text,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        truncated=response.truncated,
    )
