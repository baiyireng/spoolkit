"""命令授权要能送到聊天里（原先在 events 模式下问不到人，任务原地死掉）。

用户那轮 QQ 对话的现场：它想跑一条 `find` 找文件 → 被拒 → 换个写法再试 →
连续四次之后触发重复保护、整轮收尾。用户收到的只有一句"任务没做完"，而 agent
自己写的是"当前无人值守，无法获取用户批准"。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from spoolkit.bridge.agent_runner import AgentRunner
from spoolkit.cli.commands.bridge import run_extra_args
from spoolkit.cli.commands.run import _events_approver

ROOT = Path(__file__).resolve().parents[2]


class _Writer:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind: str, **data) -> None:
        self.events.append((kind, data))


class _Lines:
    def __init__(self, *lines: str) -> None:
        self._lines = list(lines)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


def test_回答映射到四档() -> None:
    cases = {"y": "once", "a": "always", "b": "block", "n": "deny", "": "deny"}
    for answer, expected in cases.items():
        writer = _Writer()
        approver = _events_approver(writer, reader=_Lines(answer))
        assert approver(["find", "."], "找文件") == expected, answer
        kinds = [kind for kind, _ in writer.events]
        assert kinds == ["await", "confirm"], kinds   # 问题要发出去、决定要收回执


def test_问题里带上命令与原因() -> None:
    writer = _Writer()
    approver = _events_approver(writer, reader=_Lines("y"))
    approver(["find", ".", "-name", "*.txt"], "在项目里找 txt")

    _, data = writer.events[0]
    assert data["command"] == "find . -name *.txt"
    assert data["reason"] == "在项目里找 txt"
    assert data["count"] == 1


def test_没人回答时是拒绝_而不是干等() -> None:
    """`--ask-on-stdin` 没给时压根不该走这条路；给了但流空了，也只能拒绝。"""
    writer = _Writer()
    approver = _events_approver(writer, reader=_Lines())
    assert approver(["find", "."], "找文件") == "deny"


def test_桥会打开_ask_on_stdin() -> None:
    args = argparse.Namespace(
        provider="", model="", base_url="", script="", proxy="", policy="", scope=""
    )
    assert "--ask-on-stdin" in run_extra_args(args, Path("."))


def test_待确认的话要说清是在问命令() -> None:
    runner = AgentRunner(Path("."))
    text = runner._pending_text(
        {
            "awaiting": 1,
            "awaiting_detail": {"command": "find . -name *.txt", "reason": "找文件"},
        }
    )
    assert "find . -name *.txt" in text
    assert "白名单外的命令" in text
    assert "始终允许" in text          # 四档要写全，不然用户不知道还能"始终"


class _StubRunner:
    def __init__(self) -> None:
        self.words: list[str] = []

    def snapshot(self) -> dict:
        return {
            "awaiting": 1,
            "awaiting_detail": {"command": "find .", "reason": "找文件"},
            "finished": False,
            "running": True,
        }

    def answer(self, word: str) -> bool:
        self.words.append(word)
        return True


def test_用户在聊天里回_始终_会原样传下去() -> None:
    """把四档压成 y/n 就等于把"始终允许"这个选项从聊天里删掉了。"""
    runner = AgentRunner(Path("."))
    stub = _StubRunner()
    runner._runner = stub

    assert runner("始终") == "已始终允许（记在这个工作区）。"
    assert stub.words == ["a"]

    assert runner("n") == "已拒绝。"
    assert stub.words == ["a", "n"]


def test_真跑一轮_授权后命令能执行(tmp_path: Path) -> None:
    """集成：`run --events --ask-on-stdin` + 脚本化模型调用一条白名单外命令。

    这条是这次修复的验收——修之前，同一个脚本会拿到"无人值守，无法获得批准"，
    命令根本不会执行。
    """
    script = [
        json.dumps(
            {
                "thought": "跑一条不在白名单里的命令看看",
                "tool_calls": [
                    {"name": "run_command", "arguments": {"command": ["whoami"]}}
                ],
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
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable, "-m", "spoolkit.cli.app", "run",
            "--events", "--ask-on-stdin",
            "--provider", "fake", "--script", str(script_path),
            "--root", str(tmp_path), "--no-memory", "--max-steps", "3",
            "--goal", "试试执行命令",
        ],
        input="y\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
        timeout=180,
    )

    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    await_events = [e for e in events if e.get("type") == "await"]
    assert await_events, f"没有把授权请求发出来：{proc.stdout[-800:]}"
    assert await_events[0].get("command"), "请求里要带上是哪条命令"

    ran = [e for e in events if e.get("type") == "tool" and e.get("name") == "run_command"]
    assert ran, f"命令没有执行：{events[-6:]}"
    assert ran[0].get("ok") is True and "退出码 0" in ran[0].get("detail", ""), ran[0]

    text = proc.stdout + proc.stderr
    assert "无人值守" not in text, "不该再走到'无人值守'那条路"
