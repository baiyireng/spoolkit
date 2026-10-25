"""ChatSink 只接收已经接受的节正文。"""

from spoolkit.agent.chat_sink import RecorderSink
from spoolkit.agent.document_bodies import InMemoryBodyStore, section_ref
from spoolkit.agent.document_state import DocumentState, OutlineEntry
from spoolkit.agent.longform import LongFormRunner
from spoolkit.llm.types import ChatRequest, ChatResponse

TITLES = ("窗口", "估算", "余量")


class ScriptedGateway:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    def chat(self, request: ChatRequest) -> ChatResponse:
        if not self._replies:
            raise AssertionError("脚本里的模型回复不够")
        text = self._replies.pop(0)
        return ChatResponse(text=text, prompt_tokens=10, completion_tokens=len(text))

    def context_window(self) -> int:
        return 8192


class RaisingSink:
    def __init__(self) -> None:
        self.calls = 0

    def start(self, state: DocumentState) -> None:
        self.calls += 1
        raise RuntimeError("start")

    def section_start(self, index: int, title: str) -> None:
        self.calls += 1
        raise RuntimeError("section_start")

    def section_commit(self, index: int, body: str) -> None:
        self.calls += 1
        raise RuntimeError("section_commit")

    def section_replace(self, index: int, body: str) -> None:
        self.calls += 1
        raise RuntimeError("section_replace")

    def progress(self, phase: str, index: int) -> None:
        self.calls += 1
        raise RuntimeError("progress")

    def complete(self, result: object) -> None:
        self.calls += 1
        raise RuntimeError("complete")

    def fail(self, reasons: tuple[str, ...]) -> None:
        self.calls += 1
        raise RuntimeError("fail")


