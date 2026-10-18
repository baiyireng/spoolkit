"""ControlTurn v2：控制回合不再携带 final，旧协议仍可解析。"""

import json
import logging

from spoolkit.agent.protocol import (
    AgentTurn,
    ParseFailure,
    TurnProtocol,
    build_turn_schema,
    build_turn_schema_v2,
    parse_turn,
)


class _EmptyRegistry:
    def names(self) -> tuple[str, ...]:
        return ()

    def get(self, name: str):
        return None


def _v2(**overrides) -> str:
    base = {
        "thought": "先读文件",
        "tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}],
        "done": False,
        "summary": "",
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def _legacy_done() -> str:
    return json.dumps(
        {
            "thought": "可以收束",
            "tool_calls": [],
            "done": True,
            "final": "完成了",
        },
        ensure_ascii=False,
    )


def test_v2_schema没有final且有summary() -> None:
    schema = build_turn_schema_v2(_EmptyRegistry())
    assert "final" not in schema["properties"]
    assert "summary" in schema["properties"]
    assert "answer_plan" in schema["properties"]
    assert schema["properties"]["answer_plan"]["properties"]["mode"]["enum"] == [
        "short",
        "long",
    ]
    assert "state" in schema["properties"]
    assert "assessment" in schema["properties"]
    assert schema["required"] == ["thought", "tool_calls", "done"]


def test_旧schema仍包含final() -> None:
    schema = build_turn_schema(_EmptyRegistry())
    assert "final" in schema["properties"]
    assert "summary" not in schema["properties"]


def test_v2完成时收下summary且没有final正文() -> None:
    protocol = TurnProtocol(v2=True)
    turn = protocol.parse(
        _v2(
            tool_calls=[],
            done=True,
            summary="结论：修好了。依据：测试通过。",
            answer_plan={"mode": "short", "target_tokens": 400},
        )
    )
    assert isinstance(turn, AgentTurn)
    assert turn.done is True
    assert turn.final is None
    assert turn.summary == "结论：修好了。依据：测试通过。"
    assert turn.answer_plan is not None
    assert turn.answer_plan.mode == "short"
    assert turn.answer_plan.target_tokens == 400
    assert turn.tool_calls == ()


def test_v2未完成时允许工具且summary为空() -> None:
    turn = TurnProtocol(v2=True).parse(_v2())
    assert isinstance(turn, AgentTurn)
    assert turn.done is False
    assert turn.summary == ""
    assert turn.tool_calls[0].name == "read_file"


def test_v2完成时summary为空失败() -> None:
    failure = TurnProtocol(v2=True).parse(_v2(tool_calls=[], done=True, summary=""))
    assert isinstance(failure, ParseFailure)
    assert "summary" in failure.reason


def test_v2完成时summary超过200字失败() -> None:
    failure = TurnProtocol(v2=True).parse(
        _v2(tool_calls=[], done=True, summary="结" * 201)
    )
    assert isinstance(failure, ParseFailure)
    assert "200" in failure.reason


def test_v2完成时不能再带工具() -> None:
    failure = TurnProtocol(v2=True).parse(
        _v2(done=True, summary="结论：结束。依据：无待办。")
    )
    assert isinstance(failure, ParseFailure)
    assert "tool_calls" in failure.reason


def test_v2未完成时summary必须为空() -> None:
    failure = TurnProtocol(v2=True).parse(_v2(summary="先写一半"))
    assert isinstance(failure, ParseFailure)
    assert "summary" in failure.reason


def test_v2未完成且没有工具失败() -> None:
    failure = TurnProtocol(v2=True).parse(_v2(tool_calls=[], summary=""))
    assert isinstance(failure, ParseFailure)


def test_旧格式先提示v2连续两次才降级(caplog) -> None:
    caplog.set_level(logging.WARNING)
    protocol = TurnProtocol(v2=True)
    first = protocol.parse(_legacy_done())
    assert isinstance(first, ParseFailure)
    assert first.kind == "legacy_format"
    assert "v2" in first.reason
    assert protocol.v2 is True

    second = protocol.parse(_legacy_done())
    assert isinstance(second, AgentTurn)
    assert second.final == "完成了"
    assert protocol.v2 is False
    assert protocol.take_downgrade() is True
    assert protocol.take_downgrade() is False
    assert "final" in protocol.schema(_EmptyRegistry())["properties"]
    assert any("降级" in record.message for record in caplog.records)


def test_中间成功一次就打断连续旧格式() -> None:
    protocol = TurnProtocol(v2=True)
    assert isinstance(protocol.parse(_legacy_done()), ParseFailure)
    ok = protocol.parse(
        _v2(tool_calls=[], done=True, summary="结论：继续用 v2。依据：本轮格式正确。")
    )
    assert isinstance(ok, AgentTurn)
    again = protocol.parse(_legacy_done())
    assert isinstance(again, ParseFailure)
    assert again.kind == "legacy_format"
    assert protocol.v2 is True


def test_关闭v2时解析与旧函数一致() -> None:
    raw = _legacy_done()
    protocol = TurnProtocol(v2=False)
    turn = protocol.parse(raw)
    direct = parse_turn(raw)
    assert isinstance(turn, AgentTurn)
    assert isinstance(direct, AgentTurn)
    assert turn == direct
    assert protocol.schema(_EmptyRegistry()) == build_turn_schema(_EmptyRegistry())
