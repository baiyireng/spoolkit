"""证据门槛：普通问答不必检索；实时问题必须引用本轮工具结果。"""

from spoolkit.agent.evidence_gate import check_evidence


def test_普通问答写明无需外部信息就可以收() -> None:
    decision = check_evidence("什么是上下文窗口", "无需外部信息。窗口是一次能读的长度。", ())
    assert decision.ok is True
    assert decision.hard is False


def test_普通问答没有依据也不强迫检索() -> None:
    decision = check_evidence("什么是上下文窗口", "窗口就是长度。", ())
    assert decision.ok is True
    assert decision.hard is False
    assert decision.reason
    assert "无需外部信息" in decision.reason
    assert "必须调用" not in decision.reason
    assert "search" not in decision.reason


def test_长文模式直接放行() -> None:
    decision = check_evidence(
        "写一份完整方案",
        "先给结构。",
        (),
        long_answer=True,
    )
    assert decision.ok is True
    assert decision.hard is False
    assert decision.reason == ""


def test_普通问答引用了工具结果就可以收() -> None:
    tools = ("[read_file] 成功: 窗口默认是 8192",)
    decision = check_evidence("解释一下配置", "依据窗口默认是 8192。", tools)
    assert decision.ok is True
    assert decision.hard is False


def test_实时问题只说无需外部信息不算有依据() -> None:
    for question in ("今天的价格是多少", "最新版本是什么", "这个模型的版本号", "最新的公告"):
        decision = check_evidence(question, "无需外部信息。", ())
        assert decision.ok is False
        assert decision.hard is True


def test_实时问题引用工具结果就可以收() -> None:
    tools = ("[web] 成功: 今日价格是 12 元",)
    decision = check_evidence("今天的价格是多少", "依据今日价格是 12 元。", tools)
    assert decision.ok is True
    assert decision.hard is True


def test_普通问题缺引用放行并记一笔(tmp_path) -> None:
    from pathlib import Path

    from spoolkit.agent.audit import EVIDENCE_GAPS_KIND
    from spoolkit.agent.loop import AgentLoop
    from spoolkit.config import Config
    from spoolkit.llm.fake import FakeModel
    from spoolkit.llm.tokenizer import OfflineTokenCounter
    from spoolkit.tools.registry import ToolRegistry

    assert isinstance(tmp_path, Path)
    control = (
        '{"thought":"直接答","tool_calls":[],"done":true,'
        '"summary":"窗口就是长度。"}'
    )
    notes: list[dict] = []
    gateway = FakeModel(
        script=[control, "# 答\n\n窗口就是长度。"],
        tokenizer=OfflineTokenCounter(),
    )
    loop = AgentLoop(
        gateway,
        OfflineTokenCounter(),
        ToolRegistry(),
        Config(
            project_root=tmp_path,
            context_window=8192,
            max_steps=8,
            supervise=False,
            overrides={"protocol_v2": 1},
        ),
        compaction=False,
        on_event=lambda _kind, data: notes.append(data),
    )
    result = loop.run("什么是上下文窗口")
    assert result.finished is True
    assert loop._evidence_gaps == 1
    assert any(item.get("kind") == EVIDENCE_GAPS_KIND for item in notes)
    assert not any("退回" in line for line in result.trace)
    assert gateway.requests[1].response_schema is None


def test_实时问题第二次仍无引用则放行(tmp_path) -> None:
    from spoolkit.agent.audit import EVIDENCE_GAPS_KIND
    from spoolkit.agent.loop import AgentLoop
    from spoolkit.config import Config
    from spoolkit.llm.fake import FakeModel
    from spoolkit.llm.tokenizer import OfflineTokenCounter
    from spoolkit.tools.registry import ToolRegistry

    control = (
        '{"thought":"没有查到","tool_calls":[],"done":true,'
        '"summary":"还不知道价格。"}'
    )
    notes: list[dict] = []
    gateway = FakeModel(
        script=[control, control, "# 答\n\n价格未知。"],
        tokenizer=OfflineTokenCounter(),
    )
    loop = AgentLoop(
        gateway,
        OfflineTokenCounter(),
        ToolRegistry(),
        Config(
            project_root=tmp_path,
            context_window=8192,
            max_steps=8,
            supervise=False,
            overrides={"protocol_v2": 1},
        ),
        compaction=False,
        on_event=lambda _kind, data: notes.append(data),
    )
    result = loop.run("今天的价格是多少")
    assert result.finished is True
    assert sum("退回" in line for line in result.trace) == 1
    assert loop._evidence_gaps == 1
    assert any(item.get("kind") == EVIDENCE_GAPS_KIND for item in notes)
    assert gateway.requests[2].response_schema is None
