"""状态块与首轮提示词：短目标 + 整段提示词，各出现一次。

按步执行那条路原先给 `run()` 的是**整段步骤提示词**，而状态块又会打印
`目标: <goal>` ——于是同一段话在每次调用里出现两遍。50 题那轮一次调用
约 2655 token，这一份重复占了其中约 240。
"""

import json
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.registry import ToolRegistry

段落 = "项目目标：把工作区里的题都做对\n本次只做这一步：修复 02_empty_input 的空列表异常"


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "收尾", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _loop(tmp_path: Path) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    return AgentLoop(
        gateway=FakeModel(script=[_turn("好了")], tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=ToolRegistry(),
        config=Config(project_root=tmp_path, context_window=4096),
    )


def test_整段提示词只出现一次(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.run("修复 02_empty_input", prompt=段落)
    messages = loop.gateway.requests[0].messages
    hits = [m for m in messages if 段落 in m.content]
    assert len(hits) == 1


def test_状态块里是短目标(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop.run("修复 02_empty_input", prompt=段落)
    joined = "\n".join(m.content for m in loop.gateway.requests[0].messages)
    assert "目标: 修复 02_empty_input" in joined


def test_不给_prompt_时行为不变(tmp_path: Path) -> None:
    """别的调用点（一次性任务、chat、bench）本来就只给一句目标。"""
    loop = _loop(tmp_path)
    loop.run("看看 a.py")
    joined = "\n".join(m.content for m in loop.gateway.requests[0].messages)
    assert "目标: 看看 a.py" in joined
    assert "看看 a.py" in joined
