"""长文中断后按 session、task、path、deliverable 恢复，成功后再清掉 checkpoint。"""

import json
from pathlib import Path

import pytest

from spoolkit.agent.artifacts import ArtifactRecord, ArtifactStore
from spoolkit.agent.chat_sink import RecorderSink
from spoolkit.agent.document_bodies import InMemoryBodyStore
from spoolkit.agent.document_checkpoint import (
    CheckpointError,
    DocumentCheckpointStore,
    checkpoint_key,
)
from spoolkit.agent.document_state import (
    SCHEMA_VERSION,
    DocumentState,
    OutlineEntry,
    SchemaVersionError,
)
from spoolkit.agent.file_sink import FileSink
from spoolkit.agent.longform import LongFormRunner
from spoolkit.llm.types import ChatRequest, ChatResponse
from spoolkit.tools.edit import PendingChanges

TITLES = ("窗口", "估算", "余量")
GOAL = "写一份估算说明"


class ScriptedGateway:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError("脚本里的模型回复不够")
        text = self._replies.pop(0)
        return ChatResponse(text=text, prompt_tokens=10, completion_tokens=len(text))

    def context_window(self) -> int:
        return 8192


def _piece(title: str, chars: int = 1000) -> str:
    unit = f"{title}写完。"
    text = unit * ((chars // len(unit)) + 1)
    if len(text) > chars:
        text = text[: chars - 1] + "。"
    return text


def _key(
    *,
    task_id: str = "task-1",
    path: str = "",
    deliverable: str = "chat",
    goal: str = GOAL,
) -> str:
    return checkpoint_key(
        session="sess-a",
        task_id=task_id,
        path=path,
        deliverable=deliverable,
        goal=goal,
    )


def _state(
    *,
    deliverable: str = "chat",
    path: str = "",
    goal: str = GOAL,
) -> DocumentState:
    outline = tuple(
        OutlineEntry(index=index, title=title, points=f"{title}的要点", target_chars=1000)
        for index, title in enumerate(TITLES)
    )
    return DocumentState(
        goal=goal,
        outline=outline,
        deliverable=deliverable,  # type: ignore[arg-type]
        path=path,
        phase="plan",
        pending_sections=tuple(range(len(outline))),
    )


def _runner(
    root: Path,
    replies: list[str],
    sink: object,
    *,
    deliverable: str = "chat",
    path: str = "",
    goal: str = GOAL,
    artifact_id: str = "doc-turn-1",
    turn_id: str = "turn-1",
    task_id: str = "task-1",
) -> tuple[LongFormRunner, ScriptedGateway, DocumentCheckpointStore]:
    store = DocumentCheckpointStore(root)
    gateway = ScriptedGateway(replies)
    runner = LongFormRunner(
        _state(deliverable=deliverable, path=path, goal=goal),
        gateway,  # type: ignore[arg-type]
        InMemoryBodyStore(),
        sink,  # type: ignore[arg-type]
        checkpoint=store,
        session="sess-a",
        artifact_id=artifact_id,
        task_id=task_id,
        turn_id=turn_id,
    )
    return runner, gateway, store


def test_checkpoint_key_不含_turn_且可复现() -> None:
    chat = _key()
    assert chat == _key()
    assert chat.startswith("ck-")
    assert len(chat) == 19
    assert _key(goal="另一份估算") != chat
    assert _key(task_id="task-2") != chat
    same_file = _key(path="docs/out.md", deliverable="file", goal="甲")
    assert same_file == _key(path="docs/out.md", deliverable="file", goal="乙")
    assert same_file != _key(path="docs/other.md", deliverable="file")


def test_第二轮换_artifact_仍恢复且只提交后两节(tmp_path: Path) -> None:
    first_sink = RecorderSink()
    first, _gateway, store = _runner(tmp_path, [_piece("窗口")], first_sink)
    stopped = first.run(stop_after=1)
    assert stopped.done is False
    assert stopped.state.phase == "stopped"
    assert stopped.state.status == "paused"
    assert [item.index for item in stopped.state.completed_sections] == [0]
    assert stopped.state.pending_sections == (1, 2)
    key = _key()
    checkpoint = store.load("sess-a", key)
    assert checkpoint is not None
    assert checkpoint.checkpoint_key == key
    assert checkpoint.artifact_id == "doc-turn-1"
    assert checkpoint.turn_id == "turn-1"
    assert checkpoint.task_id == "task-1"
    assert checkpoint.status == "paused"
    assert checkpoint.phase == "stopped"
    assert checkpoint.schema_version == SCHEMA_VERSION
    assert "窗口写完。" in checkpoint.body_map()[0]
    assert [item[1] for item in first_sink.events if item[0] == "section_commit"] == [0]
    path = store.path_for("sess-a", key)
    assert path == tmp_path / ".agent" / "checkpoints" / "sess-a" / f"{key}.json"
    assert path.is_file()
    assert store.load("sess-b", key) is None

    second_sink = RecorderSink()
    second, gateway, _store = _runner(
        tmp_path,
        [_piece("估算"), _piece("余量"), "完"],
        second_sink,
        artifact_id="doc-turn-2",
        turn_id="turn-2",
    )
    assert second._artifact_id == "doc-turn-2"
    assert second._checkpoint_key == key
    finished = second.run()
    assert finished.done is True
    assert [item[1] for item in second_sink.events if item[0] == "section_commit"] == [1, 2]
    assert not any(item[0] == "section_commit" and item[1] == 0 for item in second_sink.events)
    assert not path.exists()
    prompts = "\n".join(message.content for request in gateway.requests for message in request.messages)
    assert "未开始标题：## 窗口" not in prompts
    assert "当前节：第 1 节「窗口」" not in prompts


def test_失败保留_checkpoint(tmp_path: Path) -> None:
    runner, _gateway, store = _runner(
        tmp_path,
        [_piece("窗口"), _piece("估算"), _piece("余量"), "还没完"],
        RecorderSink(),
    )
    failed = runner.run()
    assert failed.done is False
    assert failed.state.status == "failed"
    loaded = store.load("sess-a", _key())
    assert loaded is not None
    assert loaded.status == "failed"
    assert loaded.state.status == "failed"
    assert store.path_for("sess-a", _key()).is_file()


def test_schema不匹配拒绝_坏文件返回空(tmp_path: Path) -> None:
    store = DocumentCheckpointStore(tmp_path)
    missing = store.load("sess-a", "ck-test")
    assert missing is None
    path = store.path_for("sess-a", "ck-test")
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    assert store.load("sess-a", "ck-test") is None
    path.write_text(
        json.dumps({"schema_version": 2, "session": "sess-a", "checkpoint_key": "ck-test"}),
        encoding="utf-8",
    )
    with pytest.raises(SchemaVersionError):
        store.load("sess-a", "ck-test")
    with pytest.raises(CheckpointError):
        store.path_for("../outside", "ck-test")


def test_file第二轮换_artifact_只登记一次且不覆盖旧产物(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path, overrides={}, run_id="run-1")
    artifacts = ArtifactStore(tmp_path)
    target = tmp_path / "docs" / "out.md"
    key = _key(path="docs/out.md", deliverable="file")

    def sink(artifact_id: str, turn_id: str) -> FileSink:
        return FileSink(
            state=_state(deliverable="file", path="docs/out.md"),
            path="docs/out.md",
            session="sess-a",
            task_id="task-1",
            turn_id=turn_id,
            artifacts=artifacts,
            pending=pending,
            artifact_id=artifact_id,
        )

    first, _gateway, store = _runner(
        tmp_path,
        ["甲节唯一" + _piece("窗口")],
        sink("doc-turn-1", "turn-1"),
        deliverable="file",
        path="docs/out.md",
    )
    first.run(stop_after=1)
    assert pending.items() == []
    paused = store.load("sess-a", key)
    assert paused is not None
    assert paused.artifact_id == "doc-turn-1"
    artifacts.put(
        ArtifactRecord(
            id="doc-turn-1",
            session="sess-a",
            task_id="task-1",
            turn_id="turn-1",
            kind="document",
            title="旧产物",
            summary="旧摘要",
            created_at="2026-01-01T00:00:00Z",
        ),
        "OLD-ARTIFACT-BODY",
    )
    old_record, old_text = artifacts.get("sess-a", "doc-turn-1")
    old_bytes = (tmp_path / old_record.path).read_bytes()

    second_sink = sink("doc-turn-2", "turn-2")
    second, _again, _store = _runner(
        tmp_path,
        [_piece("估算"), _piece("余量"), "完"],
        second_sink,
        deliverable="file",
        path="docs/out.md",
        artifact_id="doc-turn-2",
        turn_id="turn-2",
    )
    finished = second.run()
    assert finished.done is True
    assert second_sink.record is not None
    assert second_sink.record.id == "doc-turn-2"
    assert len(pending.items()) == 1
    assert pending.items()[0].path == "docs/out.md"
    text = pending.items()[0].new_text
    assert text.count("甲节唯一") == 1
    assert text.count("## 窗口") == 1
    assert text.count("## 估算") == 1
    assert text.count("## 余量") == 1
    assert not target.exists()
    assert not store.path_for("sess-a", key).exists()
    assert second_sink.pending is pending.items()[0]
    kept_record, kept_text = artifacts.get("sess-a", "doc-turn-1")
    assert kept_text == old_text == "OLD-ARTIFACT-BODY"
    assert kept_record.content_hash == old_record.content_hash
    assert (tmp_path / old_record.path).read_bytes() == old_bytes
    new_record, new_text = artifacts.get("sess-a", "doc-turn-2")
    assert new_record.id == "doc-turn-2"
    assert "甲节唯一" in new_text
    assert new_text != kept_text


def test_换_path_task_或_goal_不恢复(tmp_path: Path) -> None:
    first_sink = RecorderSink()
    first, _gateway, store = _runner(
        tmp_path,
        [_piece("窗口")],
        first_sink,
        deliverable="file",
        path="docs/out.md",
    )
    first.run(stop_after=1)
    original = _key(path="docs/out.md", deliverable="file")
    assert store.load("sess-a", original) is not None

    other_path_sink = RecorderSink()
    other_path, _gateway, _store = _runner(
        tmp_path,
        [_piece("窗口")],
        other_path_sink,
        deliverable="file",
        path="docs/other.md",
        artifact_id="doc-other-path",
        turn_id="turn-2",
    )
    other_path.run(stop_after=1)
    assert [item[1] for item in other_path_sink.events if item[0] == "section_commit"] == [0]
    assert store.load("sess-a", original) is not None
    assert "窗口写完。" in store.load("sess-a", original).body_map()[0]

    other_task_sink = RecorderSink()
    other_task, _gateway, _store = _runner(
        tmp_path,
        [_piece("窗口")],
        other_task_sink,
        deliverable="file",
        path="docs/out.md",
        task_id="task-2",
        artifact_id="doc-other-task",
        turn_id="turn-3",
    )
    other_task.run(stop_after=1)
    assert [item[1] for item in other_task_sink.events if item[0] == "section_commit"] == [0]
    assert store.path_for("sess-a", original).is_file()

    chat_sink = RecorderSink()
    chat, _gateway, _store = _runner(tmp_path, [_piece("窗口")], chat_sink)
    chat.run(stop_after=1)
    chat_key = _key()
    other_goal_sink = RecorderSink()
    other_goal, _gateway, _store = _runner(
        tmp_path,
        [_piece("窗口")],
        other_goal_sink,
        goal="另一份估算",
        artifact_id="doc-other-goal",
        turn_id="turn-4",
    )
    other_goal.run(stop_after=1)
    assert [item[1] for item in other_goal_sink.events if item[0] == "section_commit"] == [0]
    assert store.load("sess-a", chat_key) is not None
    assert store.load("sess-a", _key(goal="另一份估算")) is not None
