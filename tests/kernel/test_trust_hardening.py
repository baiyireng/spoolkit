"""放行文件的加固：坏形状与垃圾条目都不许把通道打死。

这一份是 **Task 4 评审修复轮** 加的（评审报告的 Important-1 / Important-2），
和 `test_trust.py` 分开放是有意的：那个文件是计划 Step 1 的代码块**逐字**落盘，
是"测试没被改"的凭据，修复轮只往后加，不去动它。

为什么要写这些（评审实测出来的）：

- **Important-1**：`channel-trust.json` 是人也能手改的机器级文件，形状坏掉时
  （`grants` 写成数组、`roots` 写成数字）读路径会抛 `AttributeError` / `TypeError`。
  而 `is_allowed` 是每条入站消息都要过的闸门——抛异常等于把整条通道打死，
  与 `trust.py` 自己写的"够不到，不是起不来"正相反。
- **Important-2**：旧配对文件里的条目内容没人校验，一条空用户名就够让
  `adopt_legacy` 抛 `ValueError`——而认领是在**桥启动时**跑的。
"""
from __future__ import annotations

import json
from pathlib import Path

from spoolkit.bridge.pairing import Pairings
from spoolkit.kernel import trust


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".agent").mkdir(parents=True)
    return root.resolve()


def _break_trust(payload: object) -> None:
    """手改机器级文件（模拟人编辑 / 旧版本留下的形状）。"""
    path = trust.trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_grants形状坏掉也只会够不到(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    _break_trust({"grants": ["qqbot"]})
    assert trust.list_grants() == []
    assert trust.is_allowed("qqbot", "u1") is False
    assert trust.allowed_roots("qqbot", "u1") == ()
    assert trust.may_use("qqbot", "u1", root) is False
    assert trust.revoke("qqbot", "u1") is False  # 写路径也不该抛
    # 写路径也要能活：坏文件不该让"修不回来"
    trust.grant("qqbot", "u1", [root])
    assert trust.may_use("qqbot", "u1", root) is True


def test_roots不是一串路径就不算放行(tmp_path: Path) -> None:
    """字符串会被逐字符拆成 D / : / / / t，造出"说得通却够不到"的幻影放行。"""
    root = _ws(tmp_path, "a")
    _break_trust(
        {
            "grants": {
                "qqbot\u0000u1": {"channel": "qqbot", "user": "u1", "roots": str(root)},
            }
        }
    )
    assert trust.list_grants() == []
    assert trust.is_allowed("qqbot", "u1") is False
    _break_trust(
        {
            "grants": {
                "qqbot\u0000u1": {"channel": "qqbot", "user": "u1", "roots": 3},
            }
        }
    )
    assert trust.list_grants() == []
    assert trust.is_allowed("qqbot", "u1") is False


def test_codes形状坏掉也只是没有码(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    _break_trust({"codes": ["ABC123"]})
    assert trust.pending() == []
    assert trust.find_code("ABC123") is None
    assert trust.approve_code("ABC123") is None
    assert trust.forget_code("ABC123") is False  # 写路径也不该抛
    # 写路径同样要能活
    code = trust.ensure_code("qqbot", "u1", root)
    assert trust.find_code(code) is not None


def test_旧文件里有空用户也不打断认领(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    legacy = Pairings(root / ".agent" / "bridge-pairings.json")
    legacy.approved  # 触发读取
    (root / ".agent" / "bridge-pairings.json").write_text(
        json.dumps({"approved": ["", "   ", "真人"], "codes": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert trust.adopt_legacy(root, "qqbot") == 1  # 只认领"真人"，且不抛
    assert trust.may_use("qqbot", "真人", root) is True
    assert trust.is_allowed("qqbot", "") is False


def test_旧文件里空用户的码不入账(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    legacy = Pairings(root / ".agent" / "bridge-pairings.json")
    code = legacy.ensure_code("")  # 历史上真可能这么写进去
    trust.adopt_legacy(root, "qqbot")
    assert trust.find_code(code) is None


def test_机器级里的空用户码批准时只当作没有(tmp_path: Path) -> None:
    """`approve_code` 是人手敲的路径：报"没这个码"远比抛异常好。"""
    root = _ws(tmp_path, "a")
    _break_trust(
        {
            "codes": {
                "ABC123": {"user": "", "channel": "qqbot", "root": str(root)},
            }
        }
    )
    assert trust.find_code("ABC123") is not None  # 条目还在，看得见
    assert trust.approve_code("ABC123") is None  # 但批不了，也不抛
    assert trust.is_allowed("qqbot", "") is False


def test_要合并的那一条本身坏了也不抛(tmp_path: Path) -> None:
    """`grant` 是 `adopt_legacy`（桥启动）与 `approve_code`（人手批准）的必经之路。

    坏形状在这里抛，等于那两条路照样被打死——修复轮第一版只收了读路径，
    这一处是复核抓出来的。
    """
    root = _ws(tmp_path, "a")
    for broken in ("字符串", ["数组"]):
        _break_trust({"grants": {"qqbot\u0000u1": broken}})
        trust.grant("qqbot", "u1", [root])
        assert trust.may_use("qqbot", "u1", root) is True
    _break_trust({"grants": {"qqbot\u0000u1": {"roots": 3}}})
    trust.grant("qqbot", "u1", [root])
    assert trust.may_use("qqbot", "u1", root) is True


def test_要合并的那一条roots是字符串时不会把字符写进放行(tmp_path: Path) -> None:
    """字符串 `roots` 被逐字符拆开会凭空多出 `D:\\` 这种根，等于把放行放宽。"""
    root = _ws(tmp_path, "a")
    _break_trust(
        {
            "grants": {
                "qqbot\u0000u1": {
                    "channel": "qqbot",
                    "user": "u1",
                    "roots": "D:/tmp",
                }
            }
        }
    )
    trust.grant("qqbot", "u1", [root])
    roots = trust.allowed_roots("qqbot", "u1")
    assert roots == (root,)
    assert all(len(str(item)) > 3 for item in roots)  # 没有 `D:\`、`D` 这类单字符根
