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
_turn_id: ContextVar[str] = ContextVar("metric_turn_id", default="-")


def bind_turn_id(turn_id: str):
    """这一轮用户请求的 id。没绑定时请求记录仍用 `-`。"""
    return _turn_id.set(turn_id)


def reset_turn_id(token) -> None:
    _turn_id.reset(token)


def current_turn_id() -> str:
    return _turn_id.get()


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
    target_tokens: int | None = None,
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
        "target_tokens": target_tokens,
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
    turn_id: str | None = None,
    retry_index: int = 0,
    recovery: str = "none",
    target_tokens: int | None = None,
) -> Iterator[None]:
    """标出这一次模型请求属于哪一阶段。记录逻辑仍在请求封装里。"""
    token = _scope.set(
        {
            "phase": phase,
            "task_type": task_type,
            "n_ctx": n_ctx,
            "turn_id": current_turn_id() if turn_id is None else turn_id,
            "retry_index": retry_index,
            "recovery": recovery,
            "target_tokens": target_tokens,
        }
    )
    try:
        yield
    finally:
        _scope.reset(token)


def note_empty_response(
    *,
    finish_reason: str,
    prompt_tokens: int,
    max_tokens: int,
    temperature: float,
    streaming: bool,
    retry_index: int,
) -> tuple[bool, int]:
    """记下一次空内容。返回（是否贴近上下文余量，n_ctx）。"""
    from spoolkit.context.phase_budget import DEFAULT_MARGIN

    scope = _scope.get() or {}
    n_ctx = int(scope.get("n_ctx") or 0)
    tight = n_ctx > 0 and prompt_tokens >= n_ctx - DEFAULT_MARGIN
    sink = _sink.get()
    if sink is not None:
        sink.append(
            {
                "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "event": "empty_response",
                "finish_reason": finish_reason or "",
                "prompt_tokens": prompt_tokens,
                "max_tokens": max_tokens,
                "n_ctx": n_ctx,
                "temperature": temperature,
                "streaming": streaming,
                "retry_index": retry_index,
                "context_tight": tight,
            }
        )
    return tight, n_ctx


def mark_request_retry() -> None:
    """同一次请求的下一次调用记为 retry_index=1。"""
    scope = _scope.get()
    if scope is not None:
        scope["retry_index"] = 1


def render_section_cache_by_arm(rows: Sequence[dict[str, Any]]) -> str:
    """section 命中率按臂分开。混合值只用于核对。"""
    sections = [
        row for row in rows
        if row.get("phase") == "section" and row.get("cache_hit") is not None
    ]
    if not sections:
        return ""

    def _rate(items: Sequence[dict[str, Any]], *, forced_zero: bool) -> str:
        hits = sum(1 for item in items if item.get("cache_hit"))
        if forced_zero and hits == 0:
            return "0（强制未命中）"
        return f"{hits / len(items):.2f}（{hits}/{len(items)}）"

    lines = [
        "| 臂 | 命中率 | 说明 |",
        "|---|---|---|",
    ]
    on = [row for row in sections if row.get("arm") == "cache-on"]
    off = [row for row in sections if row.get("arm") == "cache-off"]
    if on:
        lines.append(f"| cache-on | {_rate(on, forced_zero=False)} | 首节冷启动 miss |")
    if off:
        lines.append(f"| cache-off | {_rate(off, forced_zero=True)} | 对照组 |")
    lines.append(f"| 混合 | {_rate(sections, forced_zero=False)} | 仅用于核对，不用于判定 |")
    return "\n".join(lines) + "\n"


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
    first_token_ms: int | None = None,
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
            first_token_ms=first_token_ms,
            prefill_ms=_prefill_ms(timings),
            target_tokens=scope.get("target_tokens"),
        )
    )


def _progress_state() -> dict[str, int]:
    current = _progress.get() or {}
    return {
        "total": int(current.get("total") or 0),
        "completed": int(current.get("completed") or 0),
        "retried": int(current.get("retried") or 0),
        "empty": int(current.get("empty") or 0),
    }


def commit_section(*, total: int, completed: int) -> None:
    """一节提交后更新完成数。不在这里拼 JSON。"""
    state = _progress_state()
    state["total"] = total
    state["completed"] = completed
    _progress.set(state)


def note_retry() -> None:
    """本节发出了续写或重写。"""
    state = _progress_state()
    state["retried"] = int(state["retried"]) + 1
    _progress.set(state)


def note_empty_section() -> None:
    """这一节重试后仍空，正文用占位交出去。"""
    state = _progress_state()
    state["empty"] = int(state["empty"]) + 1
    _progress.set(state)


def reset_section_progress() -> None:
    _progress.set({"total": 0, "completed": 0, "retried": 0, "empty": 0})


def note_parse_failure(*, reason: str, kind: str = "") -> None:
    """解析失败时把原因记到刚写完的那条请求上。没打开采集就什么都不做。"""
    sink = _sink.get()
    if not sink:
        return
    for row in reversed(sink):
        if row.get("phase"):
            row["parse_reason"] = reason
            row["parse_kind"] = kind
            return


def observe_turn(
    *,
    turn_id: str,
    task_type: str,
    total_ms: int,
    completed: bool,
    fallback_short: bool,
    long_skipped_reason: str = "",
) -> dict[str, Any] | None:
    """回合结束时汇总已经记下的请求。没打开捕获就不写。"""
    sink = _sink.get()
    if sink is None:
        return None
    requests = [
        row for row in sink if row.get("phase") and row.get("turn_id") == turn_id
    ]
    progress = _progress_state()
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
        "empty_sections": int(progress["empty"]),
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
        "long_skipped_reason": long_skipped_reason,
    }
    sink.append(record)
    reset_section_progress()
    return record


