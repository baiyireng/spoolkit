import io
import json
from pathlib import Path
from types import SimpleNamespace

from spoolkit.cli.commands.run import _report_numbers
from spoolkit.cli.events import EventWriter, settle_with_events
from spoolkit.policy import ASK, AUTO
from spoolkit.tools.sources import SourceLog
from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.web.protocol import AWAIT, CONFIRM, DIFF, NOTE


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


def test_关联目录即使auto也不自动落盘(tmp_path: Path) -> None:
    buffer = io.StringIO()
    workspace = tmp_path / "ws"
    outside = tmp_path / "docs"
    workspace.mkdir()
    outside.mkdir()
    pending = PendingChanges(workspace, extra_roots=(outside,))
    write_file_spec(workspace, pending).handler(
        {"path": str(outside / "a.txt"), "content": "x\n"}
    )
    settle_with_events(
        pending, AUTO, ("**",), None, EventWriter(buffer), io.StringIO("n\n")
    )
    kinds = [line["type"] for line in _lines(buffer)]
    assert AWAIT in kinds
    assert not (outside / "a.txt").exists()


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


# ---- stdout 纯净：diff 与人读的留档必须走 stderr -----------------------------
#
# 实测（首轮探针）：auto 落盘时 `apply_with_audit` 把 diff 直接 `print` 到了
# stdout，于是 `--events` 的 stdout 里混进 12–91 行散文（merge-html 那条 36 行）。
# 跑分器数出来的就是 `plain_lines`，它必须为 0——否则「stdout 上只有 JSON」
# 这条约定只是文档里的一句话。


def _stdout_json_lines(captured: str) -> list[str]:
    return [line for line in captured.splitlines() if line.strip()]


def test_自动落盘时stdout只有JSON(tmp_path: Path, capsys) -> None:
    buffer = io.StringIO()
    pending = _stage(tmp_path)
    settle_with_events(
        pending, AUTO, ("src",), None, EventWriter(buffer), io.StringIO("")
    )
    out, err = capsys.readouterr()

    assert out == "", f"stdout 上出现了非事件输出：{out[:200]!r}"
    for line in _stdout_json_lines(buffer.getvalue()):
        assert json.loads(line)["type"] in (DIFF, CONFIRM, NOTE)
    # 人读的那一份没有消失，只是换了出口。
    assert "+++ b/src/a.py" in err
    assert "已自动应用 1 处修改" in err
    # 落盘语义一个字没变。
    assert (tmp_path / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_落盘后的diff不在stdout上(tmp_path: Path, capsys) -> None:
    """落到磁盘的内容照样能被 events 的消费者看见（走 diff 事件）。"""
    buffer = io.StringIO()
    pending = _stage(tmp_path)
    settle_with_events(pending, ASK, (), None, EventWriter(buffer), io.StringIO("y\n"))
    out, _ = capsys.readouterr()
    assert out == ""
    diff_lines = [line for line in _lines(buffer) if line["type"] == DIFF]
    assert len(diff_lines) == 1
    assert "x = 1" in diff_lines[0]["text"]

