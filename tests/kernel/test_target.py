"""唯一解析入口：显式 > 向上找 > 指针 > 登记表 > 当前目录。"""
from pathlib import Path

import pytest

from spoolkit.kernel import pointers, workspaces as kws
from spoolkit.kernel.target import resolve_target


def _ws(base: Path, name: str) -> Path:
    root = base / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def test_显式工作区最优先(tmp_path: Path, monkeypatch) -> None:
    here = _ws(tmp_path, "here")
    other = _ws(tmp_path, "other")
    monkeypatch.chdir(here)
    pointers.set("cli", other, "s1")
    target = resolve_target(workspace=other)
    assert target.root == other
    assert target.workspace_source == "explicit"


def test_当前目录向上找压过指针(tmp_path: Path, monkeypatch) -> None:
    here = _ws(tmp_path, "here")
    other = _ws(tmp_path, "other")
    pointers.set("cli", other, "s1")
    monkeypatch.chdir(here)
    target = resolve_target()
    assert target.root == here
    assert target.workspace_source == "cwd"


def test_不在工作区里时用指针(tmp_path: Path, monkeypatch) -> None:
    other = _ws(tmp_path, "other")
    plain = tmp_path / "plain"
    plain.mkdir()
    pointers.set("cli", other, "s1")
    monkeypatch.chdir(plain)
    target = resolve_target()
    assert target.root == other
    assert target.workspace_source == "pointer"


def test_指针指向的工作区没了就接着往下退(tmp_path: Path, monkeypatch) -> None:
    gone = _ws(tmp_path, "gone")
    keeper = _ws(tmp_path, "keeper")
    kws.register(keeper)
    pointers.set("cli", gone, "s1")
    (gone / kws.MARK).rmdir()
    gone.rmdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_target().root == keeper


def test_登记表多个又不交互就报清楚(tmp_path: Path, monkeypatch) -> None:
    first = _ws(tmp_path, "a")
    second = _ws(tmp_path, "b")
    kws.register(first)
    kws.register(second)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    with pytest.raises(kws.WorkspaceError) as raised:
        resolve_target()
    assert "--root" in str(raised.value)


def test_说不清时可以交给调用方挑(tmp_path: Path, monkeypatch) -> None:
    first = _ws(tmp_path, "a")
    second = _ws(tmp_path, "b")
    kws.register(first)
    kws.register(second)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    # known() 最近使用在前（[second, first]），回调返回谁就用谁：items[1] 是 first。
    assert resolve_target(on_ambiguous=lambda items: items[1]).root == first


def test_一个都没有就退回当前目录(tmp_path: Path, monkeypatch) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    target = resolve_target()
    assert target.root == plain.resolve()
    assert target.workspace_source == "cwd-fallback"


def test_会话显式最优先(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path, "here")
    monkeypatch.chdir(root)
    pointers.set("cli", root, "from-pointer")
    assert resolve_target(session="asked").session == "asked"


def test_指针里的会话只在同一个工作区时生效(tmp_path: Path, monkeypatch) -> None:
    here = _ws(tmp_path, "here")
    other = _ws(tmp_path, "other")
    pointers.set("cli", other, "from-pointer")
    monkeypatch.chdir(here)
    target = resolve_target()
    assert target.session == "cli"
    assert target.session_source == "default"


def test_会话默认值由入口给(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path, "here")
    monkeypatch.chdir(root)
    assert resolve_target(session_fallback="bridge").session == "bridge"
