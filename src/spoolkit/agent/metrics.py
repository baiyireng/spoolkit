"""长文指标记录。

只存计数、时延、结束原因和正文哈希。不存提示词，也不存终稿正文。
没有真机样本时，报告写「未采集」，不把脚本夹具的结果当成达标。
"""

import hashlib
import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar("metric_sink", default=None)
_scope: ContextVar[dict[str, Any] | None] = ContextVar("metric_scope", default=None)
_progress: ContextVar[dict[str, int] | None] = ContextVar("section_progress", default=None)


def build_request_metric(
    *,
    turn_id: str,
    request_id: str,
    phase: str,
    task_type: str,
    n_ctx: int,
    prompt_tokens: int,
    prompt_n: int | None,
    max_tokens: int,
    generated_text: str,
    finish_reason: str,
    total_ms: int,
    grammar: bool,
    retry_index: int,
    outcome: str,
    recovery: str,
    generated_tokens: int | None = None,
    cache_hit: bool | None = None,
    cache_reused_tokens: int | None = None,
    first_token_ms: int | None = None,
    prefill_ms: int | None = None,
) -> dict[str, Any]:
    """由一次生成的正文算出计数和哈希，正文本身不进入记录。"""
    return {
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "turn_id": turn_id,
        "request_id": request_id,
        "phase": phase,
        "task_type": task_type,
        "n_ctx": n_ctx,
        "prompt_tokens": prompt_tokens,
        "prompt_n": prompt_n,
        "cache_hit": cache_hit,
        "cache_reused_tokens": cache_reused_tokens,
        "max_tokens": max_tokens,
        "generated_tokens": generated_tokens,
        "finish_reason": finish_reason,
        "first_token_ms": first_token_ms,
        "prefill_ms": prefill_ms,
        "total_ms": total_ms,
        "grammar": grammar,
        "retry_index": retry_index,
        "outcome": outcome,
        "recovery": recovery,
        "generated_chars": len(generated_text),
        "content_hash": hashlib.sha256(generated_text.encode("utf-8")).hexdigest(),
    }


