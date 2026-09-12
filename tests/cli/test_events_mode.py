import io
import json
from pathlib import Path
from types import SimpleNamespace

from agents_dev.cli.commands.run import _report_numbers
from agents_dev.cli.events import EventWriter, settle_with_events
from agents_dev.policy import ASK, AUTO
from agents_dev.tools.sources import SourceLog
from agents_dev.tools.edit import PendingChanges, write_file_spec
from agents_dev.web.protocol import AWAIT, CONFIRM, DIFF, NOTE


def _stage(tmp_path: Path, path: str = "src/a.py") -> PendingChanges:
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": path, "content": "x = 1\n"}
    )
    return pending


def _lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().strip().splitlines()]


def test_询问策略下发diff与await(tmp_path: Path) -> None:
    buffer = io.StringIO()
    pending = _stage(tmp_path)
    settle_with_events(
        pending, ASK, (), None, EventWriter(buffer), io.StringIO("y\n")
    )
    kinds = [line["type"] for line in _lines(buffer)]
    assert DIFF in kinds
    assert AWAIT in kinds
    assert CONFIRM in kinds


def test_回答y会落盘(tmp_path: Path) -> None:
    buffer = io.StringIO()
    pending = _stage(tmp_path)
    settle_with_events(
        pending, ASK, (), None, EventWriter(buffer), io.StringIO("y\n")
    )
    assert (tmp_path / "src" / "a.py").exists()


def test_回答n不落盘(tmp_path: Path) -> None:
    buffer = io.StringIO()
    pending = _stage(tmp_path)
    settle_with_events(
        pending, ASK, (), None, EventWriter(buffer), io.StringIO("n\n")
    )
    assert not (tmp_path / "src" / "a.py").exists()


def test_范围之外的改动也要问(tmp_path: Path) -> None:
    buffer = io.StringIO()
    pending = _stage(tmp_path, "docs/b.md")
    settle_with_events(
        pending, AUTO, ("src",), None, EventWriter(buffer), io.StringIO("n\n")
    )
    assert AWAIT in [line["type"] for line in _lines(buffer)]


def test_范围内自动落盘时不发await(tmp_path: Path) -> None:
    """auto 且范围内 → 自动应用。界面只看到 diff 与 confirm，不该出按钮。"""
    buffer = io.StringIO()
    pending = _stage(tmp_path)
    settle_with_events(
        pending, AUTO, ("src",), None, EventWriter(buffer), io.StringIO("")
    )
    lines = _lines(buffer)
    assert AWAIT not in [line["type"] for line in lines]
    assert any(line["type"] == CONFIRM and line["applied"] for line in lines)
    assert (tmp_path / "src" / "a.py").exists()


def test_没有改动时不输出任何事件(tmp_path: Path) -> None:
    buffer = io.StringIO()
    settle_with_events(
        PendingChanges(tmp_path), ASK, (), None, EventWriter(buffer), io.StringIO("y\n")
    )
    assert buffer.getvalue() == ""


def test_数字核对以note事件说出来() -> None:
    """工具那行只报「成功/失败」，理由得单独说——否则页面上是个没有理由的红字。"""
    log = SourceLog()
    log.add("src-tauri 5.1 GB")
    loop = SimpleNamespace(sources=log)
    result = SimpleNamespace(final="target 8 GB")

    buffer = io.StringIO()
    _report_numbers(loop, result, EventWriter(buffer))
    lines = _lines(buffer)
    assert [line["type"] for line in lines] == [NOTE]
    assert "8 GB" in lines[0]["text"]
    assert lines[0]["ok"] is False


def test_数字都对得上时不发note(tmp_path: Path) -> None:
    """没话说就别说话——每次交付都冒一行出来，很快就被无视了。"""
    log = SourceLog()
    log.add("src-tauri 5.1 GB")
    loop = SimpleNamespace(sources=log)
    result = SimpleNamespace(final="src-tauri 5.1 GB")

    buffer = io.StringIO()
    _report_numbers(loop, result, EventWriter(buffer))
    assert buffer.getvalue() == ""

