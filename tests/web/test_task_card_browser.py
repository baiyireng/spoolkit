"""真实 Chrome 验收任务卡：会话记录 + task-events 走 /state，不靠假 DOM。"""

from __future__ import annotations

import json
import os
import shutil
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


def _px(value: str) -> float:
    return float(str(value).replace("px", ""))


def _columns(page) -> dict:
    return page.eval(
        r"""
        (() => {
          const rect = (node) => {
            const box = node.getBoundingClientRect();
            return { l: Math.round(box.left), r: Math.round(box.right) };
          };
          const col = rect(document.querySelector('#stream .col'));
          const box = rect(document.querySelector('#composer .box'));
          const tools = rect(document.getElementById('composer-tools'));
          const hint = rect(document.querySelector('#composer .st'));
          const bubble = getComputedStyle(document.querySelector('.msg.assistant .bubble'));
          const side = getComputedStyle(document.getElementById('sidebar'));
          const body = getComputedStyle(document.body);
          return {
            col_l: col.l, col_r: col.r,
            box_l: box.l, box_r: box.r,
            tools_l: tools.l, tools_r: tools.r,
            hint_l: hint.l, hint_r: hint.r,
            bubble_lh: bubble.lineHeight,
            bubble_size: bubble.fontSize,
            side_lh: side.lineHeight,
            font: body.fontFamily,
            tracking: body.letterSpacing,
          };
        })()
        """
    )


def _assert_columns(edges: dict) -> None:
    assert abs(edges["col_l"] - edges["box_l"]) <= 1, edges
    assert abs(edges["col_r"] - edges["box_r"]) <= 1, edges
    assert abs(edges["tools_l"] - edges["box_l"]) <= 1, edges
    assert abs(edges["tools_r"] - edges["box_r"]) <= 1, edges
    assert abs(edges["hint_l"] - edges["box_l"]) <= 1, edges
    assert abs(edges["hint_r"] - edges["box_r"]) <= 1, edges
    assert edges["bubble_size"] == "15px", edges
    assert abs(_px(edges["bubble_lh"]) - 24.75) <= 0.6, edges
    assert _px(edges["side_lh"]) <= 20, edges
    assert "MiSans" in edges["font"], edges
    assert abs(_px(edges["tracking"]) - 0.45) <= 0.05, edges


def _topbar(page) -> dict:
    return page.eval(
        r"""
        (() => {
          const top = document.getElementById('top');
          const brand = document.querySelector('.brand');
          const name = document.querySelector('.brand-name');
          const mark = document.querySelector('.brand-mark');
          const rule = document.querySelector('.top-rule');
          const tasks = document.getElementById('tasks-btn');
          const where = document.getElementById('where');
          const nameStyle = getComputedStyle(name);
          const markBox = mark.getBoundingClientRect();
          const tasksBox = tasks.getBoundingClientRect();
          return {
            label: brand.getAttribute('aria-label'),
            title: brand.getAttribute('title'),
            name: name.textContent,
            name_size: nameStyle.fontSize,
            name_weight: String(nameStyle.fontWeight),
            name_track: parseFloat(nameStyle.letterSpacing),
            name_display: nameStyle.display,
            mark_w: Math.round(markBox.width),
            mark_h: Math.round(markBox.height),
            rule: getComputedStyle(rule).display,
            wrap: getComputedStyle(top).flexWrap,
            where_ws: getComputedStyle(where).whiteSpace,
            top_h: Math.round(top.getBoundingClientRect().height),
            tasks_l: Math.round(tasksBox.left),
            tasks_r: Math.round(tasksBox.right),
            view_w: window.innerWidth,
            overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          };
        })()
        """
    )


def _assert_topbar(bar: dict, *, narrow: bool) -> None:
    assert bar["label"] == "spool", bar
    assert bar["title"] == "spool", bar
    assert bar["name"] == "spool", bar
    assert bar["name_display"] != "none", bar
    assert bar["name_size"] == "15px", bar
    assert bar["name_weight"] in ("650", "700"), bar
    assert bar["name_track"] < 0, bar
    assert 18 <= bar["mark_w"] <= 20, bar
    assert 18 <= bar["mark_h"] <= 20, bar
    assert bar["wrap"] == "nowrap", bar
    assert bar["where_ws"] == "nowrap", bar
    assert bar["top_h"] <= 68, bar
    assert bar["rule"] == ("none" if narrow else "block"), bar
    assert bar["tasks_l"] >= 0, bar
    assert bar["tasks_r"] <= bar["view_w"] + 1, bar
    assert bar["overflow"] <= 1, bar