def dumps_metric(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def capture_metrics() -> Iterator[list[dict[str, Any]]]:
    """打开记录。没打开时请求封装不写任何东西。"""
    bucket: list[dict[str, Any]] = []
    token = _sink.set(bucket)
    try:
        yield bucket
    finally:
        _sink.reset(token)


@contextmanager
def metric_scope(
    *,
    phase: str,
    task_type: str,
    n_ctx: int = 0,
    turn_id: str = "-",
    retry_index: int = 0,
    recovery: str = "none",
) -> Iterator[None]:
    """标出这一次模型请求属于哪一阶段。记录逻辑仍在请求封装里。"""
    token = _scope.set(
        {
            "phase": phase,
            "task_type": task_type,
            "n_ctx": n_ctx,
            "turn_id": turn_id,
            "retry_index": retry_index,
            "recovery": recovery,
        }
    )
    try:
        yield
    finally:
        _scope.reset(token)


def observe_response(
    *,
    max_tokens: int,
    prompt_tokens: int,
    generated_tokens: int,
    generated_text: str,
    finish_reason: str,
    timings: dict | None,
    total_ms: int,
    grammar: bool,
) -> None:
    """请求封装在拿到响应后调用。没有 capture_metrics 时直接返回。"""
    sink = _sink.get()
    if sink is None:
        return
    scope = _scope.get() or {}
    prompt_n, cache_hit, reused = _cache_fields(prompt_tokens, timings)
    reason = finish_reason or "stop"
    sink.append(
        build_request_metric(
            turn_id=str(scope.get("turn_id") or "-"),
            request_id=uuid.uuid4().hex[:12],
            phase=str(scope.get("phase") or "unknown"),
            task_type=str(scope.get("task_type") or "short"),
            n_ctx=int(scope.get("n_ctx") or 0),
            prompt_tokens=prompt_tokens,
            prompt_n=prompt_n,
            max_tokens=max_tokens,
            generated_text=generated_text,
            finish_reason=reason,
            total_ms=total_ms,
            grammar=grammar,
            retry_index=int(scope.get("retry_index") or 0),
            outcome="truncated" if reason in ("length", "max_tokens") else "ok",
            recovery=str(scope.get("recovery") or "none"),
            generated_tokens=generated_tokens,
            cache_hit=cache_hit,
            cache_reused_tokens=reused,
        )
    )


def commit_section(*, total: int, completed: int) -> None:
    """一节提交后更新完成数。不在这里拼 JSON。"""
    state = dict(_progress.get() or {"total": 0, "completed": 0, "retried": 0})
    state["total"] = total
    state["completed"] = completed
    _progress.set(state)


def note_retry() -> None:
    """本节发出了续写或重写。"""
    state = dict(_progress.get() or {"total": 0, "completed": 0, "retried": 0})
    state["retried"] = int(state["retried"]) + 1
    _progress.set(state)


def reset_section_progress() -> None:
    _progress.set({"total": 0, "completed": 0, "retried": 0})


def observe_turn(
    *,
    turn_id: str,
    task_type: str,
    total_ms: int,
    completed: bool,
    fallback_short: bool,
) -> dict[str, Any] | None:
    """回合结束时汇总已经记下的请求。没打开捕获就不写。"""
    sink = _sink.get()
    if sink is None:
        return None
    requests = [
        row for row in sink if row.get("phase") and row.get("turn_id") == turn_id
    ]
    progress = _progress.get() or {"total": 0, "completed": 0, "retried": 0}
    finals = [row for row in requests if row.get("phase") == "final"]
    known = [row.get("cache_hit") for row in finals if row.get("cache_hit") is not None]
    rate = None if not known else sum(1 for hit in known if hit) / len(known)
    record = {
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "turn_id": turn_id,
        "task_type": task_type,
        "sections_total": int(progress["total"]),
        "sections_completed": int(progress["completed"]),
        "sections_retried": int(progress["retried"]),
        "total_generated_tokens": sum(int(row.get("generated_tokens") or 0) for row in requests),
        "recovery_tokens": sum(
            int(row.get("generated_tokens") or 0)
            for row in requests
            if row.get("recovery") not in (None, "none")
        ),
        "total_ms": total_ms,
        "cache_hit_rate": rate,
        "completed": completed,
        "fallback_short": fallback_short,
    }
    sink.append(record)
    reset_section_progress()
    return record


def aggregate_by_task(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按任务类型汇总。没有布尔 cache_hit 时命中率为空，不写成 0。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not row.get("phase"):
            continue
        grouped.setdefault(str(row.get("task_type") or "short"), []).append(row)
    table: dict[str, dict[str, Any]] = {}
    for task, items in grouped.items():
        known = [item.get("cache_hit") for item in items if item.get("cache_hit") is not None]
        rate = None if not known else sum(1 for hit in known if hit) / len(known)
        table[task] = {"count": len(items), "cache_hit_rate": rate}
    return table


def render_task_report(rows: Sequence[dict[str, Any]]) -> str:
    """缓存命中率单独一列。样本里没有这个字段时写未采集。"""
    table = aggregate_by_task(rows)
    lines = [
        "| 任务类型 | 请求数 | 缓存命中率 |",
        "|---|---|---|",
    ]
    for task in sorted(table):
        rate = table[task]["cache_hit_rate"]
        cell = "未采集" if rate is None else f"{rate:.2f}"
        lines.append(f"| {task} | {table[task]['count']} | {cell} |")
    return "\n".join(lines) + "\n"


def _cache_fields(
    prompt_tokens: int, timings: dict | None
) -> tuple[int | None, bool | None, int | None]:
    """prompt_n 小于完整提示词才算命中。服务端没给重算量就不猜。"""
    if not timings or "prompt_n" not in timings:
        return None, None, None
    prompt_n = int(timings["prompt_n"])
    if prompt_n < prompt_tokens:
        return prompt_n, True, prompt_tokens - prompt_n
    return prompt_n, False, 0


def render_report(results: list[Any] | tuple[Any, ...], live: object = None) -> str:
    """脚本结果和真机判定分开写。live 为空就是未采集。"""
    done = sum(1 for item in results if getattr(item, "completed", False))
    total = len(results)
    live_note = "未采集" if live is None else str(live)
    return "\n".join(
        [
            "# 长文回归",
            "",
            "| 项 | 结果 |",
            "|---|---|",
            f"| 脚本夹具 | {done}/{total} 标题齐全 |",
            f"| 真机完整率 ≥ 95% | {live_note} |",
            f"| 真机截断率 | {live_note} |",
            f"| P50 首 token | {live_note} |",
            f"| 缓存命中率 | {live_note} |",
            "",
            "脚本夹具只检查标题是否齐全。真机四组实验未跑，不能当作达标。",
            "",
        ]
    )
