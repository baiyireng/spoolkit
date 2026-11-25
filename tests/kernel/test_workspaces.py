"""工作区登记：列、按名字/别名/序号/路径解析、忘掉。

登记表本身的四条规则（向上找、显式就是显式、MRU、坏了不挡活）钉在
`tests/test_workspace_root.py` 里，这里只管**新增的那一层命名**。
"""
from pathlib import Path

import pytest

from spoolkit.kernel import workspaces as kws


def _ws(base: Path, name: str) -> Path:
    root = base / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def test_列出登记表带上显示名(tmp_path: Path) -> None:
    first = _ws(tmp_path, "alpha")
    kws.register(first)
    listed = kws.list_workspaces()
    assert [item.root for item in listed] == [first]
    assert listed[0].name == "alpha"


def test_重名时显示名带上一级(tmp_path: Path) -> None:
    left = _ws(tmp_path / "left", "app")
    right = _ws(tmp_path / "right", "app")
    kws.register(left)
    kws.register(right)
    names = {item.root: item.name for item in kws.list_workspaces()}
    assert names[left] == "left/app"
    assert names[right] == "right/app"


def test_按目录名解析(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    assert kws.resolve_workspace("alpha") == root


def test_按别名解析(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    kws.set_alias(root, "我的项目")
    assert kws.resolve_workspace("我的项目") == root


def test_按序号解析且从1起(tmp_path: Path) -> None:
    first = _ws(tmp_path, "alpha")
    second = _ws(tmp_path, "beta")
    kws.register(first)
    kws.register(second)
    assert kws.resolve_workspace("1") == second  # 最近登记的在前
    assert kws.resolve_workspace("2") == first


def test_按路径解析会往上找(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    deep = root / "src" / "pkg"
    deep.mkdir(parents=True)
    assert kws.resolve_workspace(deep) == root


def test_路径不是工作区时报清楚(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(kws.WorkspaceError) as raised:
        kws.resolve_workspace(plain)
    assert kws.MARK in str(raised.value)


def test_没找到时列出候选(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    with pytest.raises(kws.WorkspaceError) as raised:
        kws.resolve_workspace("查无此区")
    assert str(root) in str(raised.value)


def test_同名多个时报清楚并给出出路(tmp_path: Path) -> None:
    left = _ws(tmp_path / "left", "app")
    right = _ws(tmp_path / "right", "app")
    kws.register(left)
    kws.register(right)
    with pytest.raises(kws.WorkspaceError) as raised:
        kws.resolve_workspace("app")
    message = str(raised.value)
    assert str(left) in message and str(right) in message
    assert "alias" in message


def test_忘掉会摘掉登记与别名但不删目录(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    kws.set_alias(root, "我的项目")
    assert kws.forget("我的项目") == root
    assert kws.list_workspaces() == []
    assert (root / kws.MARK).is_dir()


def test_登记表格式不变还是纯路径数组(tmp_path: Path) -> None:
    """兼容是验收条件：别的工具可能按老格式读它。"""
    import json

    root = _ws(tmp_path, "alpha")
    kws.register(root)
    payload = json.loads(kws.registry_path().read_text(encoding="utf-8"))
    assert payload == {"workspaces": [str(root)]}


# --- 名字解析不许被"进程 cwd 里的同名目录"劫持 ------------------------------


def test_名字优先于_cwd_里的同名目录(tmp_path: Path, monkeypatch) -> None:
    """点名一个已登记的名字时，cwd 里恰好有同名目录**不能**把它劫持走。

    实测缺陷（MCP 那条路）：点名 `snake-vue`，服务进程 cwd 在 `agents_dev` 下、
    那里恰好有个 `snake-vue\\` 子目录（它自己不是工作区），于是"向上找 `.agent/`"
    把它解析成了 `agents_dev`——登记表里真正叫 snake-vue 的那个工作区永远轮不到，
    任务在另一个工作区里跑完了。点名 b 却在 a 里干活是最难查的一类问题。
    """
    target = _ws(tmp_path / "elsewhere", "snake-vue")
    parent = _ws(tmp_path / "parent", "parent")
    (parent / "snake-vue").mkdir()  # 同名、但没有 .agent/
    kws.register(target)
    kws.register(parent)
    monkeypatch.chdir(parent)  # 站在"服务进程的启动目录"里

    assert kws.resolve_workspace("snake-vue") == target
    assert kws.resolve_workspace("snake-vue", use_cwd=False) == target


def test_不看cwd时纯名字不猜(tmp_path: Path, monkeypatch) -> None:
    """`use_cwd=False`（MCP / 桥那类入口）：相对名字不再当路径猜，只认登记表。"""
    parent = _ws(tmp_path, "parent")
    (parent / "sub").mkdir()
    monkeypatch.chdir(parent)

    assert kws.resolve_workspace("sub") == parent  # 命令行那条路的老便利保留
    with pytest.raises(kws.WorkspaceError) as raised:
        kws.resolve_workspace("sub", use_cwd=False)
    assert "没有叫 sub 的工作区" in str(raised.value)


def test_不看cwd时绝对路径照旧(tmp_path: Path, monkeypatch) -> None:
    """路径是调用方**显式写的**，与看不看 cwd 无关。"""
    root = _ws(tmp_path, "alpha")
    monkeypatch.chdir(tmp_path)
    assert kws.resolve_workspace(root, use_cwd=False) == root


def test_序号不被_cwd_里的同名目录劫持(tmp_path: Path, monkeypatch) -> None:
    first = _ws(tmp_path / "one", "alpha")
    kws.register(first)
    (tmp_path / "1" / kws.MARK).mkdir(parents=True)  # cwd 里有个叫 "1" 的工作区
    monkeypatch.chdir(tmp_path)

    assert kws.resolve_workspace("1") == first  # 序号看登记表，不看 cwd