def _status_box(page) -> dict:
    return page.eval(
        r"""
        (() => {
          const panel = document.getElementById('st-panel');
          const hints = document.getElementById('hints');
          const toasts = document.getElementById('toasts');
          const close = document.getElementById('st-close');
          const composer = document.getElementById('composer').getBoundingClientRect();
          const box = panel.getBoundingClientRect();
          const hintBox = hints.getBoundingClientRect();
          const toastBox = toasts.getBoundingClientRect();
          const closeBox = close.getBoundingClientRect();
          const scrim = getComputedStyle(document.body, '::before').content;
          return {
            position: getComputedStyle(panel).position,
            w: Math.round(box.width),
            left: Math.round(box.left),
            right: Math.round(box.right),
            top: Math.round(box.top),
            bottom: Math.round(box.bottom),
            hints_bottom: Math.round(hintBox.bottom),
            toasts_top: Math.round(toastBox.top),
            toasts_shown: getComputedStyle(toasts).display !== 'none',
            close_h: Math.round(closeBox.height),
            scrim: scrim,
            composer_bottom: Math.round(composer.bottom),
            view_w: window.innerWidth,
            view_h: window.innerHeight,
            overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          };
        })()
        """
    )


def _shot_actions(page, name: str) -> None:
    box = page.eval(
        r"""
        (() => {
          const top = document.getElementById('top').getBoundingClientRect();
          const brand = document.querySelector('.brand').getBoundingClientRect();
          const tasks = document.getElementById('tasks-btn').getBoundingClientRect();
          const status = document.getElementById('st-health').getBoundingClientRect();
          const hidden = ['st-mode', 'st-target', 'st-pair', 'st-elsewhere'].map((id) => {
            const node = document.getElementById(id);
            return getComputedStyle(node).display;
          });
          const chrome = [
            document.getElementById('top').innerText,
            document.getElementById('st-health').innerText,
            document.getElementById('tasks-btn').innerText,
          ].join('\n');
          return {
            word: document.querySelector('#st-health .st-label').textContent,
            hidden: hidden,
            chrome: chrome,
            brand_l: Math.round(brand.left),
            tasks_l: Math.round(tasks.left),
            tasks_r: Math.round(tasks.right),
            status_l: Math.round(status.left),
            status_r: Math.round(status.right),
            status_t: Math.round(status.top),
            status_b: Math.round(status.bottom),
            top_t: Math.round(top.top),
            top_b: Math.round(top.bottom),
            view_w: window.innerWidth,
            overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            wait: document.getElementById('tasks-wait').textContent,
            count: document.getElementById('tasks-count').textContent,
          };
        })()
        """
    )
    page.shot(str(SHOTS / name))
    assert box["word"] in ("正常", "异常", "未连接"), box
    assert box["hidden"] == ["none", "none", "none", "none"], box
    for old in ("已钉住", "等审批", "模型探活", "目标未钉住", "点开查看是哪些"):
        assert old not in box["chrome"], (old, box["chrome"])
    assert box["brand_l"] >= 0, box
    assert box["tasks_l"] > box["brand_l"], box
    assert box["status_l"] >= box["tasks_r"] - 4, box
    assert box["status_r"] <= box["view_w"] + 1, box
    assert box["status_t"] >= box["top_t"] - 1, box
    assert box["status_b"] <= box["top_b"] + 8, box
    assert box["overflow"] <= 1, box
    assert box["count"] != "", box


