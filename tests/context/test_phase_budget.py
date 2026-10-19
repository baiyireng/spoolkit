"""阶段预算：长文各阶段的 max_tokens，与旧的窗口比例预留分开。"""

import logging

from spoolkit.context.phase_budget import (
    compute_budget,
    output_tokens_for_turn,
    resolve_n_past,
)
from spoolkit.llm.types import Message


def test_n_past为零时控制回合用硬上限() -> None:
    result = compute_budget(n_ctx=8192, n_past=0, phase="control")
    assert result.max_tokens == 768
    assert result.capped is False
    assert result.reason == "ok"


def test_n_past贴近窗口时上下文已满() -> None:
    result = compute_budget(n_ctx=8192, n_past=8192 - 100, phase="final")
    assert result.max_tokens == 0
    assert result.capped is True
    assert result.reason == "context-full"


def test_负数n_past仍被阶段硬上限卡住() -> None:
    result = compute_budget(n_ctx=8192, n_past=-50, phase="control")
    assert result.max_tokens == 768
    assert result.capped is False
    assert result.reason == "ok"


def test_目标低于硬上限时按目标给() -> None:
    result = compute_budget(
        n_ctx=8192, n_past=0, phase="final", target_tokens=400
    )
    assert result.max_tokens == 400
    assert result.capped is False
    assert result.reason == "ok"


def test_目标高于硬上限时按上限截断() -> None:
    # 方案算法：只要没给到 target，reason 就是 context-full，
    # 即使真正卡住的是阶段硬上限而不是窗口。
    result = compute_budget(
        n_ctx=8192, n_past=0, phase="final", target_tokens=9000
    )
    assert result.max_tokens == 2500
    assert result.capped is True
    assert result.reason == "context-full"


def test_旧协议不改输出预留() -> None:
    assert (
        output_tokens_for_turn(
            protocol_v2=False,
            legacy=1228,
            n_ctx=8192,
            n_past=0,
            phase="control",
        )
        == 1228
    )


def test_v2按阶段预算计算() -> None:
    assert (
        output_tokens_for_turn(
            protocol_v2=True,
            legacy=1228,
            n_ctx=8192,
            n_past=0,
            phase="control",
        )
        == 768
    )


def test_有prompt_tokens时直接采用(caplog) -> None:
    caplog.set_level(logging.WARNING)
    assert resolve_n_past(400, [Message(role="user", content="hello" * 100)]) == 400
    assert caplog.records == []


def test_重算量小于完整提示词时按完整提示词收紧预算() -> None:
    n_ctx = 8192
    margin = 192
    prompt_n = 1000
    p_total = 6000
    assert prompt_n < p_total
    result = compute_budget(
        n_ctx=n_ctx,
        prompt_tokens=p_total,
        phase="final",
        target_tokens=8000,
        margin=margin,
    )
    assert result.max_tokens <= n_ctx - p_total - margin
    by_recompute = compute_budget(
        n_ctx=n_ctx,
        prompt_tokens=prompt_n,
        phase="final",
        target_tokens=8000,
        margin=margin,
    )
    assert result.max_tokens < by_recompute.max_tokens


def test_完整提示词距窗口只剩一百时上下文已满() -> None:
    n_ctx = 8192
    result = compute_budget(
        n_ctx=n_ctx,
        prompt_tokens=n_ctx - 100,
        phase="final",
    )
    assert result.max_tokens == 0
    assert result.reason == "context-full"


def test_重算量等于完整提示词时与旧口径一致(caplog) -> None:
    caplog.set_level(logging.WARNING)
    shared = 1000
    current = compute_budget(
        n_ctx=8192,
        prompt_tokens=shared,
        phase="final",
        target_tokens=400,
    )
    previous = compute_budget(
        n_ctx=8192,
        n_past=shared,
        phase="final",
        target_tokens=400,
    )
    assert current == previous
    assert any("n_past" in record.message for record in caplog.records)


def test_没有prompt_tokens时按字数估算并打日志(caplog) -> None:
    caplog.set_level(logging.WARNING)
    text = "字" * 16
    assert resolve_n_past(None, [Message(role="user", content=text)]) == int(16 / 1.6)
    assert any("prompt_tokens" in record.message for record in caplog.records)


def test_v2关闭时不估算也不打日志(caplog) -> None:
    caplog.set_level(logging.WARNING)
    text = "字" * 11800
    got = output_tokens_for_turn(
        protocol_v2=False,
        legacy=1228,
        n_ctx=8192,
        messages=[Message(role="user", content=text)],
        phase="control",
        safety_ratio=1.05,
    )
    assert got == 1228
    assert caplog.records == []


def test_估算余量默认一点零只有显式比例才放大() -> None:
    text = "字" * 100
    messages = [Message(role="user", content=text)]
    assert resolve_n_past(None, messages) == int(100 / 1.6)
    assert resolve_n_past(None, messages, safety_ratio=1.05) == int(100 / 1.6 * 1.05)


def test_v2首轮按一点零五估算且落在控制上限内() -> None:
    text = "字" * 11800
    messages = [Message(role="user", content=text)]
    estimate = int(11800 / 1.6 * 1.05)
    got = output_tokens_for_turn(
        protocol_v2=True,
        legacy=1228,
        n_ctx=8192,
        prompt_tokens=None,
        messages=messages,
        phase="control",
        safety_ratio=1.05,
    )
    assert 128 <= got <= 768
    assert got == min(768, 8192 - estimate - 192)


def test_v2次轮用上一轮实测收紧且不超过控制上限() -> None:
    n_ctx = 8192
    measured = 7500
    got = output_tokens_for_turn(
        protocol_v2=True,
        legacy=1228,
        n_ctx=n_ctx,
        prompt_tokens=measured,
        messages=[Message(role="user", content="字" * 100)],
        phase="control",
        safety_ratio=1.05,
    )
    assert got <= n_ctx - measured - 192
    assert got <= 768
    assert got == min(768, n_ctx - measured - 192)
