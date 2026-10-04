import io
import json

from spoolkit.cli.events import EventWriter
from spoolkit.web.protocol import FINAL, parse_line


def test_输出一行JSON() -> None:
    buffer = io.StringIO()
    EventWriter(buffer).emit(FINAL, ok=True, text="好了")
    line = buffer.getvalue().strip()
    assert json.loads(line)["type"] == FINAL


def test_可解析回来() -> None:
    buffer = io.StringIO()
    EventWriter(buffer).emit(FINAL, ok=True, text="好了")
    parsed = parse_line(buffer.getvalue())
    assert parsed is not None
    assert parsed.data["text"] == "好了"


def test_每次输出都冲洗() -> None:
    """不 flush 的话输出会攒在缓冲区里，UI 看起来像卡住。"""

    class _Probe(io.StringIO):
        flushed = 0

        def flush(self) -> None:
            type(self).flushed += 1

    probe = _Probe()
    EventWriter(probe).emit(FINAL, ok=True, text="x")
    assert probe.flushed >= 1


def test_连续输出是多行() -> None:
    buffer = io.StringIO()
    writer = EventWriter(buffer)
    writer.emit(FINAL, ok=True, text="一")
    writer.emit(FINAL, ok=False, text="二")
    assert len(buffer.getvalue().strip().splitlines()) == 2


def test_handle可直接作为回调() -> None:
    buffer = io.StringIO()
    EventWriter(buffer).handle("step", {"n": 3})
    assert json.loads(buffer.getvalue().strip())["n"] == 3


def test_handle能带kind字段() -> None:
    """`note` 事件里的 `kind` 字段不许被吞掉。

    现场（2026-10-04）：`handle` 原先写成 `self.emit(kind, **data)`，而 `data`
    里的 `kind` 与 `emit(self, kind, ...)` 的位置参数重名 →
    `TypeError: got multiple values for argument 'kind'` → 被
    `AgentLoop._emit` 的 `except Exception` 吞掉，于是**所有带 kind 的 note
    静默丢失**：013137-rename-cross-file-r0 的事件流里 8 条 note 全是「不带
    kind」的那种，`guard_refresh` / `supervisor_fact_conflict` / `audit`
    一条都没有。探针侧的 `guard_refreshes` / `supervisor_false_claims`
    因此常年读成 0，而审查层的 `stale_references` / `tests_weakened` 也会
    永远读成 0——那正是 P0 的字段口径。
    """
    buffer = io.StringIO()
    EventWriter(buffer).handle(
        "note", {"text": "旧名仍有残留", "ok": False, "kind": "audit"}
    )
    payload = json.loads(buffer.getvalue().strip())
    assert payload["type"] == "note"
    assert payload["kind"] == "audit"
    assert payload["text"] == "旧名仍有残留"

