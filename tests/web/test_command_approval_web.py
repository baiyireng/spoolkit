"""网页壳那条路也要能批"命令授权"。

和聊天通道同一条事件通道，但走的是 `web.runner.Runner`：它拼命令行、页面上的
"应用/拒绝"按钮调 `runner.confirm(apply)`。原先它拼的命令里没有 `--ask-on-stdin`，
所以子进程在 events 模式下**一律拒绝**——网页上也点不了。

这条测试直接跑真子进程 + 假模型，量的是"点一下按钮，命令真的执行了没有"。
"""

import json
import time
from pathlib import Path

from spoolkit.web.runner import Runner

ROOT = Path(__file__).resolve().parents[1]


def _script(tmp_path: Path) -> Path:
    script = [
        json.dumps(
            {
                "thought": "跑一条白名单外的命令",
                "tool_calls": [{"name": "run_command", "arguments": {"command": ["whoami"]}}],
                "state": None,
                "done": False,
                "final": None,
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {"thought": "完成", "tool_calls": [], "state": None, "done": True,
             "final": "跑完了"},
            ensure_ascii=False,
        ),
    ]
    path = tmp_path / "script.json"
    path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    return path


def _wait(runner: Runner, predicate, seconds: float = 60.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        snapshot = runner.snapshot()
        if predicate(snapshot):
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"等超时了：{runner.snapshot()}")


def test_页面点应用之后命令真的执行(tmp_path: Path) -> None:
    script = _script(tmp_path)
    runner = Runner(
        tmp_path,
        session="web-approve",
        extra_args=(
            "--provider", "fake",
            "--script", str(script),
            "--root", str(tmp_path),
            "--no-memory",
            "--max-steps", "3",
        ),
    )
    assert runner.start("试试执行命令") is True

    snapshot = _wait(runner, lambda s: s["awaiting"] > 0 or s["finished"])
    assert snapshot["awaiting"] > 0, f"没有把授权请求发到页面：{snapshot}"
    assert snapshot["awaiting_detail"].get("command") == "whoami"

    assert runner.confirm(True) is True      # 页面上的"应用"按钮

    _wait(runner, lambda s: s["finished"])
    tool_events = [
        event for event in runner.events()
        if event.get("type") == "tool" and event.get("name") == "run_command"
    ]
    assert tool_events, "命令没有被调用"
    assert tool_events[0].get("ok") is True
    assert "退出码 0" in tool_events[0].get("detail", "")


def test_页面点拒绝时命令不执行(tmp_path: Path) -> None:
    script = _script(tmp_path)
    runner = Runner(
        tmp_path,
        session="web-deny",
        extra_args=(
            "--provider", "fake",
            "--script", str(script),
            "--root", str(tmp_path),
            "--no-memory",
            "--max-steps", "3",
        ),
    )
    runner.start("试试执行命令")
    _wait(runner, lambda s: s["awaiting"] > 0 or s["finished"])

    assert runner.confirm(False) is True

    _wait(runner, lambda s: s["finished"])
    tool_events = [
        event for event in runner.events()
        if event.get("type") == "tool" and event.get("name") == "run_command"
    ]
    assert tool_events and tool_events[0].get("ok") is False, tool_events


def test_页面要显示被问的那条命令() -> None:
    """按钮上方得摆出"要执行什么、为什么"——否则用户是在盲批。"""
    from spoolkit.web.page import HTML

    assert "showCommandAsk" in HTML
    assert "要执行：" in HTML
    assert "awaiting_detail" in HTML
