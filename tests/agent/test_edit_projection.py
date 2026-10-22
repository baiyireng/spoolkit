"""循环把编辑回执投影成 summary，待确认 diff 仍是整份。"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, replace_lines_spec
from spoolkit.tools.registry import ToolRegistry


def test_loop投影不含整份diff且清单里的diff仍完整(tmp_path: Path) -> None:
    text = "".join(f"y = {index}\n" for index in range(400))
    (tmp_path / "big.py").write_text(text, encoding="utf-8")
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(replace_lines_spec(tmp_path, pending))
    call = {
        "name": "replace_lines",
        "arguments": {
            "path": "big.py",
            "start_line": 10,
            "end_line": 11,
            "content": "y = 1000\ny = 1001\n",
            "expect": "y = 9\ny = 10",
        },
    }
    script = [
        json.dumps(
            {"thought": "改两行", "tool_calls": [call], "done": False, "final": ""},
            ensure_ascii=False,
        ),
        json.dumps(
            {"thought": "收尾", "tool_calls": [], "done": True, "final": "改完了"},
            ensure_ascii=False,
        ),
    ]
    tokenizer = OfflineTokenCounter()
    loop = AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=registry,
        config=Config(project_root=tmp_path, context_window=8192, max_steps=4),
        pending=pending,
    )
    loop.run("把 big.py 第 10 到 11 行改掉")

    change = pending.items()[0]
    shown = "\n".join(
        message.content
        for request in loop.gateway.requests
        for message in request.messages
        if message.role == "tool"
    )
    assert "@@" in change.diff
    assert "-y = 9" in change.diff
    assert "+y = 1000" in change.diff
    assert change.diff not in shown
    assert "big.py" in shown
    assert "待确认" in shown
    assert "400→400" in shown
    assert "y = 0" not in shown
