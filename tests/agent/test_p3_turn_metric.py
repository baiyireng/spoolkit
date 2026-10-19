"""P3-4：回合结束写 turn_metric。缺缓存样本时命中率是未采集，不是 0。"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.metrics import (
    capture_metrics,
    observe_response,
    read_metrics,
    render_phase_report,
    write_metrics,
)
from spoolkit.config import Config
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatRequest, ChatResponse
from spoolkit.tools.registry import ToolRegistry


def _v2_done() -> str:
    return json.dumps(
        {
            "thought": "可以收了",
            "tool_calls": [],
            "done": True,
            "summary": "无需外部信息。结论已经齐了。",
        },
        ensure_ascii=False,
    )


def _old_done() -> str:
    return json.dumps(
        {
            "thought": "可以收了",
            "tool_calls": [],
            "state": None,
            "done": True,
            "final": "写完了",
        },
        ensure_ascii=False,
    )


class _Gateway:
    def __init__(self, *, v2: bool, record: bool) -> None:
        self.v2 = v2
        self.record = record
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        blob = "\n".join(item.content for item in request.messages)
        final = "进入终稿阶段" in blob
        text = "# 说明\n\n这是终稿正文。" if final else (_v2_done() if self.v2 else _old_done())
        if self.record:
            observe_response(
                max_tokens=request.max_tokens,
                prompt_tokens=400,
                generated_tokens=12,
                generated_text=text,
                finish_reason="stop",
                timings={"prompt_n": 10} if final else None,
                total_ms=5,
                grammar=request.response_schema is not None,
            )
        return ChatResponse(text=text, prompt_tokens=400, completion_tokens=12)

    def chat_stream(self, request: ChatRequest, on_delta=None) -> ChatResponse:
        response = self.chat(request)
        if on_delta is not None and response.text:
            on_delta(response.text)
        return response


def _loop(tmp_path: Path, gateway: _Gateway) -> AgentLoop:
    return AgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        tokenizer=OfflineTokenCounter(),
        registry=ToolRegistry(),
        config=Config(
            project_root=tmp_path,
            context_window=8192,
            max_steps=2,
            supervise=False,
            overrides={"protocol_v2": 1 if gateway.v2 else 0},
        ),
    )


def test_v2回合结束写出终稿命中率且首token未采集(tmp_path: Path) -> None:
    gateway = _Gateway(v2=True, record=True)
    loop = _loop(tmp_path, gateway)
    with capture_metrics() as rows:
        result = loop.run("把数字说清楚")
    assert result.finished is True
    assert len(gateway.requests) == 2
    turns = [row for row in rows if not row.get("phase")]
    assert len(turns) == 1
    assert turns[0]["cache_hit_rate"] == 1.0
    assert turns[0]["completed"] is True
    assert turns[0]["task_type"] == "short"
    assert "这是终稿正文" not in str(turns[0])
    text = render_phase_report(rows)
    assert "| final |" in text
    assert "| control |" in text
    assert "1.00" in text
    assert "未采集" in text
    assert "通过" not in text
    path = tmp_path / "metrics.jsonl"
    write_metrics(path, rows)
    loaded = read_metrics(path)
    assert render_phase_report(loaded) == text
    assert loaded[-1]["cache_hit_rate"] == 1.0


def test_section缓存按臂分开且混合值不用于判定() -> None:
    from spoolkit.agent.metrics import render_section_cache_by_arm

    rows = [
        {"phase": "section", "arm": "cache-on", "turn_id": "a", "cache_hit": False},
        {"phase": "section", "arm": "cache-on", "turn_id": "a", "cache_hit": True},
        {"phase": "section", "arm": "cache-off", "turn_id": "b", "cache_hit": False},
        {"phase": "section", "arm": "cache-off", "turn_id": "b", "cache_hit": False},
    ]
    text = render_section_cache_by_arm(rows)
    assert "cache-on" in text
    assert "0.50（1/2）" in text
    assert "首节冷启动" in text
    assert "cache-off" in text
    assert "0（强制未命中）" in text
    assert "对照组" in text
    assert "混合" in text
    assert "0.25" in text
    assert "不用于判定" in text


def test_单节下限只做观察() -> None:
    from scripts.p3_live_metrics import _judge_budget

    rows = [
        {
            "phase": "section",
            "prompt_index": index,
            "target_tokens": 400,
            "generated_tokens": 100,
        }
        for index in range(20)
    ]
    text = _judge_budget(rows)
    assert "中位数 25%" in text
    assert "不越界 20/20" in text
    assert "观察" in text
    assert "不低于目标一半 0/20" in text


def test_去掉首节后的缓存命中率单独列出() -> None:
    from spoolkit.agent.metrics import render_phase_report

    rows = [
        {"phase": "section", "turn_id": "a", "cache_hit": False, "outcome": "ok"},
        {"phase": "section", "turn_id": "a", "cache_hit": True, "outcome": "ok"},
        {"phase": "section", "turn_id": "b", "cache_hit": False, "outcome": "ok"},
        {"phase": "section", "turn_id": "b", "cache_hit": True, "outcome": "ok"},
    ]
    text = render_phase_report(rows)
    assert "0.50" in text
    assert "1.00" in text
    assert "冷启动 miss 2" in text


def test_同一路径第二次阅读会提示() -> None:
    from spoolkit.agent.loop import _long_skip_reason, _repeat_read_hint
    from spoolkit.tools.types import ToolCall

    seen: dict[str, int] = {}
    call = ToolCall(name="list_dir", arguments={"path": "."}, intent="再看")
    assert _repeat_read_hint(seen, call) == ""
    hint = _repeat_read_hint(seen, call)
    assert "已经读过" in hint
    assert _long_skip_reason("对比两件事", None, long_mode=False, room=1000) == "cue_miss"
    assert _long_skip_reason("今天天气", None, long_mode=True, room=1) == "room_insufficient"
    assert _long_skip_reason("写一份报告", None, long_mode=True, room=1000) == ""


def test_没有终稿样本时命中率不写成零(tmp_path: Path) -> None:
    gateway = _Gateway(v2=False, record=False)
    loop = _loop(tmp_path, gateway)
    with capture_metrics() as rows:
        result = loop.run("把数字说清楚")
    assert result.finished is True
    assert len(gateway.requests) == 1
    turns = [row for row in rows if not row.get("phase")]
    assert len(turns) == 1
    assert turns[0]["cache_hit_rate"] is None
    assert render_phase_report(rows) == "按阶段：未采集\n"
