"""会话记忆只在一次 run 开始时构建，再按配额注入控制提示。"""

import json
from pathlib import Path

import pytest

from spoolkit.agent.artifacts import ArtifactRecord, ArtifactStore
from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.session_summary import SessionSummaryStore
from spoolkit.config import Config
from spoolkit.context.budget import Budget
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry

BODY = "BODY-ONLY-ON-DISK-甲乙丙丁"
OTHER = "别的会话标题"


def _turn(thought: str, calls=None, final=None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _pending(root: Path, session: str) -> PendingChanges:
    pending = PendingChanges(root, overrides={}, run_id="run-1")
    pending.run_session = session
    return pending


def _loop(
    tmp_path: Path,
    script: list[str],
    *,
    pending: PendingChanges | None,
    events: list | None = None,
    overrides: dict | None = None,
    v2: bool = False,
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    merged = dict(overrides or {})
    if v2:
        merged["protocol_v2"] = 1
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=4,
            supervise=False,
            overrides=merged,
        ),
        pending=pending,
        on_event=None if events is None else (lambda kind, data: events.append((kind, dict(data)))),
    )


def _seed(root: Path, session: str) -> None:
    ArtifactStore(root).put(
        ArtifactRecord(
            id="doc-seed",
            session=session,
            task_id="task",
            turn_id="t0",
            kind="document",
            title="估算说明",
            summary="三节估算。",
            created_at="2026-01-01T00:00:00+00:00",
        ),
        f"{BODY}\n正文很长，不该进提示。",
    )
    SessionSummaryStore(root, session).record_done(
        "写过估算大纲",
        updated_at="2026-01-01T00:00:00+00:00",
        turn_id="t0",
    )


def _prompt(loop: AgentLoop, index: int = 0) -> str:
    return "\n".join(message.content for message in loop.gateway.requests[index].messages)


def test_有摘要和产物时控制提示出现会话记忆(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = "sess-a"
    _seed(tmp_path, session)
    searches = {"n": 0}
    original = ArtifactStore.search

    def counted(self, *args, **kwargs):
        searches["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ArtifactStore, "search", counted)
    (tmp_path / "a.txt").write_text("标记内容\n", encoding="utf-8")
    events: list = []
    loop = _loop(
        tmp_path,
        [
            _turn("读文件", [{"name": "read_file", "arguments": {"path": "a.txt"}}]),
            _turn("完成", final="好"),
        ],
        pending=_pending(tmp_path, session),
        events=events,
    )
    before = (tmp_path / ".agent" / "sessions" / session / "summary.json").read_bytes()
    result = loop.run("估算说明")
    assert result.finished is True
    first = _prompt(loop, 0)
    second = _prompt(loop, 1)
    assert "【会话记忆】" in first
    assert "写过估算大纲" in first
    assert "title: 估算说明" in first
    assert BODY not in first
    assert BODY not in second
    assert searches["n"] == 1
    memory = next(message.content for message in loop.gateway.requests[0].messages if "【会话记忆】" in message.content)
    assert memory == loop._session_context_text
    assert "【会话记忆】" in second
    assert next(message.content for message in loop.gateway.requests[1].messages if "【会话记忆】" in message.content) == memory
    goal_at = next(i for i, message in enumerate(loop.gateway.requests[0].messages) if message.content.startswith("目标:"))
    memory_at = next(i for i, message in enumerate(loop.gateway.requests[0].messages) if "【会话记忆】" in message.content)
    assert goal_at < memory_at
    assert loop.gateway.requests[0].messages[0].content.startswith("目标:") is False
    dumped = json.dumps(events, ensure_ascii=False)
    assert "【会话记忆】" not in dumped
    assert BODY not in dumped
    assert (tmp_path / ".agent" / "sessions" / session / "summary.json").read_bytes() == before


def test_空摘要且没有产物时不注入(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [_turn("完成", final="好")], pending=_pending(tmp_path, "sess-empty"))
    result = loop.run("随便问问")
    assert result.finished is True
    assert "【会话记忆】" not in _prompt(loop)
    assert loop._session_context_text == ""


def test_没有会话时短答与旧协议不注入(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [_turn("完成", final="旧协议结论")], pending=None)
    result = loop.run("一句话回答")
    assert result.finished is True
    assert result.final == "旧协议结论"
    assert "【会话记忆】" not in _prompt(loop)


def test_构建失败时退回不注入(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self, *args, **kwargs):
        raise RuntimeError("search down")

    monkeypatch.setattr(ArtifactStore, "search", broken)
    loop = _loop(tmp_path, [_turn("完成", final="好")], pending=_pending(tmp_path, "sess-a"))
    result = loop.run("估算说明")
    assert result.finished is True
    assert "【会话记忆】" not in _prompt(loop)


def test_不读别的会话产物(tmp_path: Path) -> None:
    _seed(tmp_path, "sess-a")
    ArtifactStore(tmp_path).put(
        ArtifactRecord(
            id="doc-other",
            session="sess-b",
            task_id="task",
            turn_id="t0",
            kind="document",
            title=OTHER,
            summary="不该出现。",
            created_at="2026-01-02T00:00:00+00:00",
        ),
        "别的正文",
    )
    loop = _loop(tmp_path, [_turn("完成", final="好")], pending=_pending(tmp_path, "sess-a"))
    loop.run("估算说明")
    text = _prompt(loop)
    assert "估算说明" in text
    assert OTHER not in text


def test_会话记忆配额过小会裁掉装不下的节(tmp_path: Path) -> None:
    session = "sess-trim"
    long_done = "已完成长文" + ("甲" * 180)
    SessionSummaryStore(tmp_path, session).record_done(
        long_done,
        updated_at="2026-01-01T00:00:00+00:00",
    )
    SessionSummaryStore(tmp_path, session).record_open_item(
        "还差一节",
        updated_at="2026-01-01T00:00:01+00:00",
    )
    window = 4096
    ratio = 0.02
    assert Budget(window=window, overrides={"session_memory_ratio": ratio}).quota("session_memory") == int(
        Budget(window=window).effective() * ratio
    )
    loop = _loop(
        tmp_path,
        [_turn("完成", final="好")],
        pending=_pending(tmp_path, session),
        overrides={"session_memory_ratio": ratio},
    )
    loop.run("整理估算说明")
    text = _prompt(loop)
    assert "【会话记忆】" in text
    assert "还差一节" in text
    assert "略过" in text
    assert long_done not in text
    assert Budget(window=1000).quota("system") == int(850 * 0.20)
    assert Budget(window=1000).quota("task_state") == int(Budget(window=1000).effective() * 0.05)
