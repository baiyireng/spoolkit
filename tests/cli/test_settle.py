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


def test_无人值守时越界的丢掉_范围内的照落(tmp_path: Path) -> None:
    """一条越界不该把同一批里**对的**改动一起作废。

    实测事故：自主运行第 43 步越界写了两处别的题，而它自己那处改动是对的。
    整批作废之后，审查看到的是「测试没过」，于是判这一步失败，
    整份计划在 43/49 处中止——剩下 6 题连试都没试。
    """
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "43_nested_get/nested.py", "content": "fixed = True\n"}
    )
    write_file_spec(tmp_path, pending).handler(
        {"path": "44_merge_counts/counter.py", "content": "越界\n"}
    )

    action, written = settle(
        pending,
        AUTO,
        ("43_nested_get",),
        non_interactive=True,
    )

    assert action == AUTO_APPLIED
    assert written == ["43_nested_get/nested.py"]
    assert (tmp_path / "43_nested_get" / "nested.py").exists()
    assert not (tmp_path / "44_merge_counts" / "counter.py").exists()


def test_无人值守时全是越界仍然拒绝(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "44_merge_counts/counter.py", "content": "越界\n"}
    )
    action, written = settle(
        pending, AUTO, ("43_nested_get",), non_interactive=True
    )
    assert action == DENIED
    assert written == []
