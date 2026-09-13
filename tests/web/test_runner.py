import queue
import sys
import time
from pathlib import Path

from agents_dev.web.runner import Runner


def _runner(tmp_path: Path, *extra: str) -> Runner:
    return Runner(tmp_path, session="t", extra_args=list(extra))


def test_命令拼装包含事件模式(tmp_path: Path) -> None:
    command = _runner(tmp_path).command("看看代码")
    assert command[0] == sys.executable
    assert "agents_dev.cli.app" in command
    assert "run" in command
    assert "--events" in command
    assert "--goal" in command
    assert "看看代码" in command


def test_初始状态是空闲的(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    assert runner.running is False
    assert runner.snapshot()["running"] is False


def test_状态里带上会话历史(tmp_path: Path) -> None:
    """聊天区域要有历史可看：网页壳按 /state 回放它。

    它**只是给人看的**：模型上下文不受它影响。两者混为一谈会得出
    "把历史塞回上下文"的结论，而那会把这个项目的支点（短上下文）推翻。
    """
    from agents_dev.memory.store import init_memory_schema
    from agents_dev.memory.transcript import ASSISTANT, USER, record_message
    from agents_dev.store.db import open_db

    db = tmp_path / ".agent" / "memory.db"
    conn = open_db(db)
    init_memory_schema(conn)
    record_message(conn, "t", USER, "看看 a.py")
    record_message(conn, "t", ASSISTANT, "它有个 f()")
    conn.close()

    messages = _runner(tmp_path).snapshot()["messages"]
    assert [item["role"] for item in messages] == [USER, ASSISTANT]
    assert messages[0]["content"] == "看看 a.py"


def test_没有历史时是空列表(tmp_path: Path) -> None:
    assert _runner(tmp_path).snapshot()["messages"] == []


def test_历史库读坏了也不影响状态接口(tmp_path: Path) -> None:
    """历史是增强，不该因为它把状态接口弄挂。"""
    target = tmp_path / ".agent"
    target.mkdir(parents=True)
    (target / "memory.db").write_text("这不是数据库", encoding="utf-8")
    assert _runner(tmp_path).snapshot()["messages"] == []


def test_订阅者在运行结束后仍能收到事件(tmp_path: Path) -> None:
    """一次极短的运行：假模型缺脚本会立刻失败退出。"""
    runner = _runner(tmp_path, "--provider", "fake", "--script", "")
    assert runner.start("无所谓") is True
    listener = runner.subscribe()
    deadline = time.time() + 60
    seen_final = False
    while time.time() < deadline:
        try:
            event = listener.get(timeout=0.5)
        except queue.Empty:
            if not runner.running:
                break
            continue
        if event.type == "final":
            seen_final = True
            break
    assert seen_final or runner.snapshot()["finished"] is True
    runner.unsubscribe(listener)


def test_运行中再次启动被拒绝(tmp_path: Path) -> None:
    runner = _runner(tmp_path, "--provider", "fake", "--script", "")
    runner.start("第一次")
    assert runner.start("第二次") is False


def test_没有待确认时确认请求被拒绝(tmp_path: Path) -> None:
    assert _runner(tmp_path).confirm(True) is False


def test_快照包含关键字段(tmp_path: Path) -> None:
    snapshot = _runner(tmp_path).snapshot()
    for key in ("running", "finished", "awaiting", "goal", "usage", "final"):
        assert key in snapshot


def test_开始后快照记录目标(tmp_path: Path) -> None:
    runner = _runner(tmp_path, "--provider", "fake", "--script", "")
    runner.start("这件事")
    assert runner.snapshot()["goal"] == "这件事"

