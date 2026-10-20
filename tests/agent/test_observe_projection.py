"""循环按工具投影：读仍走卡/正文，搜和跑不把 raw 拼进提示。"""

import json
import sys
from pathlib import Path

from spoolkit.agent.loop import AgentLoop, project_run_result
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.exec import run_command_spec
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.search import search_code_spec
from spoolkit.tools.types import ToolResult
from spoolkit.tools.verify import verify_file_spec


def test_模型看见卡和摘要看不见整份raw(tmp_path: Path) -> None:
    big = "\n".join(
        "UNIQUE_BIG_BODY" if index == 50 else f"x = {index}" for index in range(100)
    ) + "\n"
    (tmp_path / "big.py").write_text(big, encoding="utf-8")
    (tmp_path / "small.py").write_text("UNIQUE_SMALL_BODY = 1\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")
    for index in range(8):
        (tmp_path / f"f{index}.py").write_text(f"HIT_f{index}_TOKEN\n", encoding="utf-8")
    stderr_lines = ["ERROR_KEY_LINE boom"]
    stderr_lines += [f"noise {index}" for index in range(1, 99)]
    stderr_lines.append("LAST_CONTEXT_LINE")
    script = tmp_path / "boom.py"
    script.write_text(
        "import sys\n"
        + "".join(f"print({line!r}, file=sys.stderr)\n" for line in stderr_lines)
        + "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(search_code_spec(tmp_path))
    registry.register(run_command_spec(tmp_path))
    registry.register(verify_file_spec(tmp_path))
    calls = [
        {"name": "read_file", "arguments": {"path": "big.py"}},
        {"name": "read_file", "arguments": {"path": "small.py"}},
        {"name": "search_code", "arguments": {"pattern": "HIT_"}},
        {"name": "run_command", "arguments": {"command": [sys.executable, str(script)]}},
        {"name": "verify_file", "arguments": {"path": "ok.py"}},
    ]
    turns = [
        json.dumps(
            {"thought": "先看", "tool_calls": calls, "done": False, "final": ""},
            ensure_ascii=False,
        ),
        json.dumps(
            {"thought": "收尾", "tool_calls": [], "done": True, "final": "看完了"},
            ensure_ascii=False,
        ),
    ]
    tokenizer = OfflineTokenCounter()
    loop = AgentLoop(
        gateway=FakeModel(script=turns, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=8192,
            max_steps=4,
            supervise=False,
        ),
        pending=None,
    )
    loop.run("看这些文件")
    shown = "\n".join(
        message.content
        for request in loop.gateway.requests
        for message in request.messages
        if message.role == "tool"
    )
    assert "已记入观察" in shown
    assert "全文不放入对话" in shown
    assert "UNIQUE_BIG_BODY" not in shown
    assert "UNIQUE_SMALL_BODY" in shown
    assert "HIT_f0_TOKEN" in shown or "HIT_f1_TOKEN" in shown
    assert ":1:" in shown
    search = search_code_spec(tmp_path).handler({"pattern": "HIT_"})
    covered = [hit for hit in search.data["hits"] if hit["text"] in search.summary]
    assert len(search.data["hits"]) == 8
    assert len(covered) >= 5 or "摘要已截断" in search.summary
    hidden = next(hit["text"] for hit in search.data["hits"] if hit["text"] not in search.summary)
    assert hidden not in shown
    assert search.raw not in shown
    assert "ERROR_KEY_LINE boom" in shown
    assert "LAST_CONTEXT_LINE" in shown
    assert "noise 50" not in shown
    assert "仅保留关键片段" in shown
    assert "没有发现问题" in shown
    assert "Python 语法编译：通过" not in shown


def test_试跑副本提示留在失败摘要后面() -> None:
    summary = "退出码 1。ERROR_KEY_LINE boom\nLAST_CONTEXT_LINE\n仅保留关键片段"
    content = (
        "（本次命令在**试跑副本**里执行：真实工作区全程未被改动。）\n"
        + summary
        + "\n"
        + ("noise\n" * 100)
    )
    shown = project_run_result(
        ToolResult(ok=False, content=content, summary=summary, raw="stdout:\n" + ("noise\n" * 100))
    )
    assert "试跑副本" in shown
    assert "ERROR_KEY_LINE boom" in shown
    assert "LAST_CONTEXT_LINE" in shown
    assert "noise\nnoise" not in shown
