"""证据门槛：普通问答不必检索；实时问题必须引用本轮工具结果。"""

from spoolkit.agent.evidence_gate import check_evidence


def test_普通问答写明无需外部信息就可以收() -> None:
    decision = check_evidence("什么是上下文窗口", "无需外部信息。窗口是一次能读的长度。", ())
    assert decision.ok is True
    assert decision.hard is False


def test_普通问答没有依据也不强迫检索() -> None:
    decision = check_evidence("什么是上下文窗口", "窗口就是长度。", ())
    assert decision.ok is False
    assert decision.hard is False
    assert "无需外部信息" in decision.reason
    assert "必须调用" not in decision.reason
    assert "search" not in decision.reason


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
