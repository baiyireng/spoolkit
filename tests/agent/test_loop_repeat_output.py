"""历史里的重复内容不再重发。

预取（锚定 + 关键词）已经把这一步要改的文件正文放进提示词了，模型经常还是
先 `read_file` 看一遍——同一段正文于是在提示词里出现两次，而**后续每一次
调用**都带着这两份。实测 50 题那轮，工具输出就是读文件的内容，原样留在
最近几轮里反复重发。

去重是「正文完整出现过」才动手：相同正文仍在提示词里（预取或上一轮），
所以不丢信息——只是不再占第二份位置。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop, _is_repeat_output
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry

长正文 = "def average(values):\n    return sum(values) / len(values)\n" + "# 填充\n" * 60


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


def _loop(tmp_path: Path, script: list, prefetch=None) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, context_window=4096),
        prefetch=prefetch,
    )


def test_短输出不去重() -> None:
    """短内容本来就不值钱，比对反而增加噪音。"""
    seen: set[str] = set()
    assert _is_repeat_output("ok", "", seen) is False
    assert _is_repeat_output("ok", "", seen) is False


def test_预取里已有同样正文时不再重发() -> None:
    seen: set[str] = set()
    assert _is_repeat_output(长正文, "以下是相关文件当前的内容：\n" + 长正文, seen) is True


def test_本轮第二遍读到同一份内容时不再重发() -> None:
    seen: set[str] = set()
    assert _is_repeat_output(长正文, "", seen) is False
    assert _is_repeat_output(长正文, "", seen) is True


def test_读到的内容与预取重复时_历史里只留一行说明(tmp_path: Path) -> None:
    (tmp_path / "stats.py").write_text(长正文, encoding="utf-8")
    loop = _loop(
        tmp_path,
        [
            _turn("读一眼", [{"name": "read_file", "arguments": {"path": "stats.py"}}]),
            _turn("好了", final="看过了"),
        ],
        prefetch=lambda _goal: "以下是相关文件当前的内容：\nstats.py\n" + 长正文,
    )

    result = loop.run("改 stats.py")

    assert result.finished is True
    tool_messages = [
        message.content
        for request in loop.gateway.requests
        for message in request.messages
        if message.role == "tool"
    ]
    assert tool_messages, "应该有一条工具结果进历史"
    assert "上面已经给过" in tool_messages[-1]
    # 正文本身还在提示词里（预取那一份），只是没有第二份
    assert "填充" not in tool_messages[-1]
