"""episode.outcome 的口径，以及重复护栏不再留死入口。"""

from spoolkit.agent.loop import AgentLoop, outcome_for


def test_验收红不许记成success() -> None:
    assert outcome_for(True, False, False) == "fail"


def test_完成且验收过或没跑验证记成success() -> None:
    assert outcome_for(True, False, True) == "success"
    assert outcome_for(True, False, None) == "success"


def test_审查拦住或没完成不许记成success() -> None:
    assert outcome_for(True, True, True) == "fail"
    assert outcome_for(False, False, True) == "fail"


def test_refresh_reason没有这个方法() -> None:
    assert not hasattr(AgentLoop, "_refresh_reason")
