"""B1 契约：ToolResult 信封、TaskEvent、投影。不碰主循环。"""

import json
from pathlib import Path

import pytest

from spoolkit import settings
from spoolkit.agent.contract import (
    FIELD_ORDER,
    PHASES,
    SOURCES,
    SUMMARY_MAX_CHARS,
    SUMMARY_TOKEN_CEILING,
    TaskEvent,
    adapt_legacy,
    approval_event,
    clip_summary,
    error_from_exception,
    error_from_final,
    error_from_tool,
    ordered,
    project_for_model,
    retrieval_event,
    tool_event,
)
from spoolkit.llm.llamacpp import DEFAULT_BASE_URL, LlamaCppTokenCounter
from spoolkit.tools.types import Artifact, ToolResult, Usage


def test_旧工具适配成信封且不改原件() -> None:
    original = ToolResult(
        ok=True,
        content="正文" * 200,
        raw="证据-RAW-不要进模型",
        usage=Usage(prompt_tokens=3, completion_tokens=1, calls=1),
    )
    adapted = adapt_legacy(original)

    assert original.raw == "证据-RAW-不要进模型"
    assert original.summary == ""
    assert adapted.content == original.content
    assert adapted.raw == ""
    assert adapted.data == {}
    assert adapted.artifacts == ()
    assert adapted.summary == clip_summary(original.content)
    assert len(adapted.summary) <= SUMMARY_MAX_CHARS
    assert adapted.usage == original.usage
    assert adapted.ok is True


def test_已有summary时适配器只截断不改回content() -> None:
    original = ToolResult(ok=True, content="很长的正文", summary="短摘要")
    adapted = adapt_legacy(original)
    assert adapted.summary == "短摘要"
    assert adapted.content == "很长的正文"


def test_summary进提示且raw与thought不进() -> None:
    result = ToolResult(
        ok=True,
        content="整份文件不该出现在提示里",
        summary="已读 README，共 4 行",
        raw="SECRET-RAW",
        data={"lines": 4},
        artifacts=(Artifact(type="file", path="README.md"),),
        next="再看 app.py",
    )
    text = project_for_model(result, thought="SECRET-THOUGHT 整段思考")

    assert "已读 README，共 4 行" in text
    assert "SECRET-RAW" not in text
    assert "SECRET-THOUGHT" not in text
    assert "整份文件不该出现在提示里" not in text
    assert "再看 app.py" not in text
    assert "README.md" not in text


def test_没填summary时投影用截断后的content() -> None:
    result = ToolResult(ok=False, content="失败原因很短", raw="堆栈")
    text = project_for_model(result, thought="思考过程")
    assert text == "失败原因很短"
    assert "堆栈" not in text
    assert "思考过程" not in text


def test_运行时上限按字符不按离线分词() -> None:
    source = Path("src/spoolkit/agent/contract.py").read_text(encoding="utf-8")
    assert "OfflineTokenCounter" not in source
    long = "a" * (SUMMARY_MAX_CHARS + 50)
    clipped = clip_summary(long)
    assert len(clipped) <= SUMMARY_MAX_CHARS
    assert clipped != long


def test_summary最坏分词不超过服务端预算() -> None:
    url, _source = settings.resolve("base_url", "")
    counter = LlamaCppTokenCounter(url or DEFAULT_BASE_URL, timeout=30)
    width = SUMMARY_MAX_CHARS
    samples = {
        "cjk": "测" * width,
        "diff": ("- y = 1\n" * (width // 8 + 1))[:width],
        "ascii": "x\n" * (width // 2),
    }
    samples["ascii"] = samples["ascii"][:width]
    try:
        counts = {name: counter.count(text) for name, text in samples.items()}
    except Exception as exc:  # noqa: BLE001 - 服务端不可用时这条契约测不了
        pytest.fail(f"需要 llama.cpp /tokenize 验证 summary 上限，连不上：{exc}")
    finally:
        counter.close()
    worst = max(counts.values())
    assert worst <= SUMMARY_TOKEN_CEILING, counts


def test_taskevent字段顺序稳定且没有thought() -> None:
    event = tool_event(
        task_id="task",
        run_id="run",
        turn_id="turn",
        seq=2,
        step=1,
        tool_call_id="tc-2",
        intent="读 README",
        args_digest="path=README.md",
        status="ok",
        summary="4 行",
        refs=("README.md",),
        artifacts=(Artifact(type="file", path="README.md"),),
        duration=0.2,
        risk="",
    )
    assert "thought" not in TaskEvent.__dataclass_fields__
    payload = event.to_dict()
    assert list(payload) == list(FIELD_ORDER)
    again = json.loads(event.to_json())
    assert list(again) == list(FIELD_ORDER)
    assert again["phase"] == "observe"
    assert again["source"] == "tool"
    assert again["tool_call_id"] == "tc-2"
    assert again["artifacts"] == [{"type": "file", "path": "README.md", "ref": ""}]


def test_事件按seq稳定排序() -> None:
    later = tool_event(
        task_id="t", run_id="r", turn_id="u", seq=3, step=1, tool_call_id="tc-3",
        intent="", status="ok", summary="后",
    )
    earlier = retrieval_event(
        task_id="t", run_id="r", turn_id="u", seq=1, step=0, summary="预取",
    )
    assert [item.seq for item in ordered((later, earlier))] == [1, 3]


def test_失败与异常收成phase_error而不是新类型() -> None:
    tool_failed = error_from_tool(
        task_id="t", run_id="r", turn_id="u", seq=4, step=2,
        tool_call_id="tc-4", summary="用户拒绝了执行",
    )
    final_failed = error_from_final(
        task_id="t", run_id="r", turn_id="u", seq=5, step=2, summary="未完成",
    )
    exploded = error_from_exception(
        task_id="t", run_id="r", turn_id="u", seq=6, step=2,
        exc=RuntimeError("boom"),
    )
    assert {tool_failed.phase, final_failed.phase, exploded.phase} == {"error"}
    assert type(tool_failed) is TaskEvent
    assert exploded.source == "system"
    assert "boom" in exploded.summary
    assert set(PHASES) >= {
        "understand", "act", "observe", "decide", "verify",
        "final", "error", "retrieval", "approval",
    }
    assert set(SOURCES) == {"model", "tool", "system", "prefetch", "user"}


def test_预取是retrieval事件() -> None:
    event = retrieval_event(
        task_id="t", run_id="r", turn_id="u", seq=0, step=0,
        summary="README.md\n# 测量夹具",
    )
    assert event.phase == "retrieval"
    assert event.source == "prefetch"
    assert "测量夹具" in event.summary
    assert event.to_dict()["phase"] == "retrieval"


def test_diff与await用tool_call_id关联tool() -> None:
    observed = tool_event(
        task_id="t", run_id="r", turn_id="u", seq=1, step=1,
        tool_call_id="tc-9", intent="改返回值", status="ok", summary="已生成预览",
    )
    diff = approval_event(
        task_id="t", run_id="r", turn_id="u", seq=2, step=1,
        tool_call_id=observed.tool_call_id,
        status="await",
        summary="hello.py 1 处 diff",
        artifacts=(Artifact(type="diff", path="hello.py"),),
    )
    assert diff.phase == "approval"
    assert diff.tool_call_id == "tc-9"
    assert diff.tool_call_id == observed.tool_call_id


def test_非法phase被拒绝() -> None:
    with pytest.raises(ValueError):
        TaskEvent(
            task_id="t", run_id="r", turn_id="u", seq=0, step=0,
            phase="thought", source="model",
        )
