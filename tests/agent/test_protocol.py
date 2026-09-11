import json

from agents_dev.agent.protocol import AgentTurn, ParseFailure, parse_turn


def _payload(**overrides) -> str:
    base = {
        "thought": "先读文件",
        "tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}],
        "state": {"current": "读 a.py"},
        "done": False,
        "final": None,
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_解析正常输出() -> None:
    turn = parse_turn(_payload())
    assert isinstance(turn, AgentTurn)
    assert turn.thought == "先读文件"
    assert turn.tool_calls[0].name == "read_file"
    assert turn.state_delta is not None
    assert turn.state_delta.current == "读 a.py"
    assert turn.final is None


def test_解析最终答复() -> None:
    turn = parse_turn(_payload(tool_calls=[], done=True, final="完成了"))
    assert isinstance(turn, AgentTurn)
    assert turn.final == "完成了"
    assert turn.tool_calls == ()
    assert turn.done is True


def test_无状态块时状态增量为空() -> None:
    raw = json.dumps(
        {"thought": "t", "tool_calls": [], "done": True, "final": "ok"},
        ensure_ascii=False,
    )
    turn = parse_turn(raw)
    assert isinstance(turn, AgentTurn)
    assert turn.state_delta is None


def test_非JSON返回解析失败而非抛错() -> None:
    assert isinstance(parse_turn("不是 JSON"), ParseFailure)


def test_缺少thought字段返回解析失败() -> None:
    raw = json.dumps({"tool_calls": [], "done": False, "final": None}, ensure_ascii=False)
    assert isinstance(parse_turn(raw), ParseFailure)


def test_工具调用缺名字返回解析失败() -> None:
    assert isinstance(parse_turn(_payload(tool_calls=[{"arguments": {}}])), ParseFailure)


def test_工具调用参数不是对象返回解析失败() -> None:
    raw = _payload(tool_calls=[{"name": "read_file", "arguments": "a.py"}])
    assert isinstance(parse_turn(raw), ParseFailure)


def test_缺少done字段返回解析失败() -> None:
    raw = json.dumps(
        {"thought": "t", "tool_calls": [], "final": None}, ensure_ascii=False
    )
    assert isinstance(parse_turn(raw), ParseFailure)


def test_未完成又没调用工具返回失败() -> None:
    assert isinstance(
        parse_turn(_payload(tool_calls=[], done=False, final=None)), ParseFailure
    )


def test_标记完成但答复为空返回失败() -> None:
    assert isinstance(parse_turn(_payload(tool_calls=[], done=True, final="")), ParseFailure)


def test_标记完成但答复缺失返回失败() -> None:
    raw = json.dumps(
        {"thought": "t", "tool_calls": [], "done": True, "final": None},
        ensure_ascii=False,
    )
    assert isinstance(parse_turn(raw), ParseFailure)


def test_失败信息说明原因() -> None:
    failure = parse_turn("不是 JSON")
    assert isinstance(failure, ParseFailure)
    assert failure.reason


def test_输出被截断时提示出来() -> None:
    failure = parse_turn('{"thought":"正在想","tool_calls":[{"name":"read_file"')
    assert isinstance(failure, ParseFailure)
    assert "截断" in failure.reason


def test_状态块字段被正确传入增量() -> None:
    raw = _payload(state={"done_added": ["第一步"], "excluded_added": ["方案A"]})
    turn = parse_turn(raw)
    assert isinstance(turn, AgentTurn)
    assert turn.state_delta is not None
    assert turn.state_delta.done_added == ["第一步"]
    assert turn.state_delta.excluded_added == ["方案A"]


def test_状态块含未知字段返回失败() -> None:
    assert isinstance(parse_turn(_payload(state={"不存在": 1})), ParseFailure)


def test_空回合被单独标记() -> None:
    """空回合不是「格式写坏了」，是协议里没有模型想说的那件事。

    实测模型想表达「改动提完了，等用户确认」，只能发出一个空回合；
    如果把它当成普通格式错误去纠正，它会原样再来十几次。
    """
    failure = parse_turn(_payload(tool_calls=[], done=False))
    assert isinstance(failure, ParseFailure)
    assert failure.kind == "empty_turn"


def test_其它解析失败不带空回合标记() -> None:
    assert parse_turn("不是 JSON").kind == ""

