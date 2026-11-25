"""截断重试时 delta 只发续写，不整段重刷。"""

from spoolkit.agent.loop import AgentLoop


class _Probe:
    """只够调用 `_delta_for_display` / `_on_truncated_retry` 的空壳。"""

    _on_truncated_retry = AgentLoop._on_truncated_retry
    _on_model_delta = AgentLoop._on_model_delta
    _delta_for_display = AgentLoop._delta_for_display

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.on_event = self._capture
        self._final_tap = None

    def _capture(self, kind: str, data: dict) -> None:
        self.events.append((kind, data))

    def _emit(self, kind: str, data: dict) -> None:
        self.on_event(kind, data)


def test_截断重试只发超出前缀的增量() -> None:
    probe = _Probe()
    # 第一轮已上屏
    assert probe._delta_for_display("可行性评估") == "可行性评估"
    assert probe._delta_for_display("与方案") == "与方案"
    assert probe._streamed_final == "可行性评估与方案"

    probe._on_truncated_retry(100, 200)
    assert probe.events[-1][0] == "note"
    assert probe.events[-1][1]["kind"] == "truncation_retry"

    # 重试整段重生成：相同前缀不上屏
    assert probe._delta_for_display("可行性") == ""
    assert probe._delta_for_display("评估与方案") == ""
    # 越过旧稿后只发续写
    assert probe._delta_for_display("（续）") == "（续）"
    assert probe._streamed_final == "可行性评估与方案（续）"
    # 之后按普通增量
    assert probe._delta_for_display("完") == "完"
    assert probe._streamed_final == "可行性评估与方案（续）完"


def test_追赶中分片与已显示完全对齐() -> None:
    probe = _Probe()
    probe._delta_for_display("abc")
    probe._on_truncated_retry(1, 2)
    assert probe._delta_for_display("ab") == ""
    assert probe._delta_for_display("c") == ""
    assert probe._delta_for_display("d") == "d"
