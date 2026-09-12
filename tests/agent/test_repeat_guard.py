"""原地打转的检测。

小模型在短上下文里失去方向时会反复做同一个动作，而且不会自己停。
回归集里实测本地 Qwen2.5-Coder-7B 连续 10 次调用同一个 find_callers、
参数一字不差，把 12 步预算全烧光。这组测试盯的就是那种循环。
"""

import json
from pathlib import Path

from agents_dev.agent.loop import REPEAT_BLOCK_AT, AgentLoop, call_signature
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.fs import list_dir_spec, read_file_spec
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.types import ToolCall


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


def _build(tmp_path: Path, script: list) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(list_dir_spec(tmp_path))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        # 这个文件测的是**机械层**：拦截发生在调用之前，判据是签名。
        # 督导会在打转升级时插一次调用，那是另一条路——它有自己的测试文件，
        # 插进来只会把脚本顺序搅乱。
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=10,
            supervise=False,
        ),
    )


def _last_prompt(loop: AgentLoop) -> str:
    """模型最后一次实际读到的内容。"""
    return "\n".join(m.content for m in loop.gateway.requests[-1].messages)


def _read(path: str = "a.txt") -> dict:
    return {"name": "read_file", "arguments": {"path": path}}


def test_参数顺序不同仍算同一次调用() -> None:
    """模型换个字段顺序就能绕过检测的话，这道防线等于没有。"""
    assert call_signature(ToolCall("t", {"x": 1, "y": 2})) == call_signature(
        ToolCall("t", {"y": 2, "x": 1})
    )
    assert call_signature(ToolCall("t", {"x": 1})) != call_signature(
        ToolCall("t", {"x": 2})
    )
    assert call_signature(ToolCall("t", {"x": 1})) != call_signature(
        ToolCall("u", {"x": 1})
    )


def test_第二次相同调用会提醒但仍然执行(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读", [_read()]),
            _turn("再读一次", [_read()]),
            _turn("看完了", final="里面有内容"),
        ],
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    loop.run("看看 a.txt")

    assert len(executed) == 2, "第二次应当照常执行"
    assert "完全相同" in _last_prompt(loop)


def test_连续第三次不再执行(tmp_path: Path) -> None:
    """中间没有别的动作，结果不可能变——再跑一遍纯属烧预算。"""
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读", [_read()]),
            _turn("再读", [_read()]),
            _turn("还读", [_read()]),
            _turn("算了", final="结束"),
        ],
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("看看 a.txt")

    assert REPEAT_BLOCK_AT == 3
    assert len(executed) == 2
    assert "没有执行" in _last_prompt(loop)
    assert any("重复调用第 3 次" in line for line in result.trace)


def test_中间插入别的调用后计数清零(tmp_path: Path) -> None:
    """读文件 → 看目录 → 再读同一个文件，是正常的，不该被拦。"""
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读", [_read()]),
            _turn("列目录", [{"name": "list_dir", "arguments": {"path": "."}}]),
            _turn("再读", [_read()]),
            _turn("完成", final="好"),
        ],
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    loop.run("看看 a.txt")

    assert len(executed) == 3
    assert "完全相同" not in _last_prompt(loop)


def test_交替打转也会被拦住(tmp_path: Path) -> None:
    """A、B、A、B… 每一步都和上一步不同，只看「连续相同」会全漏掉。

    实测审查者用 git status / git diff 交替复读了 11 步，一次都没被拦。
    """
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("内容\n", encoding="utf-8")
    pair = [
        _turn("看 a", [_read("a.txt")]),
        _turn("看 b", [_read("b.txt")]),
    ]
    loop = _build(tmp_path, pair * 4 + [_turn("算了", final="结束")])
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.arguments.get("path", ""))
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("来回看")

    # 12 轮里每个文件最多真正读 2 次，第三次起被拦
    assert executed.count("a.txt") <= 2
    assert any("重复调用" in line for line in result.trace)
