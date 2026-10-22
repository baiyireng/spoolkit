"""spool ws：列、切、加、忘、起别名。"""
from pathlib import Path

from spoolkit.cli.app import main
from spoolkit.kernel import pointers, workspaces as kws


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def test_列表标出当前(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    pointers.set("cli", root, "cli")
    assert main(["ws"]) == 0
    out = capsys.readouterr().out
    assert str(root) in out
    assert "current" in out


def test_用名字切会写指针(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    assert main(["ws", "use", "alpha"]) == 0
    got = pointers.get("cli")
    assert got is not None and got.workspace == root


def test_加一个目录会建_agent并登记(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    target.mkdir()
    assert main(["ws", "add", str(target)]) == 0
    assert (target / kws.MARK).is_dir()
    assert target.resolve() in kws.known()


def test_起别名之后能按别名解析(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    assert main(["ws", "alias", "alpha", "我的项目"]) == 0
    assert main(["ws", "use", "我的项目"]) == 0


def test_忘掉不会删目录(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    assert main(["ws", "forget", "alpha"]) == 0
    assert kws.known() == []
    assert (root / kws.MARK).is_dir()


def test_没有叫这个名字的工作区时报错并列出候选(tmp_path: Path, capsys) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    assert main(["ws", "use", "查无此区"]) == 2
    assert str(root) in capsys.readouterr().err
