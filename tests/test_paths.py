from pathlib import Path

import pytest

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_within


def test_相对路径解析到项目根之下(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    got = resolve_within(tmp_path, "pkg/a.py")
    assert got == (tmp_path / "pkg" / "a.py").resolve()


def test_绝对路径若在项目内则接受(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    assert resolve_within(tmp_path, str(target)) == target.resolve()


def test_越界路径被拒绝(tmp_path: Path) -> None:
    with pytest.raises(PathOutsideProjectError):
        resolve_within(tmp_path, "../outside.py")


def test_用绝对路径越界同样被拒绝(tmp_path: Path) -> None:
    with pytest.raises(PathOutsideProjectError):
        resolve_within(tmp_path, str(tmp_path.parent / "outside.py"))


def test_空路径被拒绝(tmp_path: Path) -> None:
    with pytest.raises(PathOutsideProjectError):
        resolve_within(tmp_path, "")