def aggregate_by_phase(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按阶段汇总请求。没有布尔 cache_hit、也没有首 token 时留空，不写成 0。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        phase = row.get("phase")
        if not phase:
            continue
        grouped.setdefault(str(phase), []).append(row)
    table: dict[str, dict[str, Any]] = {}
    for phase, items in grouped.items():
        known = [item.get("cache_hit") for item in items if item.get("cache_hit") is not None]
        rate = None if not known else sum(1 for hit in known if hit) / len(known)
        first = [int(item["first_token_ms"]) for item in items if item.get("first_token_ms") is not None]
        elapsed = [int(item["total_ms"]) for item in items if item.get("total_ms") is not None]
        truncated = sum(1 for item in items if item.get("outcome") == "truncated")
        table[phase] = {
            "count": len(items),
            "truncation_rate": truncated / len(items),
            "cache_hit_rate": rate,
            "first_token_ms_p50": _median(first) if first else None,
            "total_ms_p50": _median(elapsed) if elapsed else None,
            "cache_hit_rate_excl_first": None,
            "cold_start_misses": None,
        }
        if phase == "section":
            excl, cold = _section_cache_excl_first(items)
            table[phase]["cache_hit_rate_excl_first"] = excl
            table[phase]["cold_start_misses"] = cold
    return table


def _section_cache_excl_first(items: Sequence[dict[str, Any]]) -> tuple[float | None, int]:
    """每篇第一节算冷启动。返回其余请求的命中率，以及冷启动条数。"""
    seen: set[str] = set()
    cold = 0
    rest_hits = 0
    rest = 0
    for row in items:
        if row.get("cache_hit") is None:
            continue
        turn = str(row.get("turn_id") or "")
        if turn not in seen:
            seen.add(turn)
            cold += 1
            continue
        rest += 1
        if row.get("cache_hit"):
            rest_hits += 1
    if rest == 0:
        return None, cold
    return rest_hits / rest, cold


def render_retry_share(rows: Sequence[dict[str, Any]]) -> str:
    """retry_index>0 的请求占比。续写固定记 1，空节换提示也记在这里。"""
    requests = [row for row in rows if row.get("phase")]
    if not requests:
        return "- 重试占比：未采集"
    retried = [row for row in requests if int(row.get("retry_index") or 0) > 0]
    total = sum(int(row.get("total_ms") or 0) for row in requests)
    spent = sum(int(row.get("total_ms") or 0) for row in retried)
    time_rate = "未采集" if total <= 0 else f"{spent / total:.1%}"
    return (
        f"- 重试占比：retry_index>0 为 {len(retried)}/{len(requests)}"
        f"（{len(retried) / len(requests):.1%}），耗时 {time_rate}。"
        "continuation 的 retry_index 固定为 1，空节换提示也记在这里。"
    )


def render_phase_report(rows: Sequence[dict[str, Any]]) -> str:
    """按 phase 出表。缓存命中率单独一列；缺样本写未采集。"""
    table = aggregate_by_phase(rows)
    if not table:
        return "按阶段：未采集\n"
    lines = [
        "| 阶段 | 请求数 | 截断率 | 缓存命中率 | 去掉首节 | 冷启动 miss | P50 首 token | P50 总时延 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for phase in sorted(table):
        item = table[phase]
        rate = item["cache_hit_rate"]
        first = item["first_token_ms_p50"]
        elapsed = item["total_ms_p50"]
        excl = item["cache_hit_rate_excl_first"]
        cold = item["cold_start_misses"]
        lines.append(
            "| {phase} | {count} | {cut:.2f} | {hit} | {excl} | {cold} | {token} | {elapsed} |".format(
                phase=phase,
                count=item["count"],
                cut=item["truncation_rate"],
                hit="未采集" if rate is None else f"{rate:.2f}",
                excl="—" if excl is None else f"{excl:.2f}",
                cold="—" if cold is None else str(cold),
                token="未采集" if first is None else str(first),
                elapsed="未采集" if elapsed is None else str(elapsed),
            )
        )
    section = table.get("section")
    if section and section["cache_hit_rate_excl_first"] is not None:
        excl = section["cache_hit_rate_excl_first"]
        cold = section["cold_start_misses"]
        arms = {
            row.get("arm")
            for row in rows
            if row.get("phase") == "section" and row.get("arm")
        }
        if "cache-on" in arms and "cache-off" in arms:
            lines.append(
                f"section 去掉首节 {excl:.2f}，冷启动 miss {cold}（每篇第一节）。"
                "上表把对照组算进去，混合值仅用于核对，不用于判定。"
            )
        else:
            verdict = "满足 ≥0.9" if excl >= 0.9 else "低于 0.9"
            lines.append(
                f"section 去掉首节 {excl:.2f}，冷启动 miss {cold}（每篇第一节）。口径：{verdict}，且冷启动单独解释。"
            )
    return "\n".join(lines) + "\n"


def write_metrics(path: Any, rows: Sequence[dict[str, Any]]) -> None:
    """追加 JSONL。一行一条，正文不在里面。"""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(dumps_metric(row) + "\n")


def read_metrics(path: Any) -> list[dict[str, Any]]:
    from pathlib import Path

    target = Path(path)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


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


def _prefill_ms(timings: dict | None) -> int | None:
    """llama.cpp 的 prompt_ms 就是 prefill。没给这个字段就不填 0。"""
    if not timings or timings.get("prompt_ms") is None:
        return None
    return int(float(timings["prompt_ms"]))


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
