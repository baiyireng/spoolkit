"""终稿纯文本请求。

第一次终稿的前缀必须与控制回合逐条相同，只在末尾追加一条 user 指令，
这样 llama.cpp 的 `cache_prompt` 才能复用已经算过的 KV。
续写仍保留这份前缀，再追加已写正文的尾部（assistant）和一条只要求新增内容的 user 指令。
尾部长度按剩余窗口收缩，先留最小可用尾部，再扩到上限，并且不挤掉最小输出。
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from spoolkit.agent.metrics import metric_scope
from spoolkit.agent.continuation import (
    REWRITE_FACTOR,
    looks_repeated,
    merge_continuation,
    overlap_chars,
)
from spoolkit.agent.structure import fences_closed
from spoolkit.context.phase_budget import (
    DEFAULT_MARGIN,
    MIN_OUTPUT,
    Phase,
    compute_budget,
)
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatRequest, ChatResponse, Message

logger = logging.getLogger(__name__)

FINAL_INSTRUCTION = (
    "（进入终稿阶段：只输出最终 Markdown 正文，先给 150–250 字 TL;DR，"
    "再按大纲展开；不要调用工具，不要输出 JSON。）"
)

CONTINUATION_INSTRUCTION = (
    "接着你已写的结尾继续写。不要重复已写内容，不要重新开头，不要总结，不要加标题。"
)

# 续写窗口上限。落在 1500–2500 之间：够接上结尾，又不把整篇正文塞回提示词。
PARTIAL_TAIL_CHARS = 2000
# 长正文至少留这么多，模型和 overlap 才看得到结尾。短于它的正文整段保留。
PARTIAL_TAIL_MIN_CHARS = 200

CONTINUATION_TAIL_INSTRUCTION = (
    "以上是你已经写出的最后一段。只输出紧接其后的新增内容，不要重复，不要重新开头，不要总结。"
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


def partial_tail(text: str, *, limit: int = PARTIAL_TAIL_CHARS) -> str:
    """已写正文的尾部。空串原样返回；短于窗口的不补、不截成空。"""
    if not text or limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]


# 只给续写插入量用。CHARS_PER_TOKEN 仍是旧路径的粗估，这里不碰它。
_CONTINUATION_COUNTER = OfflineTokenCounter()


def estimate_tokens(text: str) -> int:
    """续写尾部和续写指令的保守 token 数。

    与 OfflineTokenCounter 相同：ASCII 4 字 1 token，非 ASCII 1 字 1 token。
    不用 CHARS_PER_TOKEN。1.6 字/token 会把「甲」*2000 估成 1250，
    中文实际接近一字一 token，插进续写请求就会撑破 n_ctx。
    角色等特殊 token 不单计，留给 DEFAULT_MARGIN。
    """
    return _CONTINUATION_COUNTER.count(text)


def continuation_messages(
    prefix: Sequence[Message],
    partial: str,
    *,
    tail: str | None = None,
) -> tuple[Message, ...]:
    """控制前缀不动。非空尾部作为 assistant，后面接续写指令。"""
    chosen = partial_tail(partial) if tail is None else tail
    extra: tuple[Message, ...]
    if chosen:
        extra = (
            Message(role="assistant", content=chosen),
            Message(role="user", content=CONTINUATION_TAIL_INSTRUCTION),
        )
    else:
        extra = (Message(role="user", content=CONTINUATION_TAIL_INSTRUCTION),)
    return tuple(prefix) + extra


@dataclass(frozen=True)
class ContinuationPlan:
    tail: str
    follow_prompt: int
    follow_limit: int
    send: bool


def plan_continuation(
    partial: str,
    *,
    base_prompt: int,
    n_ctx: int | None,
    fallback_limit: int,
) -> ContinuationPlan:
    """按剩余窗口决定尾部和续写输出预算。放不下最小输出就不发请求。"""
    instruction_tokens = estimate_tokens(CONTINUATION_TAIL_INSTRUCTION)
    if n_ctx is None:
        tail = partial_tail(partial)
        return ContinuationPlan(
            tail=tail,
            follow_prompt=base_prompt + estimate_tokens(tail) + instruction_tokens,
            follow_limit=fallback_limit,
            send=fallback_limit > 0,
        )
    spare = (
        n_ctx
        - DEFAULT_MARGIN
        - base_prompt
        - instruction_tokens
        - MIN_OUTPUT
    )
    tail = _fit_tail(partial, spare)
    if tail is None:
        return ContinuationPlan(
            tail="",
            follow_prompt=base_prompt + instruction_tokens,
            follow_limit=0,
            send=False,
        )
    follow_prompt = base_prompt + estimate_tokens(tail) + instruction_tokens
    follow_limit = compute_budget(
        n_ctx=n_ctx,
        prompt_tokens=follow_prompt,
        phase="continuation",
    ).max_tokens
    return ContinuationPlan(
        tail=tail,
        follow_prompt=follow_prompt,
        follow_limit=follow_limit,
        send=follow_limit >= MIN_OUTPUT,
    )


def _fit_tail(partial: str, spare_tokens: int) -> str | None:
    """按真实后缀 token 二分。None 表示最小可用尾部放不进剩余预算。"""
    if not partial:
        return ""
    if spare_tokens <= 0:
        return None
    cap = min(len(partial), PARTIAL_TAIL_CHARS)
    minimum = min(cap, PARTIAL_TAIL_MIN_CHARS)
    if estimate_tokens(partial[-minimum:]) > spare_tokens:
        return None
    lo = minimum
    hi = cap
    best = minimum
    while lo <= hi:
        mid = (lo + hi) // 2
        if estimate_tokens(partial[-mid:]) <= spare_tokens:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return partial[-best:]


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
    prompt_tokens: int | None = None,
) -> int:
    if n_ctx is not None:
        return compute_budget(
            n_ctx=n_ctx,
            prompt_tokens=prompt_tokens,
            n_past=None if prompt_tokens is not None else n_past,
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
    prompt_tokens: int | None = None,
    target_tokens: int | None = None,
    overlap: int | None = None,
    on_delta: Any = None,
) -> FinalDraft:
    """纯文本终稿。截断后续写一次并去掉重叠；续写若在复述，则按 1.5 倍预算重写一次。

    续写本身再被截断就停，留下继续入口，不再加请求。
    """
    limit = _output_limit(
        max_tokens=max_tokens,
        n_ctx=n_ctx,
        n_past=n_past,
        prompt_tokens=prompt_tokens,
        phase="final",
        target_tokens=target_tokens,
    )
    if limit <= 0:
        logger.warning("终稿没有输出预算，改为短答并留下继续入口")
        return FinalDraft(
            text=SHORT_CONTINUE.strip(),
            prompt_tokens=prompt_tokens if prompt_tokens is not None else n_past,
            completion_tokens=0,
            truncated=True,
        )

    with metric_scope(phase="final", task_type="long", n_ctx=n_ctx or 0):
        response = _complete(gateway, final_request(prefix, max_tokens=limit), on_delta)
    structural = not response.truncated and not fences_closed(response.text)
    if not response.truncated and not structural:
        return _draft(response, response.text, False)

    if structural:
        logger.warning("终稿结构未完成，续写一次")
    else:
        logger.warning("终稿被截断，续写一次")
    if response.prompt_tokens:
        base_prompt = response.prompt_tokens
    elif prompt_tokens is not None:
        base_prompt = prompt_tokens
    else:
        base_prompt = n_past
    plan = plan_continuation(
        response.text,
        base_prompt=base_prompt,
        n_ctx=n_ctx,
        fallback_limit=limit,
    )
    if not plan.send:
        logger.warning("终稿续写预算不足，保留已写正文并留下继续入口")
        return _draft(response, response.text + SHORT_CONTINUE, True)

    held: list[str] = []
    with metric_scope(
        phase="continuation",
        task_type="long",
        n_ctx=n_ctx or 0,
        retry_index=1,
        recovery="continue",
    ):
        follow = _complete(
            gateway,
            ChatRequest(
                messages=continuation_messages(prefix, response.text, tail=plan.tail),
                max_tokens=plan.follow_limit,
                response_schema=None,
                cache_prompt=True,
            ),
            held.append if on_delta is not None else None,
        )
    merged = merge_continuation(
        response.text, follow.text, overlap=overlap_chars(overlap)
    )
    fresh = merged[len(response.text) :]
    if follow.truncated or not follow.text.strip():
        logger.warning("终稿续写未完成，改为短答并留下继续入口")
        _emit(on_delta, fresh)
        _emit(on_delta, SHORT_CONTINUE)
        return _draft(follow, merged + SHORT_CONTINUE, True)
    if not structural and looks_repeated(response.text, follow.text):
        logger.warning("终稿续写与已写内容重复，按 1.5 倍预算重写一次")
        return _rewrite_final(
            gateway,
            prefix,
            limit=limit,
            n_ctx=n_ctx,
            n_past=n_past,
            prompt_tokens=follow.prompt_tokens or response.prompt_tokens or prompt_tokens,
            on_delta=on_delta,
        )
    _emit(on_delta, fresh)
    return _draft(follow, merged, False)


def _rewrite_final(
    gateway: Any,
    prefix: Sequence[Message],
    *,
    limit: int,
    n_ctx: int | None,
    n_past: int,
    prompt_tokens: int | None = None,
    on_delta: Any,
) -> FinalDraft:
    """整段重写一次。预算是原终稿的 1.5 倍，不是 2 倍。"""
    requested = int(limit * REWRITE_FACTOR)
    rewrite_limit = _output_limit(
        max_tokens=requested,
        n_ctx=n_ctx,
        n_past=n_past,
        prompt_tokens=prompt_tokens,
        phase="final",
        target_tokens=requested,
    )
    if rewrite_limit <= 0:
        logger.warning("终稿重写没有输出预算，改为短答并留下继续入口")
        _emit(on_delta, SHORT_CONTINUE)
        return FinalDraft(
            text=SHORT_CONTINUE.strip(),
            prompt_tokens=prompt_tokens if prompt_tokens is not None else n_past,
            completion_tokens=0,
            truncated=True,
        )
    with metric_scope(
        phase="final",
        task_type="long",
        n_ctx=n_ctx or 0,
        retry_index=2,
        recovery="rewrite",
    ):
        rewrite = _complete(
            gateway, final_request(prefix, max_tokens=rewrite_limit), on_delta
        )
    if rewrite.truncated or not rewrite.text.strip():
        logger.warning("终稿重写未完成，改为短答并留下继续入口")
        _emit(on_delta, SHORT_CONTINUE)
        return _draft(rewrite, rewrite.text + SHORT_CONTINUE, True)
    return _draft(rewrite, rewrite.text, False)


def _emit(on_delta: Any, text: str) -> None:
    if on_delta is not None and text:
        on_delta(text)
