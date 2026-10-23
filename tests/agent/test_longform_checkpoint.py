"""长文中断后按 session 和 artifact 恢复，成功后再清掉 checkpoint。"""

import json
from pathlib import Path

import pytest

from spoolkit.agent.artifacts import ArtifactStore
from spoolkit.agent.chat_sink import RecorderSink
from spoolkit.agent.document_bodies import InMemoryBodyStore
from spoolkit.agent.document_checkpoint import (
    CheckpointError,
    DocumentCheckpointStore,
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


def _state(*, deliverable: str = "chat", path: str = "") -> DocumentState:
    outline = tuple(
        OutlineEntry(index=index, title=title, points=f"{title}的要点", target_chars=1000)
        for index, title in enumerate(TITLES)
    )
    return DocumentState(
        goal="写一份估算说明",
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
) -> tuple[LongFormRunner, ScriptedGateway, DocumentCheckpointStore]:
    store = DocumentCheckpointStore(root)
    gateway = ScriptedGateway(replies)
    runner = LongFormRunner(
        _state(deliverable=deliverable, path=path),
        gateway,  # type: ignore[arg-type]
        InMemoryBodyStore(),
        sink,  # type: ignore[arg-type]
        checkpoint=store,
        session="sess-a",
        artifact_id="doc-1",
        task_id="task-1",
        turn_id="turn-1",
    )
    return runner, gateway, store


def test_中途停止留下_checkpoint_恢复时不重写已完成节(tmp_path: Path) -> None:
    first_sink = RecorderSink()
    first, _gateway, store = _runner(tmp_path, [_piece("窗口")], first_sink)
    stopped = first.run(stop_after=1)
    assert stopped.done is False
    assert stopped.state.phase == "stopped"
    assert stopped.state.status == "paused"
    assert [item.index for item in stopped.state.completed_sections] == [0]
    assert stopped.state.pending_sections == (1, 2)
    checkpoint = store.load("sess-a", "doc-1")
    assert checkpoint is not None
    assert checkpoint.phase == "stopped"
    assert checkpoint.schema_version == SCHEMA_VERSION
    assert "窗口写完。" in checkpoint.body_map()[0]
    assert [item[1] for item in first_sink.events if item[0] == "section_commit"] == [0]
    path = store.path_for("sess-a", "doc-1")
    assert path == tmp_path / ".agent" / "checkpoints" / "sess-a" / "doc-1.json"
    assert path.is_file()

    second_sink = RecorderSink()
    second, gateway, _store = _runner(
        tmp_path,
        [_piece("估算"), _piece("余量"), "完"],
        second_sink,
    )
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
    loaded = store.load("sess-a", "doc-1")
    assert loaded is not None
    assert loaded.state.status == "failed"
    assert store.path_for("sess-a", "doc-1").is_file()


def test_schema不匹配拒绝_坏文件返回空(tmp_path: Path) -> None:
    store = DocumentCheckpointStore(tmp_path)
    missing = store.load("sess-a", "doc-1")
    assert missing is None
    path = store.path_for("sess-a", "doc-1")
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    assert store.load("sess-a", "doc-1") is None
    path.write_text(json.dumps({"schema_version": 2, "session": "sess-a", "artifact_id": "doc-1"}), encoding="utf-8")
    with pytest.raises(SchemaVersionError):
        store.load("sess-a", "doc-1")
    with pytest.raises(CheckpointError):
        store.path_for("../outside", "doc-1")


def test_file恢复后只登记一次待确认(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path, overrides={}, run_id="run-1")
    artifacts = ArtifactStore(tmp_path)
    target = tmp_path / "docs" / "out.md"

    def sink() -> FileSink:
        return FileSink(
            state=_state(deliverable="file", path="docs/out.md"),
            path="docs/out.md",
            session="sess-a",
            task_id="task-1",
            turn_id="turn-1",
            artifacts=artifacts,
            pending=pending,
            artifact_id="doc-1",
        )

    first, _gateway, store = _runner(
        tmp_path,
        ["甲节唯一" + _piece("窗口")],
        sink(),
        deliverable="file",
        path="docs/out.md",
    )
    first.run(stop_after=1)
    assert pending.items() == []
    assert store.load("sess-a", "doc-1") is not None

    second_sink = sink()
    second, _again, _store = _runner(
        tmp_path,
        [_piece("估算"), _piece("余量"), "完"],
        second_sink,
        deliverable="file",
        path="docs/out.md",
    )
    finished = second.run()
    assert finished.done is True
    assert len(pending.items()) == 1
    assert pending.items()[0].path == "docs/out.md"
    text = pending.items()[0].new_text
    assert text.count("甲节唯一") == 1
    assert not target.exists()
    assert not store.path_for("sess-a", "doc-1").exists()
    assert second_sink.pending is pending.items()[0]
