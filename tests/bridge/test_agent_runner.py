"""端到端：一条"聊天消息"真的驱动了一次 agent 运行。

这里**不假造 agent**：`AgentRunner` 起的是真的 `run --events` 子进程，
只是把模型换成假模型（离线、确定）。量的是这条链本身——
消息进去、子进程起来、事件流被读、结局被取回、回复出来。
"""

import json
from pathlib import Path

from agents_dev.bridge.agent_runner import AgentRunner


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "收尾", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _script(tmp_path: Path, answers: list[str]) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
    return path


def test_一条消息换来一段答复(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = _script(tmp_path, [_turn("看过了：a.py 里只有一个 x")])
    runner = AgentRunner(
        tmp_path,
        session="bridge-test",
        extra_args=(
            "--provider", "fake",
            "--script", str(script),
            "--policy", "auto",
            "--no-memory",
            "--max-steps", "2",
        ),
        timeout=120.0,
    )

    answer = runner("看看 a.py")

    assert "看过了" in answer


def test_模型起不来时给一句人话(tmp_path: Path) -> None:
    """子进程崩了也要有答复：聊天通道里"没有反应"是最糟的结果。"""
    runner = AgentRunner(
        tmp_path,
        extra_args=("--provider", "llamacpp", "--base-url", "http://127.0.0.1:1", "--no-memory"),
        timeout=60.0,
    )

    answer = runner("做事")

    assert answer  # 有话说，不是空字符串
    assert "跑" in answer or "失败" in answer or "连不上" in answer or "退出" in answer


def test_超时会回执_而不是干等(tmp_path: Path) -> None:
    """长任务可能跑十几分钟；超时要回一句"我先不等了"，而不是空白。"""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    # 假模型脚本只给一轮，但给两个 turn 让子进程有活干；clock 造一个"已经超时"。
    script = _script(tmp_path, [_turn("一"), _turn("二")])
    ticks = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0])
    runner = AgentRunner(
        tmp_path,
        extra_args=(
            "--provider", "fake", "--script", str(script), "--no-memory", "--max-steps", "2",
        ),
        timeout=10.0,
        clock=lambda: next(ticks, 1000.0),
        sleep=lambda _seconds: None,
    )

    answer = runner("做事")

    assert "超过" in answer or "没有产出" in answer
