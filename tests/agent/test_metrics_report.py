"""指标按任务类型汇总，缓存命中率有格子可看。"""

from spoolkit.agent.metrics import (
    aggregate_by_task,
    capture_metrics,
    commit_section,
    metric_scope,
    note_parse_failure,
    note_retry,
    observe_response,
    observe_turn,
    render_task_report,
    reset_section_progress,
)


def _row(task_type: str, cache_hit: bool | None) -> dict:
    return {
        "task_type": task_type,
        "phase": "final",
        "cache_hit": cache_hit,
        "outcome": "ok",
        "total_ms": 10,
    }


def test_按任务类型聚合且缓存命中率可见() -> None:
    rows = [
        _row("long", True),
        _row("long", False),
        _row("short", None),
    ]
    table = aggregate_by_task(rows)
    assert table["long"]["count"] == 2
    assert table["long"]["cache_hit_rate"] == 0.5
    assert table["short"]["count"] == 1
    assert table["short"]["cache_hit_rate"] is None
    text = render_task_report(rows)
    assert "缓存命中率" in text
    assert "long" in text
    assert "short" in text
    assert "0.50" in text
    assert "未采集" in text


def test_回合结束汇总已提交的节和终稿命中率() -> None:
    reset_section_progress()
    with capture_metrics() as rows:
        commit_section(total=5, completed=4)
        note_retry()
        with metric_scope(phase="final", task_type="long", turn_id="t1"):
            observe_response(
                max_tokens=200,
                prompt_tokens=100,
                generated_tokens=10,
                generated_text="甲",
                finish_reason="stop",
                timings={"prompt_n": 20},
                total_ms=5,
                grammar=False,
            )
            observe_response(
                max_tokens=200,
                prompt_tokens=100,
                generated_tokens=8,
                generated_text="乙",
                finish_reason="stop",
                timings={"prompt_n": 100},
                total_ms=4,
                grammar=False,
            )
        record = observe_turn(
            turn_id="t1",
            task_type="long",
            total_ms=30,
            completed=False,
            fallback_short=True,
        )
    assert record is not None
    assert record["sections_total"] == 5
    assert record["sections_completed"] == 4
    assert record["sections_retried"] == 1
    assert record["total_generated_tokens"] == 18
    assert record["cache_hit_rate"] == 0.5
    assert record["fallback_short"] is True
    assert "甲" not in str(record)
    assert rows[-1]["turn_id"] == "t1"
    table = aggregate_by_task(rows)
    assert table["long"]["count"] == 2


def test_prompt_ms写入prefill缺字段则留空() -> None:
    with capture_metrics() as rows:
        with metric_scope(phase="final", task_type="short", turn_id="t2"):
            observe_response(
                max_tokens=32,
                prompt_tokens=18,
                generated_tokens=2,
                generated_text="OK",
                finish_reason="stop",
                timings={"prompt_n": 4, "prompt_ms": 358.2},
                total_ms=400,
                grammar=False,
                first_token_ms=360,
            )
            observe_response(
                max_tokens=32,
                prompt_tokens=18,
                generated_tokens=2,
                generated_text="OK",
                finish_reason="stop",
                timings={"prompt_n": 18},
                total_ms=400,
                grammar=False,
            )
    assert rows[0]["prefill_ms"] == 358
    assert rows[0]["first_token_ms"] == 360
    assert rows[1]["prefill_ms"] is None
    assert rows[1]["first_token_ms"] is None
    assert rows[0]["generated_chars"] == 2
    assert "generated_text" not in rows[0]


def test_解析失败原因写到最近一条请求() -> None:
    with capture_metrics() as rows:
        with metric_scope(phase="control", task_type="short", turn_id="t3"):
            observe_response(
                max_tokens=32,
                prompt_tokens=10,
                generated_tokens=4,
                generated_text="{}",
                finish_reason="stop",
                timings=None,
                total_ms=1,
                grammar=True,
            )
        note_parse_failure(reason="已标记 done，但 summary 为空", kind="")
    assert rows[0]["parse_reason"] == "已标记 done，但 summary 为空"
    assert rows[0]["parse_kind"] == ""
