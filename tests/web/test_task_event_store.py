"""runner 收下 task_event，并按 tool_call_id 派生 approval。不写磁盘。"""

import json
from pathlib import Path

from spoolkit.web.protocol import TASK_EVENT, Event, parse_line
from spoolkit.web.runner import Runner, trace_line


def _tool(task_id: str, call_id: str, seq: int, *, step: int = 0) -> Event:
    return Event(
        TASK_EVENT,
        {
            "task_id": task_id,
            "run_id": "run-1",
            "turn_id": "turn-1",
            "seq": seq,
            "step": step,
            "phase": "observe",
            "source": "tool",
            "tool_call_id": call_id,
            "intent": "",
            "args_digest": "",
            "status": "ok",
            "summary": "改过了",
            "refs": [],
            "artifacts": [],
            "duration": None,
            "risk": "",
        },
    )


def _events(runner: Runner) -> list[dict]:
    return runner.snapshot(health={"ok": True})["task_events"]


def test_编辑的tool_diff_confirm按call_id串起来(tmp_path: Path) -> None:
    runner = Runner(tmp_path, session="t")
    tool = {"name": "replace_lines", "ok": True, "detail": "a.py 待确认"}
    diff = {"path": "a.py", "text": "@@ -1 +1 @@\n-x\n+y\n", "tool_call_id": "tc-0-0"}
    waiting = {"count": 1, "tool_call_ids": ["tc-0-0"]}
    confirm = {"applied": True, "count": 1, "tool_call_ids": ["tc-0-0"]}
    runner._absorb(Event("tool", tool))
    runner._absorb(_tool("job-edit", "tc-0-0", 2))
    runner._absorb(Event("diff", diff))
    runner._absorb(Event("await", waiting))
    runner._absorb(_tool("job-edit", "tc-0-0", 2))
    runner._absorb(Event("diff", diff))
    runner._absorb(Event("confirm", confirm))

    stored = _events(runner)
    assert [item["seq"] for item in stored] == [2, 3, 4]
    assert [item["phase"] for item in stored] == ["observe", "approval", "approval"]
    assert [item["status"] for item in stored] == ["ok", "await", "confirmed"]
    assert {item["tool_call_id"] for item in stored} == {"tc-0-0"}
    assert {item["task_id"] for item in stored} == {"job-edit"}
    approval = stored[1]
    assert approval["source"] == "system"
    assert approval["artifacts"] == [{"type": "diff", "path": "a.py", "ref": "tc-0-0"}]
    json.dumps(stored)

    logged = runner.events()
    assert logged[0]["type"] == "tool"
    assert logged[0]["name"] == "replace_lines"
    assert logged[0]["ok"] is True
    assert logged[0]["detail"] == "a.py 待确认"
    assert [item["type"] for item in logged] == [
        "tool", "task_event", "diff", "await", "task_event", "diff", "confirm",
    ]
    assert logged[2]["path"] == "a.py"
    assert logged[2]["text"].startswith("@@")
    assert logged[3]["count"] == 1
    assert logged[6]["applied"] is True
    assert trace_line(Event(TASK_EVENT, stored[0])) is None
    assert all(line["type"] != TASK_EVENT for line in runner.snapshot(health={"ok": True})["trace"]["lines"])
    assert runner.snapshot(health={"ok": True})["diffs"] == []


