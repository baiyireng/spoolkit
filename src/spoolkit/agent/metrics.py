"""长文指标记录。

只存计数、时延、结束原因和正文哈希。不存提示词，也不存终稿正文。
没有真机样本时，报告写「未采集」，不把脚本夹具的结果当成达标。
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


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