def _piece(title: str, chars: int = 1000) -> str:
    unit = f"{title}写完。"
    text = unit * ((chars // len(unit)) + 1)
    if len(text) > chars:
        text = text[: chars - 1] + "。"
    return text


def _plan() -> DocumentState:
    outline = tuple(
        OutlineEntry(index=index, title=title, points=f"{title}的要点", target_chars=500)
        for index, title in enumerate(TITLES)
    )
    return DocumentState(
        goal="写一份估算说明",
        outline=outline,
        deliverable="chat",
        phase="plan",
        pending_sections=(0, 1, 2),
    )


def _names(sink: RecorderSink) -> list[object]:
    return [event[0] for event in sink.events]


def _commits(sink: RecorderSink, index: int) -> list[str]:
    return [str(event[2]) for event in sink.events if event[0] == "section_commit" and event[1] == index]


def test_three_sections_commit_once_each_then_complete() -> None:
    sink = RecorderSink()
    runner = LongFormRunner(
        _plan(),
        ScriptedGateway([_piece(title) for title in TITLES] + ["完"]),
        InMemoryBodyStore(),
        sink,
    )
    result = runner.run()
    assert result.done
    assert _names(sink) == [
        "start",
        "section_start",
        "progress",
        "section_commit",
        "section_start",
        "progress",
        "section_commit",
        "section_start",
        "progress",
        "section_commit",
        "progress",
        "progress",
        "complete",
    ]
    assert [event[1:] for event in sink.events if event[0] == "section_start"] == [
        (0, "窗口"),
        (1, "估算"),
        (2, "余量"),
    ]
    assert [event[1] for event in sink.events if event[0] == "progress"] == [
        step.phase for step in result.steps
    ]
    assert sink.events[-1] == ("complete", True, "完")
    for index, title in enumerate(TITLES):
        assert _commits(sink, index) == [runner.bodies.get(section_ref(index))]
        assert _commits(sink, index)[0].startswith(f"{title}写完。")


def test_rejected_think_and_scaffold_are_not_committed() -> None:
    sink = RecorderSink()
    runner = LongFormRunner(
        _plan(),
        ScriptedGateway(
            [
                "<think>先分析一下</think>\n窗口不该留下思考。",
                _piece("窗口"),
                "要点：把提示再写一遍。\n估算不该留下标签。",
                _piece("估算"),
                _piece("余量"),
                "完",
            ]
        ),
        InMemoryBodyStore(),
        sink,
    )
    result = runner.run()
    assert result.done
    blob = "\n".join(_commits(sink, index)[0] for index in range(3))
    assert "<think>" not in blob
    assert "要点：" not in blob
    assert all(len(_commits(sink, index)) == 1 for index in range(3))


def test_repair_commits_only_the_final_body() -> None:
    sink = RecorderSink()
    runner = LongFormRunner(
        _plan(),
        ScriptedGateway(
            ["窗口先收一句。", _piece("窗口补足"), _piece("估算"), _piece("余量"), "完"]
        ),
        InMemoryBodyStore(),
        sink,
    )
    result = runner.run()
    assert result.done
    assert result.state.repair_attempts[0] == 1
    committed = _commits(sink, 0)
    assert len(committed) == 1
    assert "窗口先收一句。" in committed[0]
    assert "窗口补足写完。" in committed[0]
    assert committed[0] == runner.bodies.get(section_ref(0))


def test_rewrite_commits_only_the_replacement() -> None:
    sink = RecorderSink()
    replacement = _piece("窗口改写")
    runner = LongFormRunner(
        _plan(),
        ScriptedGateway(
            [
                "窗口先收一句。",
                "补了一点。",
                "又补了一点。",
                replacement,
                _piece("估算"),
                _piece("余量"),
                "完",
            ]
        ),
        InMemoryBodyStore(),
        sink,
    )
    result = runner.run()
    assert result.done
    committed = _commits(sink, 0)
    assert committed == [replacement]
    assert "补了一点。" not in committed[0]
    assert "窗口先收一句。" not in committed[0]


def test_failed_document_calls_fail_not_complete() -> None:
    outline = (OutlineEntry(0, "窗口", "窗口的要点", 500),)
    state = DocumentState(
        goal="写一份估算说明",
        outline=outline,
        deliverable="chat",
        phase="plan",
        pending_sections=(0,),
    )
    sink = RecorderSink()
    result = LongFormRunner(
        state,
        ScriptedGateway(["窗口先收一句。", "补了一点。", "又补了一点。", "改写也没写够。"]),
        InMemoryBodyStore(),
        sink,
    ).run()
    assert not result.done
    assert "complete" not in _names(sink)
    assert sink.events[-1][0] == "fail"
    assert "short_section" in sink.events[-1][1]
    assert "改写也没写够。" not in "\n".join(_commits(sink, 0))


def test_sink_errors_do_not_change_the_run() -> None:
    sink = RaisingSink()
    store = InMemoryBodyStore()
    result = LongFormRunner(
        _plan(),
        ScriptedGateway([_piece(title) for title in TITLES] + ["完"]),
        store,
        sink,
    ).run()
    assert result.done
    assert result.ending == "完"
    assert sink.calls > 0
    assert store.get(section_ref(0)).startswith("窗口写完。")


def test_validate_repair_replaces_the_earlier_commit() -> None:
    """三节都先提交，全文不足后校验重开最后一节；sink 应 replace 而不是再 commit。"""
    sink = RecorderSink()
    first = _piece("窗口", 800)
    second = _piece("估算", 800)
    third = _piece("余量", 800)
    supplement = _piece("余量补充", 700)
    runner = LongFormRunner(
        _plan(),
        ScriptedGateway([first, second, third, supplement, "完"]),
        InMemoryBodyStore(),
        sink,
    )
    result = runner.run()
    assert result.done
    commits = [event for event in sink.events if event[0] == "section_commit"]
    replaces = [event for event in sink.events if event[0] == "section_replace"]
    assert [event[1] for event in commits] == [0, 1, 2]
    assert len(replaces) == 1
    assert replaces[0][1] == 2
    assert third in replaces[0][2]
    assert supplement in replaces[0][2]
    assert replaces[0][2] == runner.bodies.get(section_ref(2))