def test_连续两次编辑和未关联diff不串task_id(tmp_path: Path) -> None:
    runner = Runner(tmp_path, session="t")
    runner._absorb(_tool("job-a", "tc-0-0", 1, step=0))
    runner._absorb(_tool("job-b", "tc-1-0", 2, step=1))
    runner._absorb(Event("diff", {"path": "a.py", "text": "a", "tool_call_id": "tc-0-0"}))
    runner._absorb(Event("diff", {"path": "a.py", "text": "b", "tool_call_id": "tc-1-0"}))
    runner._absorb(Event("diff", {"path": "a.py", "text": "c", "tool_call_id": "tc-missing"}))
    runner._absorb(Event("await", {"count": 2, "tool_call_ids": ["tc-0-0", "tc-1-0"]}))
    runner._absorb(Event("confirm", {
        "applied": False,
        "count": 2,
        "tool_call_ids": ["tc-0-0", "tc-1-0"],
    }))

    stored = _events(runner)
    by_call = {}
    for item in stored:
        by_call.setdefault(item["tool_call_id"], []).append(item)

    assert [item["task_id"] for item in by_call["tc-0-0"]] == ["job-a", "job-a", "job-a"]
    assert [item["status"] for item in by_call["tc-0-0"]] == ["ok", "await", "rejected"]
    assert [item["task_id"] for item in by_call["tc-1-0"]] == ["job-b", "job-b", "job-b"]
    assert [item["step"] for item in by_call["tc-1-0"]] == [1, 1, 1]
    missing = by_call["tc-missing"]
    assert len(missing) == 1
    assert missing[0]["task_id"] == ""
    assert missing[0]["run_id"] == "t:-"
    assert missing[0]["turn_id"] == ""
    assert missing[0]["status"] == "await"
    assert [item["seq"] for item in stored] == list(range(1, len(stored) + 1))


def test_命令授权的await和confirm共用tool_call_id(tmp_path: Path) -> None:
    runner = Runner(tmp_path, session="t")
    runner._absorb(_tool("job-cmd", "tc-0-0", 4))
    await_data = {
        "count": 1,
        "command": "find .",
        "reason": "这条命令需要你批准才执行。",
        "tool_call_id": "tc-0-0",
    }
    confirm_data = {
        "applied": True,
        "count": 1,
        "command": "find .",
        "tool_call_id": "tc-0-0",
    }
    runner._absorb(Event("await", await_data))
    runner._absorb(Event("confirm", confirm_data))
    runner._absorb(Event("await", await_data))

    stored = [item for item in _events(runner) if item["phase"] == "approval"]
    assert [item["status"] for item in stored] == ["await", "confirmed"]
    assert {item["tool_call_id"] for item in stored} == {"tc-0-0"}
    assert {item["task_id"] for item in stored} == {"job-cmd"}
    logged = [item for item in runner.events() if item["type"] in ("await", "confirm")]
    assert logged[0]["command"] == "find ."
    assert logged[0]["reason"].startswith("这条命令需要你批准")
    assert logged[1]["applied"] is True
    assert logged[1]["count"] == 1


def _path(tmp_path: Path, session: str = "t") -> Path:
    from spoolkit.session_state import safe

    return tmp_path / ".agent" / "task-events" / f"{safe(session)}.jsonl"


def test_写入后同会话能恢复(tmp_path: Path) -> None:
    runner = Runner(tmp_path, session="t")
    tool = _tool("task", "tc-0-0", 1)
    tool.data["run_id"] = ""
    tool.data["turn_id"] = "turn-9"
    tool.data["raw"] = "SECRET_RAW"
    tool.data["thought"] = "秘密思考"
    runner._absorb(tool)
    runner._absorb(Event("diff", {"path": "a.py", "text": "@@", "tool_call_id": "tc-0-0"}))
    runner._absorb(Event("confirm", {"applied": True, "count": 1, "tool_call_ids": ["tc-0-0"]}))
    first = _events(runner)
    assert first[0]["task_id"] == "task"
    assert first[0]["run_id"] == "t:turn-9"
    assert "SECRET_RAW" not in _path(tmp_path).read_text(encoding="utf-8")
    assert "秘密思考" not in _path(tmp_path).read_text(encoding="utf-8")
    assert "raw" not in first[0]
    assert "thought" not in first[0]

    again = Runner(tmp_path, session="t")
    assert _events(again) == first
    chained = [item for item in _events(again) if item["tool_call_id"] == "tc-0-0"]
    assert [item["phase"] for item in chained] == ["observe", "approval", "approval"]
    assert {item["run_id"] for item in chained} == {"t:turn-9"}
    logged = runner.events()
    assert logged[0]["type"] == "task_event"
    assert logged[1]["type"] == "diff"
    assert logged[1]["path"] == "a.py"


