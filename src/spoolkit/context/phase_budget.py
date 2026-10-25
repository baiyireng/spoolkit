"""按阶段计算输出预算。

主循环在 `protocol.v2` 关闭时仍用 `Budget.output_reserve()`。
这里的硬上限只在 v2 打开后生效，避免 8K 窗口把长文和决策挤在同一份 max_tokens 里。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

Phase = Literal["control", "final", "outline", "section", "continuation"]
BudgetReason = Literal["ok", "context-full", "below-min"]

# 代码里的默认。主循环实际读旋钮 protocol_v2，默认 0，与这里一致。
PROTOCOL_V2 = False


def protocol_v2_enabled(overrides: Mapping[str, float] | None = None) -> bool:
    """工作区把 protocol_v2 设成 1 才走新协议。默认仍是旧 JSON。"""
    from spoolkit.limits import resolve

    value, _source = resolve("protocol_v2", overrides)
    return value >= 1

HARD_CAP: dict[Phase, int] = {
    "control": 768,
    "final": 2500,
    "outline": 500,
    "section": 1000,
    "continuation": 2500,
}

DEFAULT_MARGIN = 192
MIN_OUTPUT = 128
CHARS_PER_TOKEN = 1.6


@dataclass(frozen=True)
class BudgetResult:
    max_tokens: int
    capped: bool
    reason: BudgetReason


def compute_budget(
    *,
    n_ctx: int,
    n_past: int | None = None,
    prompt_tokens: int | None = None,
    phase: Phase,
    target_tokens: int | None = None,
    margin: int | None = None,
) -> BudgetResult:
    """窗口减去完整提示词 P_total 和余量，再与阶段硬上限、目标取最小。

    `prompt_tokens` 是 P_total（含缓存前缀）。`n_past` 只是旧名字，
    传入时仍按 P_total 解释，并打一条废弃警告。
    """
    total = _prompt_total(prompt_tokens, n_past)
    room = margin if margin is not None else DEFAULT_MARGIN
    o_max = max(0, n_ctx - total - room)
    if o_max < MIN_OUTPUT:
        return BudgetResult(max_tokens=0, capped=True, reason="context-full")
    target = HARD_CAP[phase] if target_tokens is None else target_tokens
    max_tokens = min(o_max, HARD_CAP[phase], target)
    short = max_tokens < target
    return BudgetResult(
        max_tokens=max_tokens,
        capped=short,
        reason="context-full" if short else "ok",
    )


def output_tokens_for_turn(
    *,
    protocol_v2: bool,
    legacy: int,
    n_ctx: int = 0,
    n_past: int | None = None,
    prompt_tokens: int | None = None,
    messages: Sequence[object] = (),
    phase: Phase = "control",
    target_tokens: int | None = None,
    margin: int | None = None,
    safety_ratio: float = 1.0,
) -> int:
    """v2 关闭时原样返回旧预留，避免估算或打日志。"""
    if not protocol_v2:
        return legacy
    if prompt_tokens is None and n_past is None:
        prompt_tokens = resolve_n_past(None, messages, safety_ratio=safety_ratio)
    return compute_budget(
        n_ctx=n_ctx,
        n_past=n_past,
        prompt_tokens=prompt_tokens,
        phase=phase,
        target_tokens=target_tokens,
        margin=margin,
    ).max_tokens


def _prompt_total(prompt_tokens: int | None, n_past: int | None) -> int:
    if prompt_tokens is not None:
        return prompt_tokens
    if n_past is not None:
        logger.warning("n_past 已废弃，按 P_total 解释；请改传 prompt_tokens")
        return n_past
    logger.warning("没有 prompt_tokens，按 0 计算输出预算")
    return 0


def resolve_n_past(
    prompt_tokens: int | None,
    messages: Sequence[object],
    *,
    chars_per_token: float = CHARS_PER_TOKEN,
    safety_ratio: float = 1.0,
) -> int:
    """优先用服务端 `usage.prompt_tokens`。拿不到时按字数估算并打日志。

    `safety_ratio` 默认 1.0。只有主循环在估算时传 1.05，其它调用方保持原口径。
    """
    if prompt_tokens is not None and prompt_tokens >= 0:
        return prompt_tokens
    chars = 0
    for message in messages:
        content = message if isinstance(message, str) else getattr(message, "content", "")
        chars += len(content or "")
    estimate = int(chars / chars_per_token * safety_ratio) if chars_per_token > 0 else 0
    logger.warning(
        "拿不到 prompt_tokens，按消息长度估算 n_past=%s",
        estimate,
    )
    return estimate
