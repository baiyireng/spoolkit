from pathlib import Path

from agents_dev.cli.settle import (
    AUTO_APPLIED,
    CONFIRMED,
    DECLINED,
    DENIED,
    NONE,
    settle,
)
from agents_dev.policy import ASK, AUTO, DENY
from agents_dev.tools.edit import PendingChanges, load_baseline, write_file_spec


def _stage(tmp_path: Path, path: str = "src/a.py") -> PendingChanges:
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": path, "content": "x = 1\n"}
    )
    return pending


def test_没有改动时不产生动作(tmp_path: Path) -> None:
    assert settle(PendingChanges(tmp_path), ASK, (), ask=lambda _: "y") == (NONE, [])


def test_只读策略丢弃改动(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    action, written = settle(pending, DENY, ("**",), ask=lambda _: "y")
    assert action == DENIED
    assert written == []
    assert not (tmp_path / "src" / "a.py").exists()


def test_自动策略在范围内直接落盘(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    action, written = settle(
        pending, AUTO, ("src",), baseline_path=tmp_path / ".agent" / "b.json",
        ask=lambda _: "n",
    )
    assert action == AUTO_APPLIED
    assert written == ["src/a.py"]
    assert (tmp_path / "src" / "a.py").exists()


def test_自动策略仍然记录基线(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    baseline = tmp_path / ".agent" / "b.json"
    settle(pending, AUTO, ("src",), baseline_path=baseline, ask=lambda _: "n")
    assert load_baseline(baseline) is not None


def test_自动策略越界时退回确认(tmp_path: Path) -> None:
    pending = _stage(tmp_path, "docs/b.md")
    action, written = settle(
        pending, AUTO, ("src",), ask=lambda _: "y"
    )
    # 越界不能自动落盘，但确认之后仍然可以应用
    assert action == CONFIRMED
    assert written == ["docs/b.md"]


def test_越界且用户拒绝时不落盘(tmp_path: Path) -> None:
    pending = _stage(tmp_path, "docs/b.md")
    action, written = settle(pending, AUTO, ("src",), ask=lambda _: "n")
    assert action == DECLINED
    assert written == []
    assert not (tmp_path / "docs" / "b.md").exists()


def test_询问策略走确认流程(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    assert settle(pending, ASK, (), ask=lambda _: "y")[0] == CONFIRMED


def test_询问策略被拒时丢弃(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    assert settle(pending, ASK, (), ask=lambda _: "n")[0] == DECLINED


def test_无人值守时越界直接拒绝而不是询问(tmp_path: Path) -> None:
    pending = _stage(tmp_path, "docs/b.md")
    action, written = settle(
        pending,
        AUTO,
        ("src",),
        ask=lambda _: "y",
        non_interactive=True,
    )
    assert action == DENIED
    assert written == []
    assert not (tmp_path / "docs" / "b.md").exists()


def test_无人值守但范围内仍然自动落盘(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    action, _ = settle(
        pending, AUTO, ("src",), ask=lambda _: "n", non_interactive=True
    )
    assert action == AUTO_APPLIED