def test_坏行跳过且重复行只留一条(tmp_path: Path) -> None:
    from spoolkit.agent.contract import tool_event

    payload = tool_event(
        task_id="job",
        run_id="run-1",
        turn_id="turn-1",
        seq=1,
        step=0,
        tool_call_id="tc-0-0",
        status="ok",
        summary="看过了",
    ).to_dict()
    line = json.dumps(payload, ensure_ascii=False)
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        line + "\nnot-json\n" + line + "\n{\"phase\": \"nope\"}\n\n",
        encoding="utf-8",
    )
    runner = Runner(tmp_path, session="t")
    assert _events(runner) == [payload]
    kept = [item for item in path.read_text(encoding="utf-8").splitlines() if item.strip()]
    assert kept == [line]


def test_超过上限只留最近几条(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("spoolkit.web.runner.TASK_EVENT_LIMIT", 2)
    runner = Runner(tmp_path, session="t")
    for seq, call_id in enumerate(("tc-0-0", "tc-0-1", "tc-0-2"), start=1):
        runner._absorb(_tool("job", call_id, seq))
    stored = _events(runner)
    assert [item["tool_call_id"] for item in stored] == ["tc-0-1", "tc-0-2"]
    again = Runner(tmp_path, session="t")
    assert [item["tool_call_id"] for item in _events(again)] == ["tc-0-1", "tc-0-2"]
    assert len(_path(tmp_path).read_text(encoding="utf-8").splitlines()) == 2


def test_不同run的approval互不覆盖(tmp_path: Path) -> None:
    runner = Runner(tmp_path, session="t")
    for run_id, seq in (("run-a", 1), ("run-b", 2)):
        tool = _tool("job", "tc-0-0", seq)
        tool.data["run_id"] = run_id
        tool.data["turn_id"] = run_id
        runner._absorb(tool)
        runner._absorb(Event("diff", {"path": "a.py", "text": run_id, "tool_call_id": "tc-0-0"}))
    approvals = [item for item in _events(runner) if item["phase"] == "approval"]
    assert [(item["run_id"], item["tool_call_id"], item["status"]) for item in approvals] == [
        ("run-a", "tc-0-0", "await"),
        ("run-b", "tc-0-0", "await"),
    ]
    assert approvals[0]["task_id"] == "job"
    assert approvals[1]["summary"].startswith("a.py")
    again = Runner(tmp_path, session="t")
    assert _events(again) == _events(runner)


def test_写入失败不丢掉内存里的事件(tmp_path: Path) -> None:
    (tmp_path / ".agent").write_text("not-a-directory", encoding="utf-8")
    runner = Runner(tmp_path, session="t")
    runner._absorb(_tool("job", "tc-0-0", 1))
    assert _events(runner)[0]["tool_call_id"] == "tc-0-0"
    assert _events(runner)[0]["run_id"] == "run-1"


def test_协议行能进runner(tmp_path: Path) -> None:
    line = json.dumps({"type": TASK_EVENT, "task_id": "job", "seq": 1, "phase": "observe", "source": "tool"})
    event = parse_line(line)
    assert event is not None and event.type == TASK_EVENT
    runner = Runner(tmp_path, session="t")
    runner._absorb(event)
    assert _events(runner)[0]["task_id"] == "job"


def test_恢复后tool还在且旧行缺tool为空(tmp_path: Path) -> None:
    folder = tmp_path / ".agent" / "task-events"
    folder.mkdir(parents=True)
    old = {
        "task_id": "run-1:old",
        "run_id": "run-1",
        "turn_id": "old",
        "seq": 1,
        "step": 0,
        "phase": "observe",
        "source": "tool",
        "tool_call_id": "tc-0-0",
        "status": "ok",
        "summary": "旧记录",
    }
    fresh = {
        **old,
        "task_id": "run-1:new",
        "turn_id": "new",
        "seq": 2,
        "tool": "search_code",
        "summary": "搜过了",
    }
    (folder / "t.jsonl").write_text(
        json.dumps(old, ensure_ascii=False) + "\n" + json.dumps(fresh, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    runner = Runner(tmp_path, session="t")
    stored = _events(runner)
    assert [item["tool"] for item in stored] == ["", "search_code"]
    groups = runner.snapshot(health={"ok": True})["task_groups"]
    assert [item["events"][0]["tool"] for item in groups] == ["", "search_code"]
    assert [item["task_id"] for item in groups] == ["run-1:old", "run-1:new"]
