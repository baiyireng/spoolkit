"""真实 Chrome 验收任务卡：会话记录 + task-events 走 /state，不靠假 DOM。"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from spoolkit.memory.store import init_memory_schema
from spoolkit.store.db import open_db
from spoolkit.web.protocol import TASK_EVENT, Event
from spoolkit.web.runner import Runner
from spoolkit.web.server import _as_state, build_server

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "recon" / "shots"
LONG_GOAL = "把配置文件整理成可提交的说明" + "而且目标很长" * 8
LONG_SUMMARY = "读到的配置分成三段，每段都有注释" + "摘要很长" * 12


def _browser() -> str | None:
    from tests.web.test_assets import _browser as pick

    return pick()


def _memory(root: Path) -> sqlite3.Connection:
    path = root / ".agent" / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(path)
    init_memory_schema(conn)
    return conn


def _say(conn: sqlite3.Connection, role: str, content: str, at: float) -> None:
    conn.execute(
        "INSERT INTO transcript(session_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
        ("t", role, content, "", at),
    )
    conn.commit()


def _event(task_id: str, turn_id: str, seq: int, phase: str, status: str, summary: str, **extra) -> dict:
    payload = {
        "task_id": task_id,
        "run_id": "run-1",
        "turn_id": turn_id,
        "seq": seq,
        "step": 1 if phase == "observe" else 0,
        "phase": phase,
        "source": "tool" if phase == "observe" else "system",
        "tool_call_id": "",
        "tool": "",
        "intent": "",
        "args_digest": "",
        "status": status,
        "summary": summary,
        "refs": [],
        "artifacts": [],
        "duration": None,
        "risk": "",
    }
    payload.update(extra)
    return payload


def _turn(runner: Runner, events: list[dict], started: float, ended: float) -> None:
    runner._turn_open = True
    runner._turn_ids = set()
    runner._turn_started = started
    for item in events:
        runner._absorb(Event(TASK_EVENT, item))
    runner._seal_turn_locked()
    runner._turns[-1]["started_at"] = started
    runner._turns[-1]["ended_at"] = ended
    runner._turns[-1]["lines"] = [{"type": "step", "text": "不该出现的轨迹"}]


def _wait(page, expression: str, timeout: float = 8) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.eval(expression):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _patch_summary(runner: Runner, task_id: str, phase: str, summary: str) -> None:
    with runner._lock:
        for item in runner._task_events:
            if item.get("task_id") == task_id and item.get("phase") == phase:
                item["summary"] = summary


def test_真实Chrome渲染任务卡并定位审批(tmp_path: Path) -> None:
    """会话库里的往来和 task-events 经 /state 画成任务卡，1440 与 390 都能量。"""
    browser = _browser()
    if browser is None:
        pytest.skip("没有 Chrome/Edge，无法做真实浏览器验收")
    sys.path.insert(0, str(ROOT / "recon"))
    from cdp import CDP

    root = tmp_path / ("long-workspace-path-" + "segment" * 8)
    root.mkdir()
    conn = _memory(root)
    _say(conn, "user", "整理配置", 10)
    _say(conn, "assistant", "配置已经整理好。", 20)
    _say(conn, "user", "继续看长文件", 40)
    _say(conn, "assistant", "还在读配置。", 50)
    conn.close()

    done_id = "run-1:done"
    live_id = "run-1:live"
    runner = Runner(root, session="t", extra_args=["--provider", "fake", "--script", ""])
    _turn(
        runner,
        [
            _event(done_id, "done", 1, "understand", "running", LONG_GOAL),
            _event(done_id, "done", 2, "retrieval", "ok", "预取目录", source="prefetch"),
            _event(
                done_id, "done", 3, "observe", "ok", "4 行",
                tool="read_file", intent="读源码", tool_call_id="tc-done", source="tool",
            ),
            _event(done_id, "done", 4, "verify", "ok", "没有发现问题"),
            _event(done_id, "done", 5, "final", "ok", "这是一段不该进任务卡的长结论"),
        ],
        1,
        30,
    )
    _turn(
        runner,
        [
            _event(live_id, "live", 1, "understand", "running", LONG_GOAL),
            _event(
                live_id, "live", 2, "observe", "ok", LONG_SUMMARY,
                tool="read_file", intent="读源码", tool_call_id="tc-run", source="tool",
            ),
            _event(
                live_id, "live", 3, "approval", "await", "a.py 待确认",
                tool_call_id="tc-run",
                artifacts=[{"type": "diff", "path": "a.py", "ref": "tc-run"}],
            ),
        ],
        31,
        60,
    )
    runner._diffs = [{"path": "a.py", "text": "+x\n", "tool_call_id": "tc-run"}]
    runner._awaiting = 1

    state = _as_state(runner)
    httpd = build_server(state, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    server = f"http://127.0.0.1:{httpd.server_port}"
    port = 9353
    profile = tmp_path / "task-card-browser"
    proc = subprocess.Popen(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--disable-extensions",
            f"--remote-debugging-port={port}",
            "--user-data-dir=" + str(profile),
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        page = CDP(port)
        page.viewport(1440, 900, mobile=False)
        page.navigate(server + "/")
        assert _wait(page, "document.querySelectorAll('.task-card').length >= 2"), "任务卡没有从 /state 画出来"
        assert _wait(page, "!!document.querySelector('.ask-card')"), "待确认卡没有出现"

        page.eval(
            """
            (() => {
              const done = document.querySelector('.task-card-done .task-head');
              if (done && done.getAttribute('aria-expanded') === 'false') done.click();
            })()
            """
        )
        detail = page.eval(
            r"""
            (() => {
              const done = document.querySelector('.task-card-done');
              const live = document.querySelector('.task-card-running');
              const doneMsg = done && done.closest('.msg');
              const liveMsg = live && live.closest('.msg');
              const goal = live.querySelector('.task-goal');
              const summary = live.querySelector('.task-summary');
              const goalStyle = getComputedStyle(goal);
              const summaryStyle = getComputedStyle(summary);
              const card = live.getBoundingClientRect();
              const composer = document.getElementById('composer').getBoundingClientRect();
              const send = document.getElementById('send').getBoundingClientRect();
              return {
                done_aria: done.querySelector('.task-head').getAttribute('aria-expanded'),
                live_aria: live.querySelector('.task-head').getAttribute('aria-expanded'),
                done_trace: !!(doneMsg && doneMsg.querySelector('.trace')),
                live_trace: !!(liveMsg && liveMsg.querySelector('.trace')),
                tool: live.querySelector('.task-tool').textContent,
                intent: live.querySelector('.task-intent').textContent,
                status: live.querySelector('.task-row-status').textContent,
                jump: live.querySelector('.task-jump').textContent,
                summary: summary.textContent,
                summary_title: summary.title,
                goal_title: goal.title,
                goal_clamp: goalStyle.webkitLineClamp,
                summary_clamp: summaryStyle.webkitLineClamp,
                goal_h: Math.round(goal.getBoundingClientRect().height),
                summary_h: Math.round(summary.getBoundingClientRect().height),
                chip: live.querySelector('.task-chip').textContent,
                chevron: !!live.querySelector('.task-chevron'),
                verify: done.querySelector('.task-verify')
                  ? done.querySelector('.task-body .task-verify').textContent : '',
                final: done.querySelector('.task-final')
                  ? done.querySelector('.task-final').textContent : '',
                raw: document.getElementById('stream').textContent.indexOf('RAW_SECRET') >= 0,
                long_final: document.getElementById('stream').textContent.indexOf('不该进任务卡') >= 0,
                ask_id: document.querySelector('.ask-card').getAttribute('data-tool-call-id'),
                task_apply: live.querySelectorAll('.ask-apply').length,
                card_h: Math.round(card.height),
                card_w: Math.round(card.width),
                view_h: window.innerHeight,
                view_w: window.innerWidth,
                composer_top: Math.round(composer.top),
                composer_bottom: Math.round(composer.bottom),
                send_bottom: Math.round(send.bottom),
              };
            })()
            """
        )
        assert detail["done_aria"] == "true"
        assert detail["live_aria"] == "true", detail
        assert detail["done_trace"] is False, detail
        assert detail["live_trace"] is False, detail
        assert detail["tool"] == "read_file", detail
        assert detail["intent"] == "读源码", detail
        assert detail["status"] == "完成", detail
        assert detail["jump"] == "查看改动", detail
        assert detail["summary"] == LONG_SUMMARY
        assert detail["summary_title"] == LONG_SUMMARY
        assert detail["goal_title"] == LONG_GOAL
        assert detail["goal_clamp"] == "2", detail
        assert detail["summary_clamp"] == "2", detail
        assert detail["goal_h"] <= 48, detail
        assert detail["summary_h"] <= 48, detail
        assert detail["chip"] == "进行中", detail
        assert detail["chevron"] is True
        assert detail["verify"] == "没有发现问题", detail
        assert detail["final"] == "结论已生成", detail
        assert detail["raw"] is False
        assert detail["long_final"] is False, detail
        assert detail["ask_id"] == "tc-run", detail
        assert detail["task_apply"] == 0
        assert detail["card_h"] < detail["view_h"] * 0.8, detail
        assert detail["card_w"] < detail["view_w"] - 80, detail
        assert detail["composer_bottom"] <= detail["view_h"] + 1, detail
        assert detail["send_bottom"] <= detail["view_h"] + 1, detail
        assert detail["composer_top"] < detail["view_h"], detail

        page.eval(
            """
            (() => {
              const done = document.querySelector('.task-card-done .task-head');
              if (done && done.getAttribute('aria-expanded') === 'true') done.click();
            })()
            """
        )
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.shot(str(SHOTS / "task-card-1440.png"))

        page.eval("document.querySelector('.task-card-running .task-head').click()")
        _patch_summary(runner, live_id, "understand", LONG_GOAL + "重绑甲")
        state.bump()
        assert _wait(
            page,
            "document.querySelector('.task-card-running .task-goal').title.indexOf('重绑甲') >= 0"
            " && document.querySelector('.task-card-running .task-head').getAttribute('aria-expanded') === 'false'",
        ), "手动收起后重绑把开合重置了"
        page.eval("document.querySelector('.task-card-running .task-head').click()")
        rebound = "重绑摘要乙" + "摘要很长" * 4
        _patch_summary(runner, live_id, "observe", rebound)
        state.bump()
        assert _wait(
            page,
            "document.querySelector('.task-card-running .task-summary') && "
            "document.querySelector('.task-card-running .task-summary').textContent.indexOf('重绑摘要乙') === 0 && "
            "document.querySelector('.task-card-running .task-head').getAttribute('aria-expanded') === 'true'",
        ), "手动展开后重绑把开合重置了"

        page.eval("document.querySelector('.task-card-running .task-jump').click()")
        located = page.eval(
            r"""
            (() => {
              const ask = document.querySelector('.ask-card');
              const box = ask.getBoundingClientRect();
              const active = document.activeElement;
              return {
                id: ask.getAttribute('data-tool-call-id'),
                top: Math.round(box.top),
                bottom: Math.round(box.bottom),
                view_h: window.innerHeight,
                focus: active ? active.className : '',
              };
            })()
            """
        )
        assert located["id"] == "tc-run", located
        assert located["focus"] == "ask-apply", located
        assert located["top"] < located["view_h"] and located["bottom"] > 0, located

        page.navigate(server + "/")
        assert _wait(page, "document.querySelectorAll('.task-card').length >= 2"), "刷新后任务卡没了"
        refreshed = page.eval(
            r"""
            (() => {
              const done = document.querySelector('.task-card-done .task-head');
              const live = document.querySelector('.task-card-running .task-head');
              return {
                done: done ? done.getAttribute('aria-expanded') : '',
                live: live ? live.getAttribute('aria-expanded') : '',
                tool: document.querySelector('.task-card-running .task-tool').textContent,
              };
            })()
            """
        )
        assert refreshed["done"] == "false", refreshed
        assert refreshed["live"] == "true", refreshed
        assert refreshed["tool"] == "read_file"

        page.viewport(390, 844, mobile=True)
        assert _wait(
            page,
            "document.getElementById('sidebar').getBoundingClientRect().right <= 1",
            timeout=4,
        ), "窄屏侧栏没有收起"
        page.eval("document.querySelector('.task-card-running').scrollIntoView({block:'start'})")
        _patch_summary(runner, live_id, "observe", LONG_SUMMARY)
        state.bump()
        assert _wait(
            page,
            "document.querySelector('.task-card-running .task-summary').textContent.indexOf('读到的配置分成三段') === 0",
        ), "窄屏截图前长摘要没有写回"
        narrow = page.eval(
            r"""
            (() => {
              const card = document.querySelector('.task-card-running');
              const stream = document.getElementById('stream');
              const where = document.getElementById('where');
              const top = document.getElementById('top');
              const cardBox = card.getBoundingClientRect();
              const streamBox = stream.getBoundingClientRect();
              const whereStyle = getComputedStyle(where);
              return {
                page_overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                card_overflow: card.scrollWidth - card.clientWidth,
                card_right: Math.round(cardBox.right),
                stream_right: Math.round(streamBox.right),
                card_left: Math.round(cardBox.left),
                stream_left: Math.round(streamBox.left),
                where_h: Math.round(where.getBoundingClientRect().height),
                top_h: Math.round(top.getBoundingClientRect().height),
                white_space: whereStyle.whiteSpace,
                where_title: where.title,
                where_text: where.textContent,
                goal_clamp: getComputedStyle(card.querySelector('.task-goal')).webkitLineClamp,
              };
            })()
            """
        )
        page.shot(str(SHOTS / "task-card-390.png"))
        assert narrow["page_overflow"] <= 1, narrow
        assert narrow["card_overflow"] <= 1, narrow
        assert narrow["card_right"] <= narrow["stream_right"] + 1, narrow
        assert narrow["card_left"] >= narrow["stream_left"] - 1, narrow
        assert narrow["white_space"] == "nowrap", narrow
        assert narrow["where_h"] <= 24, narrow
        # 窄屏菜单是 44px 触控尺寸，顶栏大约 8+44+8。路径本身仍是单行省略。
        assert narrow["top_h"] <= 68, narrow
        assert "long-workspace-path-" in narrow["where_title"], narrow
        assert narrow["where_text"].endswith("…") or "…" in narrow["where_text"] or len(narrow["where_text"]) < len(narrow["where_title"]), narrow
        assert narrow["goal_clamp"] == "2", narrow
    finally:
        proc.kill()
        proc.wait(timeout=10)
        httpd.shutdown()
        httpd.server_close()
