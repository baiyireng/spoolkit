"""长文回归夹具：五类题目走完大纲和逐节，指标里不留正文。"""

from collections import Counter

from spoolkit.agent.long_regression import LONG_PROMPTS, run_cut_short, run_scripted
from spoolkit.agent.metrics import build_request_metric, dumps_metric, render_report


def test_二十条覆盖五类且脚本路径标题齐全() -> None:
    assert len(LONG_PROMPTS) == 20
    assert Counter(item.category for item in LONG_PROMPTS) == {
        "方案": 4,
        "报告": 4,
        "对比": 4,
        "架构": 4,
        "表格": 4,
    }
    results = run_scripted(LONG_PROMPTS)
    assert len(results) == 20
    assert all(item.completed for item in results)
    assert all(item.fallback_short is False for item in results)
    assert sum(item.sections_total >= 5 for item in results) >= 5
    assert {item.category for item in results} == {"方案", "报告", "对比", "架构", "表格"}


def test_截断样本不算完整() -> None:
    result = run_cut_short()
    assert result.completed is False
    assert result.fallback_short is True
    assert result.sections_total >= 3


def test_指标行不留正文且短于一千字节() -> None:
    secret = "独特口令甲乙丙"
    record = build_request_metric(
        turn_id="t1",
        request_id="r1",
        phase="section",
        task_type="long",
        n_ctx=8192,
        prompt_tokens=1000,
        prompt_n=None,
        max_tokens=800,
        generated_text=secret,
        finish_reason="stop",
        total_ms=12,
        grammar=False,
        retry_index=0,
        outcome="ok",
        recovery="none",
    )
    line = dumps_metric(record)
    assert secret not in line
    assert "content_hash" in record
    assert record["generated_chars"] == len(secret)
    assert len(line.encode("utf-8")) < 1024
    assert record["prompt_tokens"] == 1000
    assert record["prompt_n"] is None


def test_报告写明判定且不把脚本当成真机达标() -> None:
    results = run_scripted(LONG_PROMPTS[:1])
    report = render_report(results, live=None)
    assert "未采集" in report
    assert "95%" in report
    assert "脚本" in report
    assert "口头通过" not in report
