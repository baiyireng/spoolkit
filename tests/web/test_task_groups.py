"""任务卡：按 task_id 分组，挂到这一轮的助手消息上。不画界面。"""

import json
import sqlite3
from pathlib import Path

from spoolkit.memory.store import init_memory_schema
from spoolkit.store.db import open_db
from spoolkit.web.protocol import TASK_EVENT, Event
from spoolkit.web.runner import Runner
from spoolkit.web.task_view import build_task_groups


def _event(task_id: str, turn_id: str, seq: int, phase: str, status: str, summary: str, **extra) -> dict:
    payload = {
        "task_id": task_id,
        "run_id": "run-1",
        "turn_id": turn_id,
        "seq": seq,
        "step": 0,
        "phase": phase,
        "source": "system" if phase != "observe" else "tool",
        "tool_call_id": "",
        "intent": "",
        "args_digest": "",
        "status": status,
        "summary": summary,
        "refs": ["file:a.py"],
        "artifacts": [],
        "duration": None,
        "risk": "",
    }
    payload.update(extra)
    return payload


def _story(task_id: str, turn_id: str, *, verify: str = "ok", final: str = "ok") -> list[dict]:
    return [
        _event(task_id, turn_id, 1, "understand", "running", "写一个文件"),
        _event(task_id, turn_id, 2, "retrieval", "ok", "预取目录", source="prefetch"),
        _event(task_id, turn_id, 3, "observe", "ok", "a.py：已写入", tool_call_id="tc-0-0", source="tool"),
        _event(task_id, turn_id, 4, "verify", verify, "编译失败" if verify == "error" else "没有发现问题"),
        _event(task_id, turn_id, 5, "final", final, "写好了" if final == "ok" else "已取消"),
    ]


def test_单任务按序分组且不带raw() -> None:
    events = _story("run-1:aaaa", "aaaa")
    events[2]["artifacts"] = [{"type": "diff", "path": "a.py", "ref": "tc-0-0"}]
    events[2]["raw"] = "RAW_SECRET"
    events[2]["thought"] = "秘密思考"
    groups = build_task_groups(events)
    assert len(groups) == 1
    group = groups[0]
    assert group["task_id"] == "run-1:aaaa"
    assert group["run_id"] == "run-1"
    assert group["turn_id"] == "aaaa"
    assert group["goal"] == "写一个文件"
    assert group["status"] == "done"
    assert [item["phase"] for item in group["events"]] == [
        "understand",
        "retrieval",
        "observe",
        "verify",
        "final",
    ]
    assert group["artifacts"] == [{"type": "diff", "path": "a.py", "ref": "tc-0-0"}]
    packed = json.dumps(group, ensure_ascii=False)
    assert "RAW_SECRET" not in packed
    assert "秘密思考" not in packed
    assert "raw" not in group["events"][2]


def test_同run不同turn不串() -> None:
    events = _story("run-1:aaaa", "aaaa") + _story("run-1:bbbb", "bbbb")
    groups = build_task_groups(events)
    assert [item["task_id"] for item in groups] == ["run-1:aaaa", "run-1:bbbb"]
    assert groups[0]["run_id"] == groups[1]["run_id"] == "run-1"
    assert groups[0]["turn_id"] != groups[1]["turn_id"]
    assert all(item["task_id"] == "run-1:aaaa" for item in groups[0]["events"])
    assert all(item["task_id"] == "run-1:bbbb" for item in groups[1]["events"])


def test_状态_验证失败_未完成_取消() -> None:
    assert build_task_groups(_story("t", "a", verify="error"))[0]["status"] == "verify_failed"
    running = build_task_groups(_story("t", "a")[:3])
    assert running[0]["status"] == "running"
    failed = build_task_groups(
        [
            _event("t", "a", 1, "understand", "running", "做不完"),
            _event("t", "a", 2, "error", "error", "已达步数上限"),
        ]
    )
    assert failed[0]["status"] == "failed"
    cancelled = build_task_groups(_story("t", "a", final="cancelled"))
    assert cancelled[0]["status"] == "cancelled"


def _memory(root: Path) -> sqlite3.Connection:
    path = root / ".agent" / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(path)
    init_memory_schema(conn)
    return conn


def _say(conn: sqlite3.Connection, session: str, role: str, content: str, at: float) -> None:
    conn.execute(
        "INSERT INTO transcript(session_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
        (session, role, content, "", at),
    )
    conn.commit()


def _turn(runner: Runner, events: list[dict], started: float, ended: float) -> None:
    runner._turn_open = True
    runner._turn_ids = set()
    runner._turn_started = started
    for item in events:
        runner._absorb(Event(TASK_EVENT, item))
    runner._seal_turn_locked()
    runner._turns[-1]["started_at"] = started
    runner._turns[-1]["ended_at"] = ended


def test_消息上每张卡只挂一次且快照可序列化(tmp_path: Path) -> None:
    conn = _memory(tmp_path)
    _say(conn, "t", "user", "第一次", 10)
    _say(conn, "t", "assistant", "第一次答", 20)
    _say(conn, "t", "user", "第二次", 40)
    _say(conn, "t", "assistant", "第二次答", 50)
    conn.close()

    runner = Runner(tmp_path, session="t")
    first = _story("run-1:aaaa", "aaaa")
    second = _story("run-1:bbbb", "bbbb", verify="error")
    _turn(runner, first, 1, 30)
    _turn(runner, second, 31, 60)

    snapshot = runner.snapshot(health={"ok": True})
    json.dumps(snapshot, ensure_ascii=False)
    messages = snapshot["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0]["tasks"] == []
    assert messages[2]["tasks"] == []
    assert [item["task_id"] for item in messages[1]["tasks"]] == ["run-1:aaaa"]
    assert [item["task_id"] for item in messages[3]["tasks"]] == ["run-1:bbbb"]
    mounted = [item["task_id"] for message in messages for item in message["tasks"]]
    assert mounted == ["run-1:aaaa", "run-1:bbbb"]
    assert len(mounted) == len(set(mounted))
    assert [item["status"] for item in snapshot["task_groups"]] == ["done", "verify_failed"]
    assert all(line.get("type") != TASK_EVENT for line in snapshot["trace"]["lines"])
    packed = json.dumps(snapshot["task_groups"], ensure_ascii=False)
    assert "raw" not in snapshot["task_groups"][0]["events"][0]
    assert "RAW" not in packed
