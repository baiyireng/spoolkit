"""挂起待批清单必须按**会话**找。

真机探针（`--session probe`）那次：清单明明写在
`.agent/sessions/probe/review/pending-actions.json`，而 `pending_actions_note`
用**默认会话**去找 `.agent/sessions/cli/review/…`——找不到就返回空串，
于是「有 1 个动作挂起待批」这句**静默消失**，用户以为这一轮是普通收工。
写的那一侧（`loop` 的 `pending_path`）一直是带会话的，只有读的这一侧没有。

这三条用例钉的是同一个不变式，从内到外：
1. 按会话找得到（读法本身）；
2. 别的会话的清单不算数（防串味——A 的批复不该去执行 B 的动作）；
3. 收尾那条路**真的把会话传下来了**（防"函数改对了、调用点忘了"）。
"""

import io
import json
from pathlib import Path
from types import SimpleNamespace

from spoolkit import session_state
from spoolkit.cli.commands.run import _settle_pending
from spoolkit.cli.events import EventWriter
from spoolkit.cli.settle import pending_actions_note
from spoolkit.tools.edit import PendingChanges


def _write_pending(root: Path, session: str, actions: list[dict]) -> Path:
    path = session_state.dir_for(root, session) / "review" / "pending-actions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"escalated_triggers": ["T10"], "pending_actions": actions}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_按会话找得到(tmp_path: Path) -> None:
    _write_pending(
        tmp_path,
        "probe",
        [{"trigger": "T10", "action": "", "fingerprint": "a70d7c7f88b3"}],
    )
    note = pending_actions_note(tmp_path, "probe")
    assert "T10" in note
    assert "a70d7c7f88b3" in note


def test_别的会话的清单不算数(tmp_path: Path) -> None:
    """会话是一等公民：A 会话的挂起清单不许被 B 会话读出来。"""
    _write_pending(
        tmp_path,
        "probe",
        [{"trigger": "T10", "action": "", "fingerprint": "a70d7c7f88b3"}],
    )
    assert pending_actions_note(tmp_path, "cli") == ""


def test_不传会话仍按默认会话找(tmp_path: Path) -> None:
    """老行为不破：默认会话（`spool run` 不带 `--session`）照旧读得到。"""
    _write_pending(
        tmp_path,
        session_state.DEFAULT_SESSION,
        [{"trigger": "T7", "action": "删文件", "fingerprint": "f0f0f0f0"}],
    )
    assert "T7" in pending_actions_note(tmp_path)


def test_收尾把会话传下去了(tmp_path: Path) -> None:
    """`_settle_pending` 必须把这次的会话传进读法里。

    这条就是「接线」那一层：函数改对了、调用点忘了，症状与没修一样
    （静默丢一句话），所以由它来钉。
    """
    _write_pending(
        tmp_path,
        "probe",
        [{"trigger": "T10", "action": "", "fingerprint": "a70d7c7f88b3"}],
    )
    buffer = io.StringIO()
    _settle_pending(
        SimpleNamespace(session="probe"),
        tmp_path,
        PendingChanges(tmp_path),
        None,
        writer=EventWriter(buffer),
    )
    payloads = [
        json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()
    ]
    notes = [item for item in payloads if item.get("kind") == "pending_actions"]
    assert notes, f"没有发出挂起清单那条 note：{payloads}"
    assert "T10" in notes[0]["text"]
