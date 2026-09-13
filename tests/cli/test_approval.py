from pathlib import Path

from spoolkit.cli.approval import review_and_apply
from spoolkit.tools.edit import PendingChanges, write_file_spec


def _stage(tmp_path: Path) -> PendingChanges:
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "a.py", "content": "print(1)\n"}
    )
    return pending


def test_回答y时应用修改(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    applied, written = review_and_apply(pending, ask=lambda _: "y")
    assert applied is True
    assert written == ["a.py"]
    assert (tmp_path / "a.py").exists()


def test_回答n时丢弃修改(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    applied, written = review_and_apply(pending, ask=lambda _: "n")
    assert applied is False
    assert written == []
    assert not (tmp_path / "a.py").exists()


def test_直接回车视为拒绝(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    applied, _ = review_and_apply(pending, ask=lambda _: "")
    assert applied is False
    assert not (tmp_path / "a.py").exists()


def test_大小写不敏感(tmp_path: Path) -> None:
    pending = _stage(tmp_path)
    applied, _ = review_and_apply(pending, ask=lambda _: "Y")
    assert applied is True


def test_没有待确认修改时直接返回(tmp_path: Path) -> None:
    applied, written = review_and_apply(
        PendingChanges(tmp_path), ask=lambda _: "y"
    )
    assert applied is False
    assert written == []

