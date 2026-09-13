"""同一个循环跑两次，第二次不带第一次的历史。

这条是「对话式使用」能不能成立的前提：`chat` 每轮都调用 `loop.run()`，
而**上下文必须每轮重置**——上一轮的往返（包括它读过的文件内容）不进这一轮。
状态靠外置的进度与记忆延续，不靠把历史塞回上下文。

混为一谈会得出"那就把历史塞回上下文吧"的结论，而那会把这个项目的支点
（短上下文）直接推翻，所以这里用一条测试把它钉死。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import register_edit_tools
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.edit import PendingChanges


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "收尾", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _loop(tmp_path: Path, script: list[str]) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    register_edit_tools(registry, tmp_path, pending)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=registry,
        config=Config(project_root=tmp_path, context_window=4096),
    )


def test_第二次run不带第一次的往返(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("SECRET_MARKER = 1\n", encoding="utf-8")
    loop = _loop(tmp_path, [_turn("第一次答复"), _turn("第二次答复")])

    loop.run("读一下 a.py")
    loop.run("另一件事")

    second = loop.gateway.requests[-1]
    joined = "\n".join(message.content for message in second.messages)
    assert "另一件事" in joined
    assert "第一次答复" not in joined
