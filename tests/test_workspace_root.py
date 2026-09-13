"""工作区解析：任意目录都能干活的依据。

这条链原先没有：每条子命令各自把 `--root` 默认成 `"."`。于是"在工作区的子目录
里敲命令"和"在别处敲 `spool approve`"都会找错工作区，而报出来的话（"没有这个
配对码"）指不到病根。规则收在 `workspace.py`，这些用例把四条规则钉死。
"""

from pathlib import Path

import pytest

from spoolkit import workspace


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path_factory, monkeypatch):
    """登记表落在真机上会串味：测试必须看自己的那一份。"""
    monkeypatch.setenv(workspace.STATE_ENV, str(tmp_path_factory.mktemp("state")))


def _workspace(base: Path, name: str) -> Path:
    root = base / name
    (root / workspace.MARK).mkdir(parents=True)
    return root.resolve()


def test_从子目录往上找到工作区根(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path, "ws")
    deep = root / "src" / "pkg"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert workspace.resolve_root(None) == root


def test_显式指定就是指定(tmp_path, monkeypatch) -> None:
    """给了 --root 就不许再往上找：他要的就是那个目录。"""
    root = _workspace(tmp_path, "ws")
    (root / "sub").mkdir()
    monkeypatch.chdir(root / "sub")
    another = tmp_path / "another"
    another.mkdir()
    assert workspace.resolve_root(another) == another.resolve()


def test_登记表让别的目录也能定位工作区(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path, "ws")
    workspace.register(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert workspace.resolve_root(None) == root


def test_登记表里有多个时非交互要报清楚(tmp_path, monkeypatch) -> None:
    first = _workspace(tmp_path, "a")
    second = _workspace(tmp_path, "b")
    workspace.register(first)
    workspace.register(second)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(workspace.WorkspaceError) as raised:
        workspace.resolve_root(None, interactive=lambda: False)
    message = str(raised.value)
    assert str(second) in message and str(first) in message
    assert "--root" in message


def test_登记表里有多个时交互让人选(tmp_path, monkeypatch) -> None:
    first = _workspace(tmp_path, "a")
    second = _workspace(tmp_path, "b")
    workspace.register(first)
    workspace.register(second)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    picked = workspace.resolve_root(
        None, choose=lambda items: first, interactive=lambda: True
    )
    assert picked == first


def test_登记过但已经删掉的工作区会被忽略(tmp_path, monkeypatch) -> None:
    gone = _workspace(tmp_path, "gone")
    workspace.register(gone)
    (gone / workspace.MARK).rmdir()
    gone.rmdir()
    assert workspace.known() == []


def test_登记是幂等的且最近用过的排在最前(tmp_path) -> None:
    first = _workspace(tmp_path, "a")
    second = _workspace(tmp_path, "b")
    workspace.register(first)
    workspace.register(second)
    workspace.register(first)
    assert workspace.known() == [first, second]


def test_登记表坏了也不挡干活(tmp_path, monkeypatch) -> None:
    workspace.registry_path().parent.mkdir(parents=True, exist_ok=True)
    workspace.registry_path().write_text("{ 这不是 json", encoding="utf-8")
    assert workspace.known() == []


def test_哪个都没有时退回当前目录(tmp_path, monkeypatch) -> None:
    """一个工作区都没登记、当前目录也不是：保持老行为，别在这里拦人。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert workspace.resolve_root(None) == plain.resolve()
