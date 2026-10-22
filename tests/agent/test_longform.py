"""显式 LongFormRunner。假网关按脚本回答，正文留在 BodyStore。"""

from spoolkit.agent.document_bodies import InMemoryBodyStore, section_ref
from spoolkit.agent.document_state import (
    TAIL_CAP,
    CurrentSection,
    DocumentState,
    OutlineEntry,
    has_think,
    scaffold_hits,
)
from spoolkit.agent.longform import (
    GUARD_RETRY,
    NOVELTY_RETRY,
    SCAFFOLD_RETRY,
    LongFormRunner,
    finish_messages,
    finalize_messages,
    repair_messages,
    rewrite_messages,
)
from spoolkit.llm.types import ChatRequest, ChatResponse

TITLES = ("窗口", "估算", "余量", "截断", "核对", "做法")
ENDING = "确保推理过程不因溢出而失败。"


class ScriptedGateway:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError("脚本里的模型回复不够")
        text = self._replies.pop(0)
        return ChatResponse(text=text, prompt_tokens=10, completion_tokens=len(text))

    def context_window(self) -> int:
        return 8192


def _piece(title: str, chars: int = 500) -> str:
    unit = f"{title}写完。"
    text = unit * ((chars // len(unit)) + 1)
    if len(text) > chars:
        text = text[: chars - 1] + "。"
    return text


def _marked(title: str, chars: int = 500) -> str:
    chars_list = list(_piece(title, chars))
    token = list("MIDDLETOKEN")
    chars_list[250 : 250 + len(token)] = token
    return "".join(chars_list)


def _outline() -> tuple[OutlineEntry, ...]:
    return tuple(
        OutlineEntry(index=index, title=title, points=f"{title}的要点", target_chars=500)
        for index, title in enumerate(TITLES)
    )


def _state(body: str) -> DocumentState:
    stripped = body.strip()
    return DocumentState(
        goal="写一份估算说明",
        outline=_outline(),
        deliverable="chat",
        current_section=CurrentSection(
            index=0,
            title="窗口",
            summary="",
            tail=stripped[-TAIL_CAP:],
            char_count=len(stripped),
            status="open",
        ),
        pending_sections=(1, 2, 3, 4, 5),
        phase="write",
    )


def _store(body: str) -> InMemoryBodyStore:
    store = InMemoryBodyStore()
    store.put(0, body)
    return store


def _run(body: str, replies: list[str]) -> tuple[LongFormRunner, ScriptedGateway, DocumentState]:
    state = _state(body)
    gateway = ScriptedGateway(replies)
    runner = LongFormRunner(state, gateway, _store(body))
    return runner, gateway, state


def _pending(start: int, chars: int = 500) -> list[str]:
    return [_piece(TITLES[index], chars) for index in range(start, len(TITLES))]


def test_prompts_keep_experiment_wording() -> None:
    finish = "\n".join(item.content for item in finish_messages("窗口", 1, "总需求为"))
    assert "不要写「完」" in finish
    assert "总需求为" in finish
    assert "只从最后一个字之后继续" in finish
    current = "\n".join(
        item.content
        for item in repair_messages(
            title="余量",
            points="窗口余量",
            target_chars=500,
            body="余量写到一半",
            current=True,
        )
    )
    assert "只补完当前节" in current
    assert "目标剩余字数" in current
    pending = "\n".join(
        item.content
        for item in repair_messages(
            title="余量",
            points="窗口余量",
            target_chars=500,
            body="余量写到一半",
            current=False,
        )
    )
    assert "只补完这一节" in pending
    rewritten = "\n".join(
        item.content
        for item in rewrite_messages(
            title="过程",
            points="实际比较了哪些输入",
            previous_summary="目标已经写完",
            target_chars=500,
        )
    )
    assert "重写当前节" in rewritten
    assert "上一节摘要" in rewritten
    assert "目标字数：500" in rewritten
    assert "不要写思考过程" in rewritten
    assert finalize_messages()[-1].content == "各节都已写完。只输出最后一行「完」。不要写其他内容。"
    assert GUARD_RETRY == "只输出中文 Markdown 正文，不要思考过程/英文分析。"
    assert SCAFFOLD_RETRY == "只输出正文，不复述提示词结构。"
    assert NOVELTY_RETRY == "只写新的句子，不要重复已有内容。"


def test_fake_gateway_finishes_one_document() -> None:
    fragment = "窗口写到一半"
    finish = _marked("窗口补")
    runner, gateway, state = _run(fragment, [finish, *_pending(1), "完"])
    snapshot = state.to_json()
    result = runner.run()
    assert state.to_json() == snapshot
    assert [step.phase for step in result.steps] == [
        "finish_current",
        "write_pending",
        "write_pending",
        "write_pending",
        "write_pending",
        "write_pending",
        "validate",
        "finalize",
    ]
    assert result.done
    assert result.ending == "完"
    assert result.reasons == ()
    assert result.state.status == "done"
    assert result.state.phase == "finalize"
    assert result.state.current_section is None
    assert result.state.pending_sections == ()
    assert all(item.artifact_id == "" and item.path == "" for item in result.state.completed_sections)
    blob = result.state.to_json()
    assert "MIDDLETOKEN" not in blob
    assert "MIDDLETOKEN" in runner.bodies.get(section_ref(0))
    assert gateway.requests[0].max_tokens > 0
    assert gateway.requests[0].messages[0].role == "system"
    assert result.state.check_bodies(
        tuple((index, runner.bodies.get(section_ref(index))) for index in range(6))
    ) == ()


def test_current_section_repair() -> None:
    runner, _gateway, _state = _run(
        "窗口写到一半",
        ["窗口先收一句。", _piece("窗口补足"), *_pending(1), "完"],
    )
    result = runner.run()
    assert result.done
    assert [step.phase for step in result.steps[:2]] == ["finish_current", "repair_current"]
    assert result.steps[1].index == 0
    assert result.steps[1].accepted
    assert result.state.repair_attempts[0] == 1
    assert result.state.rewrite_attempts[0] == 0


def test_current_section_rewrite() -> None:
    runner, gateway, _state = _run(
        "窗口写到一半",
        ["窗口先收一句。", "补了一点。", "又补了一点。", _piece("窗口改写"), *_pending(1), "完"],
    )
    result = runner.run()
    assert result.done
    assert [step.phase for step in result.steps[:4]] == [
        "finish_current",
        "repair_current",
        "repair_current",
        "rewrite_current",
    ]
    assert result.steps[3].accepted
    assert result.state.repair_attempts[0] == 2
    assert result.state.rewrite_attempts[0] == 1
    assert "窗口改写写完。" in runner.bodies.get(section_ref(0))
    assert "重写当前节" in gateway.requests[3].messages[-1].content


def test_pending_section_repair() -> None:
    runner, _gateway, _state = _run(
        _piece("窗口"),
        ["估算只有一句。", _piece("估算补足"), *_pending(2), "完"],
    )
    result = runner.run()
    assert result.done
    repair = next(step for step in result.steps if step.phase == "repair_pending")
    assert repair.index == 1
    assert repair.accepted
    assert result.state.repair_attempts[1] == 1


def test_pending_section_rewrite() -> None:
    runner, _gateway, _state = _run(
        _piece("窗口"),
        ["估算只有一句。", "仍然短。", "还是短。", _piece("估算改写"), *_pending(2), "完"],
    )
    result = runner.run()
    assert result.done
    assert [step.phase for step in result.steps[:5]] == [
        "write_pending",
        "repair_pending",
        "repair_pending",
        "rewrite_pending",
        "write_pending",
    ]
    assert result.steps[3].index == 1
    assert result.steps[4].index == 2
    assert result.state.rewrite_attempts[1] == 1


def test_one_section_failure_does_not_block_the_next() -> None:
    runner, _gateway, _state = _run(
        _piece("窗口"),
        ["估算只有一句。", "仍然短。", "还是短。", "改写也没写够。", *_pending(2), "完"],
    )
    result = runner.run()
    assert not result.done
    assert result.ending == ""
    assert "short_section" in result.reasons
    assert [step.phase for step in result.steps].count("write_pending") == 5
    rewrite = next(step for step in result.steps if step.phase == "rewrite_pending")
    later = next(step for step in result.steps if step.phase == "write_pending" and step.index == 2)
    assert result.steps.index(rewrite) < result.steps.index(later)
    assert "finalize" not in [step.phase for step in result.steps]
    assert runner.bodies.get(section_ref(2))
    assert 1 in dict(result.failures)
    assert 2 not in dict(result.failures)


def test_check_bodies_failure_routes_to_repair() -> None:
    body = _piece("窗口", 450)
    runner, _gateway, _state = _run(
        body,
        [*_pending(1, 450), _piece("全文补齐", 600), "完"],
    )
    result = runner.run()
    phases = [step.phase for step in result.steps]
    first = next(step for step in result.steps if step.phase == "validate")
    assert first.accepted is False
    assert "short_doc" in first.reason
    repair = next(step for step in result.steps if step.phase == "repair_pending")
    assert repair.index == 5
    assert phases.index("repair_pending") > phases.index("validate")
    assert result.done
    assert result.reasons == ()
    assert phases[-2:] == ["validate", "finalize"]


def test_done_marker_is_rejected_before_finalize() -> None:
    runner, _gateway, _state = _run(
        "窗口写到一半",
        ["窗口先写一段。\n完\n", _piece("窗口补"), *_pending(1), "完"],
    )
    result = runner.run()
    assert result.done
    assert result.ending == "完"
    for index in range(6):
        lines = runner.bodies.get(section_ref(index)).splitlines()
        assert all(line.strip() != "完" for line in lines)


def test_think_scaffold_and_repetition_stay_out_of_bodies() -> None:
    runner, _gateway, _state = _run(
        "窗口写到一半",
        [
            "<think>先分析一下</think>\n窗口不该留下思考。",
            _piece("窗口补"),
            "要点：把提示再写一遍。\n估算不该留下标签。",
            _piece("估算补"),
            ENDING,
            ENDING,
            _piece("余量补"),
            *_pending(3),
            "完",
        ],
    )
    result = runner.run()
    assert result.done
    for index in range(6):
        body = runner.bodies.get(section_ref(index))
        assert not has_think(body)
        assert scaffold_hits(body) == ()
    assert ENDING not in runner.bodies.get(section_ref(0))
    assert runner.bodies.get(section_ref(2)).count(ENDING) == 1


def test_transitions_replay() -> None:
    replies = [_marked("窗口补"), *_pending(1), "完"]
    first, _gateway, _state = _run("窗口写到一半", replies)
    second, _gateway, _state = _run("窗口写到一半", list(replies))
    left = first.run()
    right = second.run()
    assert left.steps == right.steps
    assert left.state == right.state
    assert left.ending == right.ending
    assert [first.bodies.get(section_ref(index)) for index in range(6)] == [
        second.bodies.get(section_ref(index)) for index in range(6)
    ]
