"""一轮结束时把产物和结果写进会话摘要。失败不改变主任务结果。"""

import json
from pathlib import Path

import pytest

from spoolkit.agent.artifacts import ArtifactRecord
from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.session_summary import (
    TEXT_CAP,
    SessionSummaryStore,
    record_session_outcome,
)
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.registry import ToolRegistry

STAMP = "2026-10-23T00:00:00Z"
BODY = "只有正文文件里才有的尾巴标记甲乙丙丁。"


def _record(session: str = "sess-a", artifact_id: str = "doc-1") -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact_id,
        session=session,
        task_id="task",
        turn_id="t0",
        kind="document",
        title="估算说明",
        summary="三节估算。",
        created_at=STAMP,
        path=f".agent/artifacts/{session}/abc",
    )


def _write(tmp_path: Path, **kwargs: object) -> SessionSummaryStore:
    store = SessionSummaryStore(tmp_path, "sess-a")
    payload = dict(
        session="sess-a",
        updated_at=STAMP,
        task_summary="整理估算",
        artifacts=(_record(),),
        finished=True,
        final="写完了估算说明",
        failure_reason="",
        turn_id="turn-1",
    )
    payload.update(kwargs)
    record_session_outcome(store, **payload)  # type: ignore[arg-type]
    return store


def test_成功写入产物完成项和标题(tmp_path: Path) -> None:
    store = _write(tmp_path)
    loaded = store.load()
    assert loaded.headline == "整理估算"
    assert loaded.what_done == ("写完了估算说明",)
    assert loaded.open_items == ()
    assert loaded.artifacts[0].id == "doc-1"
    assert loaded.artifacts[0].title == "估算说明"
    assert loaded.last_turn_id == "turn-1"
    assert BODY not in (tmp_path / ".agent" / "sessions" / "sess-a" / "summary.json").read_text(encoding="utf-8")


def test_过长结论收成摘要(tmp_path: Path) -> None:
    store = _write(tmp_path, final="结" * (TEXT_CAP + 20), artifacts=())
    done = store.load().what_done[0]
    assert len(done) == TEXT_CAP
    assert done.endswith("…")


def test_失败和停止只记未完成(tmp_path: Path) -> None:
    failed = _write(tmp_path, finished=False, final="已达步数上限", failure_reason="模型超时")
    loaded = failed.load()
    assert loaded.what_done == ()
    assert loaded.open_items == ("模型超时",)
    stopped = SessionSummaryStore(tmp_path, "sess-stop")
    record_session_outcome(
        stopped,
        session="sess-stop",
        updated_at=STAMP,
        task_summary="整理估算",
        artifacts=(),
        finished=False,
        final="半截正文",
        failure_reason="已停止",
        turn_id="turn-2",
    )
    assert stopped.load().what_done == ()
    assert stopped.load().open_items == ("已停止",)


def test_空会话不写文件(tmp_path: Path) -> None:
    store = SessionSummaryStore(tmp_path, "sess-a")
    record_session_outcome(
        store,
        session="",
        updated_at=STAMP,
        task_summary="整理估算",
        artifacts=(_record(),),
        finished=True,
        final="写完了",
        failure_reason="",
        turn_id="turn-1",
    )
    assert not (tmp_path / ".agent" / "sessions" / "sess-a" / "summary.json").exists()


def test_重复调用不追加(tmp_path: Path) -> None:
    store = _write(tmp_path)
    _write(tmp_path)
    loaded = store.load()
    assert loaded.what_done == ("写完了估算说明",)
    assert len(loaded.artifacts) == 1
    assert loaded.open_items == ()


def test_不写别的会话产物且已有标题保持不变(tmp_path: Path) -> None:
    store = SessionSummaryStore(tmp_path, "sess-a")
    record_session_outcome(
        store,
        session="sess-a",
        updated_at=STAMP,
        task_summary="原标题",
        artifacts=(),
        finished=True,
        final="第一次",
        failure_reason="",
        turn_id="turn-1",
    )
    record_session_outcome(
        store,
        session="sess-a",
        updated_at=STAMP,
        task_summary="新标题",
        artifacts=(_record("sess-b", "doc-other"),),
        finished=True,
        final="第二次",
        failure_reason="",
        turn_id="turn-2",
    )
    loaded = store.load()
    assert loaded.headline == "原标题"
    assert [item.id for item in loaded.artifacts] == []
    assert "第二次" in loaded.what_done


def test_写入失败被吞掉(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self, path: Path, payload: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(SessionSummaryStore, "_atomic_write", broken)
    store = SessionSummaryStore(tmp_path, "sess-a")
    record_session_outcome(
        store,
        session="sess-a",
        updated_at=STAMP,
        task_summary="整理估算",
        artifacts=(_record(),),
        finished=True,
        final="写完了",
        failure_reason="",
        turn_id="turn-1",
    )
    assert store.load().what_done == ()


def _loop(tmp_path: Path, script: list[str], pending: PendingChanges | None) -> AgentLoop:
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=ToolRegistry(),
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=1,
            supervise=False,
        ),
        pending=pending,
    )


def _turn(done: bool, final: str | None) -> str:
    return json.dumps(
        {"thought": "看一下", "tool_calls": [], "state": None, "done": done, "final": final},
        ensure_ascii=False,
    )


def test_没有会话的短答不写摘要(tmp_path: Path) -> None:
    result = _loop(tmp_path, [_turn(True, "好")], None).run("一句话")
    assert result.finished is True
    assert result.final == "好"
    assert not list((tmp_path / ".agent").rglob("summary.json"))


def test_未完成写入未完成项且失败不影响结果(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = PendingChanges(tmp_path, overrides={}, run_id="run-1")
    pending.run_session = "sess-a"
    result = _loop(tmp_path, [_turn(False, None)], pending).run("还没做完")
    assert result.finished is False
    loaded = SessionSummaryStore(tmp_path, "sess-a").load()
    assert loaded.what_done == ()
    assert loaded.open_items
    assert loaded.headline == "还没做完"

    def broken(self, path: Path, payload: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(SessionSummaryStore, "_atomic_write", broken)
    again = _loop(tmp_path, [_turn(True, "仍然返回")], pending).run("再来一次")
    assert again.finished is True
    assert again.final == "仍然返回"