def _shot_status(page, name: str, *, narrow: bool) -> None:
    page.eval("document.getElementById('st-health').click()")
    assert _wait(page, "!document.getElementById('st-panel').classList.contains('hidden')"), "状态面板没有打开"
    assert _wait(
        page,
        "getComputedStyle(document.getElementById('st-panel')).opacity === '1'",
    ), "状态面板入场还没停"
    time.sleep(0.2)
    box = _status_box(page)
    page.shot(str(SHOTS / name))
    page.eval("document.getElementById('st-close').click()")
    assert _wait(page, "document.getElementById('st-panel').classList.contains('hidden')"), "状态面板没有关上"
    assert box["overflow"] <= 1, box
    assert box["composer_bottom"] <= box["view_h"] + 1, box
    assert box["close_h"] >= (44 if narrow else 24), box
    if narrow:
        assert box["position"] == "fixed", box
        assert box["left"] <= 1, box
        assert box["right"] >= box["view_w"] - 1, box
        assert box["bottom"] >= box["view_h"] - 8, box
        assert box["hints_bottom"] <= box["top"] + 1, box
        assert box["scrim"] != "none", box
    else:
        assert box["position"] == "static", box
        assert 340 <= box["w"] <= 380, box
        if box["toasts_shown"]:
            assert box["toasts_top"] >= box["bottom"] - 1, box


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
    other_events = root / ".agent" / "task-events" / "other.jsonl"
    other_events.parent.mkdir(parents=True, exist_ok=True)
    other_events.write_text(
        "\n".join(
            [
                json.dumps(
                    _event("other-1", "other", 1, "understand", "running", "其他会话的任务"),
                    ensure_ascii=False,
                ),
                json.dumps(
                    _event(
                        "other-1", "other", 2, "approval", "await", "待确认命令",
                        tool_call_id="tc-other",
                    ),
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    later = time.time() + 60
    os.utime(other_events, (later, later))
    from spoolkit.kernel.sessions import write_meta
    write_meta(root, "other", title="其他会话的很长标题用来检查窄屏")
    write_meta(root, "t", title="整理配置")

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
              const bubble = liveMsg.querySelector('.bubble').getBoundingClientRect();
              const composer = document.getElementById('composer').getBoundingClientRect();
              const send = document.getElementById('send').getBoundingClientRect();
              const lineStyle = getComputedStyle(live.querySelector('.task-row-line'));
              const jumpStyle = getComputedStyle(live.querySelector('.task-jump'));
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
                chip: live.querySelector('.task-mark').textContent,
                done_mark: done.querySelector('.task-mark').textContent,
                row_display: lineStyle.display,
                jump_border: parseFloat(jumpStyle.borderTopWidth),
                gap: Math.round(card.top - bubble.bottom),
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
        assert detail["done_mark"] == "完成", detail
        assert detail["row_display"] == "grid", detail
        assert detail["jump_border"] >= 1, detail
        assert 6 <= detail["gap"] <= 16, detail
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
        wide_cols = _columns(page)
        _assert_columns(wide_cols)
        _assert_topbar(_topbar(page), narrow=False)
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.shot(str(SHOTS / "task-card-1440.png"))
        shutil.copyfile(SHOTS / "task-card-1440.png", SHOTS / "task-card-visual-1440.png")
        shutil.copyfile(SHOTS / "task-card-1440.png", SHOTS / "shell-visual-1440.png")
        shutil.copyfile(SHOTS / "task-card-1440.png", SHOTS / "type-composer-1440.png")
        shutil.copyfile(SHOTS / "task-card-1440.png", SHOTS / "brand-topbar-1440.png")
        _shot_actions(page, "top-actions-1440.png")
        _shot_status(page, "overlays-visual-1440.png", narrow=False)

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
        narrow_cols = _columns(page)
        _assert_columns(narrow_cols)
        _assert_topbar(_topbar(page), narrow=True)
        page.shot(str(SHOTS / "task-card-390.png"))
        shutil.copyfile(SHOTS / "task-card-390.png", SHOTS / "task-card-visual-390.png")
        shutil.copyfile(SHOTS / "task-card-390.png", SHOTS / "shell-visual-390.png")
        shutil.copyfile(SHOTS / "task-card-390.png", SHOTS / "type-composer-390.png")
        shutil.copyfile(SHOTS / "task-card-390.png", SHOTS / "brand-topbar-390.png")
        _shot_actions(page, "top-actions-390.png")
        _shot_status(page, "overlays-visual-390.png", narrow=True)
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
        page.eval("document.getElementById('tasks-btn').click()")
        assert _wait(page, "!document.getElementById('tasks-panel').classList.contains('hidden')"), "任务面板没有打开"
        page.eval("document.querySelector('#tasks-list .task-line').click()")
        assert _wait(page, "!!document.querySelector('#tasks-detail .task-card')"), "任务详情没有展开"
        panel = page.eval(
            r"""
            (() => {
              const box = document.getElementById('tasks-panel');
              const body = document.getElementById('tasks-body');
              const goal = document.querySelector('#tasks-list .task-goal');
              const composer = document.getElementById('composer').getBoundingClientRect();
              const panelBox = box.getBoundingClientRect();
              const bodyStyle = getComputedStyle(body);
              const panelStyle = getComputedStyle(box);
              return {
                count: document.getElementById('tasks-count').textContent,
                wait: document.getElementById('tasks-wait').textContent,
                search_left: Math.round(document.getElementById('tasks-search').getBoundingClientRect().left),
                search_right: Math.round(document.getElementById('tasks-search').getBoundingClientRect().right),
                search_w: Math.round(document.getElementById('tasks-search').getBoundingClientRect().width),
                scope_left: Math.round(document.getElementById('tasks-scope').getBoundingClientRect().left),
                scope_right: Math.round(document.getElementById('tasks-scope').getBoundingClientRect().right),
                search_type: document.getElementById('tasks-search').getAttribute('type'),
                search_label: document.getElementById('tasks-search').getAttribute('aria-label'),
                goal_clamp: getComputedStyle(goal).webkitLineClamp,
                goal_title: goal.title,
                tool: document.querySelector('#tasks-detail .task-tool').textContent,
                jump: document.querySelector('#tasks-detail .task-jump').textContent,
                page_overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                panel_overflow: box.scrollWidth - box.clientWidth,
                panel_left: Math.round(panelBox.left),
                panel_right: Math.round(panelBox.right),
                panel_position: panelStyle.position,
                body_y: bodyStyle.overflowY,
                composer_bottom: Math.round(composer.bottom),
                view_w: window.innerWidth,
                view_h: window.innerHeight,
              };
            })()
            """
        )
        page.shot(str(SHOTS / "task-panel-390.png"))
        page.shot(str(SHOTS / "task-search-390.png"))
        assert panel["count"] == "2", panel
        assert panel["search_type"] == "search", panel
        assert panel["search_label"] == "搜索任务", panel
        assert panel["search_right"] <= panel["view_w"] + 1, panel
        assert panel["search_left"] >= panel["panel_left"] - 1, panel
        assert panel["search_w"] > 40, panel
        assert panel["scope_right"] <= panel["view_w"] + 1, panel
        assert panel["scope_left"] >= panel["panel_left"] - 1, panel
        assert panel["wait"] == "1", panel
        assert panel["goal_clamp"] == "2", panel
        assert panel["goal_title"].startswith("把配置文件整理成可提交的说明"), panel
        assert panel["tool"] == "read_file", panel
        assert panel["jump"] == "查看改动", panel
        assert panel["page_overflow"] <= 1, panel
        assert panel["panel_overflow"] <= 1, panel
        assert panel["panel_left"] <= 1, panel
        assert panel["panel_right"] <= panel["view_w"] + 1, panel
        assert panel["panel_position"] == "fixed", panel
        assert panel["body_y"] in ("auto", "scroll"), panel
        assert panel["composer_bottom"] <= panel["view_h"] + 1, panel
        assert _wait(
            page,
            "document.getElementById('tasks-history').textContent.indexOf('正在加载') < 0",
        ), "任务历史没有加载完"
        page.eval("document.querySelector('#tasks-scope [data-scope=\"all\"]').click()")
        assert _wait(
            page,
            "!![...document.querySelectorAll('#tasks-list .task-goal')].find((node) => node.textContent === '其他会话的任务')",
        ), "全部会话里没有其他会话的任务"
        page.eval(
            """
            [...document.querySelectorAll('#tasks-list .task-line')].find((node) => {
              const goal = node.querySelector('.task-goal');
              return goal && goal.textContent === '其他会话的任务';
            }).click()
            """
        )
        history = page.eval(
            r"""
            (() => {
              const box = document.getElementById('tasks-panel');
              const chip = document.querySelector('#tasks-list .task-session');
              const chipBox = chip.getBoundingClientRect();
              const detail = document.getElementById('tasks-detail');
              return {
                count: document.getElementById('tasks-count').textContent,
                chip: chip.textContent,
                chip_right: Math.round(chipBox.right),
                chip_left: Math.round(chipBox.left),
                note: detail.textContent.indexOf('该任务属于其他会话') >= 0,
                jumps: detail.querySelectorAll('.task-jump').length,
                panel_overflow: box.scrollWidth - box.clientWidth,
                panel_right: Math.round(box.getBoundingClientRect().right),
                view_w: window.innerWidth,
              };
            })()
            """
        )
        page.shot(str(SHOTS / "task-history-390.png"))
        assert history["count"] == "2", history
        assert history["chip"] == "其他会话的很长标题用来检查窄屏", history
        assert history["note"] is True, history
        assert history["jumps"] == 0, history
        assert history["chip_right"] <= history["panel_right"] + 1, history
        assert history["chip_left"] >= 0, history
        assert history["panel_overflow"] <= 1, history
        assert history["panel_right"] <= history["view_w"] + 1, history
        page.eval("document.getElementById('tasks-close').click()")
        assert _wait(
            page,
            "document.getElementById('tasks-panel').classList.contains('hidden')",
        ), "任务面板没有关上"
        page.eval("document.getElementById('menu').click()")
        assert _wait(page, "!!document.querySelector('#sessions .rename')"), "当前会话没有重命名入口"
        page.eval("document.querySelector('#sessions .rename').click()")
        assert _wait(
            page,
            "!document.getElementById('rename').classList.contains('hidden')",
        ), "重命名对话框没有打开"
        rename = page.eval(
            r"""
            (() => {
              const card = document.querySelector('.rename-card');
              const input = document.getElementById('rename-input');
              const cardBox = card.getBoundingClientRect();
              const inputBox = input.getBoundingClientRect();
              const dialog = document.getElementById('rename');
              return {
                value: input.value,
                role: dialog.getAttribute('role'),
                modal: dialog.getAttribute('aria-modal'),
                card_left: Math.round(cardBox.left),
                card_right: Math.round(cardBox.right),
                input_left: Math.round(inputBox.left),
                input_right: Math.round(inputBox.right),
                view_w: window.innerWidth,
                page_overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
              };
            })()
            """
        )
        page.shot(str(SHOTS / "session-rename-390.png"))
        assert rename["value"] == "整理配置", rename
        assert rename["role"] == "dialog", rename
        assert rename["modal"] == "true", rename
        assert rename["card_left"] >= 0, rename
        assert rename["card_right"] <= rename["view_w"] + 1, rename
        assert rename["input_left"] >= rename["card_left"] - 1, rename
        assert rename["input_right"] <= rename["card_right"] + 1, rename
        assert rename["page_overflow"] <= 1, rename
        page.eval("document.getElementById('rename-cancel').click()")
        page.eval("document.getElementById('scrim').click()")
        page.viewport(360, 800, mobile=True)
        assert _wait(
            page,
            "document.getElementById('sidebar').getBoundingClientRect().right <= 1",
            timeout=4,
        ), "360 侧栏没有收起"
        tight = page.eval(
            r"""
            (() => {
              const card = document.querySelector('.task-card-running');
              const body = card.querySelector('.task-body');
              const bodyStyle = getComputedStyle(body);
              return {
                page_overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                card_overflow: card.scrollWidth - card.clientWidth,
                body_x: bodyStyle.overflowX,
                body_max_px: Math.round(parseFloat(bodyStyle.maxHeight)),
                card_right: Math.round(card.getBoundingClientRect().right),
                view_w: window.innerWidth,
                view_h: window.innerHeight,
              };
            })()
            """
        )
        assert tight["page_overflow"] <= 1, tight
        assert tight["card_overflow"] <= 1, tight
        assert tight["card_right"] <= tight["view_w"] + 1, tight
        assert tight["body_x"] in ("hidden", "clip", "auto"), tight
        assert tight["body_max_px"] <= tight["view_h"] * 0.46 + 1, tight
    finally:
        proc.kill()
        proc.wait(timeout=10)
        httpd.shutdown()
        httpd.server_close()
