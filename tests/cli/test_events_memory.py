"""`--events`（网页壳 / QQ）也要蒸馏，并把最近几轮带进本轮提示。"""

import argparse
from pathlib import Path
from types import SimpleNamespace

from spoolkit.agent.loop import LoopResult
from spoolkit.agent.state import TaskState
from spoolkit.cli.runtime import distiller_for
from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.transcript import ASSISTANT, USER, record_message
from spoolkit.store.db import open_db
from spoolkit.tools.edit import PendingChanges


def test_假模型不装蒸馏器() -> None:
    assert distiller_for(SimpleNamespace(provider="fake"), object(), object()) is None


def test_真实供应商装蒸馏器() -> None:
    assert callable(distiller_for(SimpleNamespace(provider="llamacpp"), object(), object()))


def test_没记忆就不装() -> None:
    assert distiller_for(SimpleNamespace(provider="llamacpp"), object(), None) is None


def test_events路径把蒸馏器和上一轮带给循环(tmp_path: Path, monkeypatch) -> None:
    """QQ 上「厦门」下一句「那天气呢」：本轮提示要带上上一句，结束要蒸馏。"""
    seen: dict = {}

    class FakeLoop:
        def run(self, goal, resume=False, prompt=None, **_kwargs):
            seen["goal"] = goal
            seen["prompt"] = prompt
            return LoopResult(
                finished=True,
                final="好",
                state=TaskState(task_id="t", goal=goal),
                steps=1,
                resets=0,
            )

    def fake_assemble(_root, _gateway, config=None, wiring=None):
        del config
        seen["distiller"] = getattr(wiring, "distiller", None)
        return FakeLoop()

    monkeypatch.setattr("spoolkit.cli.commands.run.assemble_loop", fake_assemble)
    monkeypatch.setattr("spoolkit.cli.commands.run.web_access", lambda *a, **k: None)
    monkeypatch.setattr("spoolkit.cli.commands.run.web_search_backend", lambda *a, **k: None)

    (tmp_path / ".agent").mkdir()
    conn = open_db(tmp_path / ".agent" / "memory.db")
    init_memory_schema(conn)
    record_message(conn, "bridge", USER, "厦门")
    record_message(conn, "bridge", ASSISTANT, "近两天多云")
    conn.close()

    args = argparse.Namespace(
        root=str(tmp_path),
        goal="那天气呢",
        session="bridge",
        provider="llamacpp",
        model="",
        no_memory=False,
        max_steps=4,
        subagent_steps=4,
        no_supervise=True,
        step_ceiling=0,
        resume=False,
        allow_read=[],
        policy="ask",
        scope="",
        web=False,
        ask_on_stdin=False,
        from_channel=True,
        no_mcp=True,
        events=True,
        max_targets=0,
        review_limit=0,
    )
    from spoolkit.cli.commands.run import _events_mode

    assert _events_mode(args, tmp_path, object(), 4096, PendingChanges(tmp_path)) == 0
    assert seen["distiller"] is not None
    assert seen["goal"] == "那天气呢"
    assert "厦门" in (seen["prompt"] or "")
    assert "当前：那天气呢" in (seen["prompt"] or "")
