"""放行：谁、能碰哪几个工作区。换工作区不该再重新配对。"""
from pathlib import Path

import pytest

from spoolkit.bridge.pairing import Pairings
from spoolkit.kernel import trust


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".agent").mkdir(parents=True)
    return root.resolve()


def test_没放行过就是没放行() -> None:
    assert trust.is_allowed("qqbot", "u1") is False
    assert trust.allowed_roots("qqbot", "u1") == ()


def test_放行要给工作区且默认只给一个(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    trust.grant("qqbot", "u1", [root])
    assert trust.is_allowed("qqbot", "u1") is True
    assert trust.may_use("qqbot", "u1", root) is True


def test_放行不能让调用方漏给工作区(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        trust.grant("qqbot", "u1", [])


def test_两个工作区可以分别放行(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    trust.grant("qqbot", "u1", [a])
    trust.grant("qqbot", "u1", [b])
    assert trust.may_use("qqbot", "u1", a) is True
    assert trust.may_use("qqbot", "u1", b) is True


def test_没被放行到的工作区够不到(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    trust.grant("qqbot", "u1", [a])
    assert trust.may_use("qqbot", "u1", b) is False


def test_通道之间不串(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    trust.grant("qqbot", "u1", [a])
    assert trust.is_allowed("telegram", "u1") is False


def test_撤销(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    trust.grant("qqbot", "u1", [a])
    assert trust.revoke("qqbot", "u1") is True
    assert trust.is_allowed("qqbot", "u1") is False
    assert trust.revoke("qqbot", "u1") is False


def test_同一个人的码复用不刷屏(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    first = trust.ensure_code("qqbot", "u1", a)
    assert trust.ensure_code("qqbot", "u1", a) == first


def test_按码批准带上工作区(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    code = trust.ensure_code("qqbot", "u1", a)
    got = trust.approve_code(code)
    assert got is not None and got.root == a and got.user == "u1"
    assert trust.may_use("qqbot", "u1", a) is True
    # 批准之后那个码就没了
    assert trust.find_code(code) is None


def test_丢掉没用的码(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    code = trust.ensure_code("qqbot", "u1", a)
    assert trust.forget_code(code) is True
    assert trust.is_allowed("qqbot", "u1") is False


def test_认领旧配对记录且不删原文件(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    legacy = Pairings(root / ".agent" / "bridge-pairings.json")
    legacy.approve("老用户")
    assert trust.adopt_legacy(root, "qqbot") == 1
    assert trust.may_use("qqbot", "老用户", root) is True
    assert (root / ".agent" / "bridge-pairings.json").is_file()
    # 幂等：再认领一次不会变成两条
    assert trust.adopt_legacy(root, "qqbot") == 1
    assert len(trust.list_grants()) == 1


def test_认领之后旧的待批准码也能用(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    legacy = Pairings(root / ".agent" / "bridge-pairings.json")
    code = legacy.ensure_code("还在等的人")
    trust.adopt_legacy(root, "qqbot")
    got = trust.find_code(code)
    assert got is not None and got.root == root and got.channel == "qqbot"
