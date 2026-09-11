"""强制收敛：连续只查看不修改时，把「继续查」从语法里拿掉。

实测这套模型对文字提醒完全免疫——重复提醒、拦截说明都照发不误。
能推得动它的只有「这一轮物理上只能选什么」，所以这里断言的是发给模型的
schema，而不是提示词里写了什么。
"""

import json
from pathlib import Path

from agents_dev.agent.loop import NO_EDIT_LIMIT, AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.edit import PendingChanges, replace_lines_spec, write_file_spec
from agents_dev.tools.fs import read_file_spec
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.types import ToolResult


def _turn(thought: str, calls=None, final=None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _read() -> dict:
    return {"name": "read_file", "arguments": {"path": "a.py"}}


def _edit() -> dict:
    return {"name": "write_file", "arguments": {"path": "a.py", "content": "x = 1\n"}}


def _build(tmp_path: Path, script: list, writable: bool = True) -> AgentLoop:
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    if writable:
        pending = PendingChanges(tmp_path)
        registry.register(write_file_spec(tmp_path, pending))
        registry.register(replace_lines_spec(tmp_path, pending))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, context_window=4096, max_steps=12),
    )


def _allowed(loop: AgentLoop, index: int) -> list[str]:
    """第 index 次请求里，模型被允许调用的工具名。"""
    schema = loop.gateway.requests[index].response_schema
    items = schema["properties"]["tool_calls"]["items"]
    return list(items["properties"]["name"].get("enum", []))


def test_连续只查看后收窄为只能写(tmp_path: Path) -> None:
    script = [_turn("看", [_read()]) for _ in range(NO_EDIT_LIMIT)]
    script += [_turn("改", [_edit()]), _turn("完成", final="改好了")]
    loop = _build(tmp_path, script)
    loop.run("改一下")

    assert "read_file" in _allowed(loop, 0)
    for i in range(NO_EDIT_LIMIT):
        assert "read_file" in _allowed(loop, i), f"第 {i} 轮不该收窄"
    forced = _allowed(loop, NO_EDIT_LIMIT)
    assert set(forced) == {"write_file", "replace_lines"}, forced
    # 提出改动之后计数清零，下一轮恢复全部工具
    assert "read_file" in _allowed(loop, NO_EDIT_LIMIT + 1)


def test_中途提过改动就不会触发收窄(tmp_path: Path) -> None:
    script = [_turn("看", [_read()]) for _ in range(NO_EDIT_LIMIT)]
    script.insert(2, _turn("改", [_edit()]))
    script += [_turn("完成", final="好了")]
    loop = _build(tmp_path, script)
    loop.run("改一下")

    for i in range(len(loop.gateway.requests)):
        assert "read_file" in _allowed(loop, i), f"第 {i} 轮不该收窄"


def test_只读角色没有收窄这回事(tmp_path: Path) -> None:
    """没有写工具时收窄成空集合，模型将无路可走——所以压根不该有这条路。"""
    script = [_turn("看", [_read()]) for _ in range(NO_EDIT_LIMIT + 2)]
    script.append(_turn("完成", final="看完了"))
    loop = _build(tmp_path, script, writable=False)
    assert loop._commit_schema is None
    loop.run("看看")
    for i in range(len(loop.gateway.requests)):
        assert "read_file" in _allowed(loop, i)


def test_收窄时说明原因(tmp_path: Path) -> None:
    """收窄而不说明，模型只会看到工具忽然变少。"""
    script = [_turn("看", [_read()]) for _ in range(NO_EDIT_LIMIT)]
    script += [_turn("改", [_edit()]), _turn("完成", final="好了")]
    loop = _build(tmp_path, script)
    loop.run("改一下")

    last = loop.gateway.requests[NO_EDIT_LIMIT]
    text = "\n".join(message.content for message in last.messages)
    assert "没有提出任何改动" in text


def test_自动验证的结果会回灌给模型(tmp_path: Path) -> None:
    """模型不会自己去跑测试，所以跑完必须把结论放到它面前。"""
    script = [_turn("改", [_edit()]), _turn("完成", final="好了")]
    loop = _build(tmp_path, script)
    calls = []

    def verify() -> ToolResult:
        calls.append(1)
        return ToolResult(ok=False, content="test_v 失败：assert 1 == 2")

    loop.verify = verify
    loop.run("改一下")

    assert len(calls) == 1
    prompt = _text_of(loop.gateway.requests[-1])
    assert "自动跑了一遍项目里的测试" in prompt
    assert "assert 1 == 2" in prompt


def test_没有改动就不验证(tmp_path: Path) -> None:
    """只查看的那几步不该顺带跑测试——那是白花时间。"""
    script = [_turn("看", [_read()]), _turn("完成", final="看完了")]
    loop = _build(tmp_path, script)
    calls = []
    loop.verify = lambda: calls.append(1) or ToolResult(ok=True, content="通过")
    loop.run("看看")
    assert calls == []


def _text_of(request) -> str:
    return "\n".join(message.content for message in request.messages)


def test_空回合的反馈是命令式的(tmp_path: Path) -> None:
    """模型卡在「等用户确认」的空回合里时，得告诉它下一步做什么。

    实测它收到「请只输出规定的 JSON」之后，会原样再发十几次同一个空回合。
    """
    empty = json.dumps(
        {"thought": "等用户确认", "tool_calls": [], "done": False, "final": None},
        ensure_ascii=False,
    )
    loop = _build(tmp_path, [empty, empty, _turn("算了", final="结束")])
    loop.run("改一下")

    prompt = _text_of(loop.gateway.requests[1])
    assert "不要等用户确认" in prompt
    assert "done 设为 true" in prompt


def test_连续空回合会提前收尾(tmp_path: Path) -> None:
    """它不是在思考，是在复读。实测连发 11 次，把预算全烧在复读上。"""
    empty = json.dumps(
        {"thought": "等用户确认", "tool_calls": [], "done": False, "final": None},
        ensure_ascii=False,
    )
    script = [_turn("改", [_edit()])] + [empty] * 6
    loop = _build(tmp_path, script)
    result = loop.run("改一下")

    assert result.finished is False
    assert "停住了" in result.final
    # 只跑了「一次改动 + 两次空回合」，没有把 12 步预算烧光
    assert result.steps <= 4


def test_偶发空回合不会立刻收尾(tmp_path: Path) -> None:
    """一次空回合可能只是格式抖动，别一碰就停。"""
    empty = json.dumps(
        {"thought": "嗯", "tool_calls": [], "done": False, "final": None},
        ensure_ascii=False,
    )
    script = [empty, _turn("改", [_edit()]), _turn("完成", final="好了")]
    loop = _build(tmp_path, script)
    result = loop.run("改一下")
    assert result.finished is True
