"""终稿纯文本请求。

前缀必须与控制回合逐条相同，只在末尾追加一条 user 指令，
这样 llama.cpp 的 `cache_prompt` 才能复用已经算过的 KV。
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from spoolkit.context.phase_budget import Phase, compute_budget
from spoolkit.llm.types import ChatRequest, ChatResponse, Message

logger = logging.getLogger(__name__)

FINAL_INSTRUCTION = (
    "（进入终稿阶段：只输出最终 Markdown 正文，先给 150–250 字 TL;DR，"
    "再按大纲展开；不要调用工具，不要输出 JSON。）"
)

CONTINUATION_INSTRUCTION = (
    "接着你已写的结尾继续写。不要重复已写内容，不要重新开头，不要总结，不要加标题。"
)

# 续写仍被截断时留给用户的入口。不能悄悄把半截当成写完。
SHORT_CONTINUE = "\n\n（未写完。需要的话回复「继续」，从这里接着写。）"

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


def _output_limit(
    *,
    max_tokens: int | None,
    n_ctx: int | None,
    n_past: int,
    phase: Phase,
    target_tokens: int | None,
) -> int:
    if n_ctx is not None:
        return compute_budget(
            n_ctx=n_ctx,
            n_past=n_past,
            phase=phase,
            target_tokens=target_tokens,
        ).max_tokens
    if max_tokens is None:
        raise ValueError("终稿需要 max_tokens 或 n_ctx")
    return max_tokens


def _draft(response: ChatResponse, text: str, truncated: bool) -> FinalDraft:
    return FinalDraft(
        text=text,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        truncated=truncated,
    )


def _complete(
    gateway: Any,
    request: ChatRequest,
    on_delta: Any,
) -> ChatResponse:
    """有流式入口就按原文回调；否则整段 chat，再一次性交出正文。"""
    stream = getattr(gateway, "chat_stream", None)
    if on_delta is not None and callable(stream):
        return stream(request, on_delta=on_delta)
    response: ChatResponse = gateway.chat(request)
    if on_delta is not None and response.text:
        on_delta(response.text)
    return response


def request_final(
    gateway: Any,
    prefix: Sequence[Message],
    *,
    max_tokens: int | None = None,
    n_ctx: int | None = None,
    n_past: int = 0,
    target_tokens: int | None = None,
    on_delta: Any = None,
) -> FinalDraft:
    """纯文本终稿。截断后续写一次；仍截断则短答并留下继续入口。不翻倍重试。"""
    limit = _output_limit(
        max_tokens=max_tokens,
        n_ctx=n_ctx,
        n_past=n_past,
        phase="final",
        target_tokens=target_tokens,
    )
    if limit <= 0:
        logger.warning("终稿没有输出预算，改为短答并留下继续入口")
        return FinalDraft(
            text=SHORT_CONTINUE.strip(),
            prompt_tokens=n_past,
            completion_tokens=0,
            truncated=True,
        )

    response = _complete(gateway, final_request(prefix, max_tokens=limit), on_delta)
    if not response.truncated:
        return _draft(response, response.text, False)

    logger.warning("终稿被截断，续写一次")
    follow_limit = _output_limit(
        max_tokens=limit,
        n_ctx=n_ctx,
        n_past=response.prompt_tokens or n_past,
        phase="continuation",
        target_tokens=None,
    )
    if follow_limit <= 0:
        logger.warning("终稿续写未完成，改为短答并留下继续入口")
        return _draft(response, response.text + SHORT_CONTINUE, True)

    follow = _complete(
        gateway,
        ChatRequest(
            messages=tuple(prefix) + (
                Message(role="user", content=CONTINUATION_INSTRUCTION),
            ),
            max_tokens=follow_limit,
            response_schema=None,
            cache_prompt=True,
        ),
        on_delta,
    )
    merged = response.text + follow.text
    if follow.truncated or not follow.text.strip():
        logger.warning("终稿续写未完成，改为短答并留下继续入口")
        if on_delta is not None:
            on_delta(SHORT_CONTINUE)
        return _draft(follow, merged + SHORT_CONTINUE, True)
    return _draft(follow, merged, False)
