"""本机 `spool extra`：把关联目录写进配置，聊天里加不了。"""

import argparse
from pathlib import Path

from spoolkit import readroots
from spoolkit.cli.app import main
from spoolkit.cli.commands.extra import extra_command
from spoolkit.cli.commands.run import _resolve_read_roots


def _args(tmp_path: Path, action: str = "list", path: str = "") -> argparse.Namespace:
    return argparse.Namespace(root=str(tmp_path), action=action, path=path)


def test_add_把目录写进配置(tmp_path: Path, capsys) -> None:
    target = tmp_path / "docs"
    target.mkdir()
    assert extra_command(_args(tmp_path, "add", str(target))) == 0
    stored = [Path(item) for item in readroots.load(tmp_path)]
    assert target.resolve() in stored
    out = capsys.readouterr().out
    assert "已关联" in out
    assert str(target.resolve()) in out


def test_remove_能从配置里删掉(tmp_path: Path) -> None:
    target = tmp_path / "docs"
    target.mkdir()
    extra_command(_args(tmp_path, "add", str(target)))
    assert extra_command(_args(tmp_path, "remove", str(target))) == 0
    assert readroots.load(tmp_path) == []


def test_list_能看见记下的目录(tmp_path: Path, capsys) -> None:
    target = tmp_path / "docs"
    target.mkdir()
    extra_command(_args(tmp_path, "add", str(target)))
    assert extra_command(_args(tmp_path, "list")) == 0
    assert str(target.resolve()) in capsys.readouterr().out


def test_没有这个目录就不写入(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert extra_command(_args(tmp_path, "add", str(missing))) == 2
    assert readroots.load(tmp_path) == []


def test_入口挂上了extra(tmp_path: Path) -> None:
    target = tmp_path / "docs"
    target.mkdir()
    (tmp_path / ".agent").mkdir()
    assert main(["extra", "add", str(target), "--root", str(tmp_path)]) == 0
    assert target.resolve() in [Path(item) for item in readroots.load(tmp_path)]


def test_配置里的目录会并进本次运行(tmp_path: Path) -> None:
    """`spool extra add` 之后，不必每次再写 `--allow-read`。"""
    project = tmp_path / "ws"
    outside = tmp_path / "docs"
    project.mkdir()
    outside.mkdir()
    extra_command(_args(project, "add", str(outside)))
    args = argparse.Namespace(allow_read=[])
    roots, bad = _resolve_read_roots(args, project)
    assert bad == []
    assert outside.resolve() in roots
