"""编辑工具连续失败时注入格式提示。

dogfood 暴露：write_file/replace_text 连续失败后模型只会原样重试，
不告诉它正确格式就死循环（贪吃蛇第 2 步连续 4 次 write_file 失败）。
修复：连续失败 ≥2 次时往工具输出里追加格式提示。
"""
import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import replace_text_spec
from spoolkit.tools.registry import ToolRegistry


def _turn(calls, final=None):
    return json.dumps(
        {
            "thought": "动手",
            "tool_calls": calls,
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _replace(path, old, new):
    return {"name": "replace_text", "arguments": {"path": path, "old": old, "new": new}}


def _loop(tmp_path: Path, script: list) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(replace_text_spec(tmp_path, __import__("spoolkit.tools.edit", fromlist=["PendingChanges"]).PendingChanges(tmp_path)))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, context_window=4096),
    )


def test_连续失败2次注入格式提示(tmp_path: Path) -> None:
    """replace_text 连续失败 2 次，第 2 次的输出里应该有格式提示。"""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    # 用文件里不存在的 old 片段，连续失败 2 次，第 3 次收尾
    loop = _loop(
        tmp_path,
        [
            _turn([_replace("app.py", "不存在的内容A", "new")]),
            _turn([_replace("app.py", "不存在的内容B", "new")]),
            _turn([], final="放弃了"),
        ],
    )
    result = loop.run("改 app.py")

    # trace 里应该有注入提示的记录
    trace_text = "\n".join(result.trace)
    assert "已注入格式提示" in trace_text
    assert "连续失败 2 次" in trace_text


def test_成功后计数清零(tmp_path: Path) -> None:
    """失败 1 次后成功，计数清零；再失败 1 次不触发提示。"""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    # 第 1 次：失败（old 不存在）
    # 第 2 次：成功（old 存在）
    # 第 3 次：失败 1 次（计数刚清零，不触发）
    loop = _loop(
        tmp_path,
        [
            _turn([_replace("app.py", "不存在", "y = 2")]),
            _turn([_replace("app.py", "x = 1", "y = 2")]),
            _turn([_replace("app.py", "又不存在", "z = 3")]),
            _turn([], final="好了"),
        ],
    )
    result = loop.run("改 app.py")

    trace_text = "\n".join(result.trace)
    # 失败 1 次不触发提示（阈值是 2）
    assert "已注入格式提示" not in trace_text
