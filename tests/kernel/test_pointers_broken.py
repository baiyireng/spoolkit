"""坏形状的机器级状态文件不许变成"发了消息没人理"（Task 6 评审的 Critical-1）。

`clients.json` 是人也能手改的机器级文件。写成 `{"clients": "不是字典"}` 之后，
读路径 `AttributeError`、写路径 `ValueError`（`dict("...")`）——而 `AttributeError`
会穿过桥的 `except WorkspaceError`，症状不是"报错"，而是**用户发出去的消息没有
任何回应**。`trust.py` 里同族的 `_mapping()` 就是治这个的，这一份把它推广到指向。
"""

import json
from pathlib import Path

from spoolkit.bridge.channel import Incoming
from spoolkit.bridge.core import Bridge
from spoolkit.kernel import pointers
from spoolkit.kernel import workspaces as kws


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def _break(payload: object) -> None:
    path = pointers.pointers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_指向文件形状坏掉也够不到而不炸(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    for broken in ({"clients": "不是字典"}, {"clients": ["数组"]}, {"clients": 3}, ["顶层是数组"]):
        _break(broken)
        assert pointers.all_pointers() == {}
        assert pointers.get("cli") is None
        # 写路径也要能活：坏文件不该让"再也修不回来"
        pointers.set("cli", root, "s1")
        assert pointers.get("cli") is not None


def test_桥遇到坏状态文件也要回一句话(tmp_path: Path, monkeypatch) -> None:
    """Critical-1 的原症状：`handle` 抛出去 = 用户永远等不到回复。"""
    root = _ws(tmp_path, "a")
    kws.register(root)
    _break({"clients": "不是字典"})
    sent: list[tuple[str, str]] = []

    class _Channel:
        def send(self, text: str, conversation: str) -> None:
            sent.append((conversation, text))

        def close(self) -> None:
            pass

    from spoolkit.kernel import trust

    trust.grant("fake", "u1", [root])
    bridge = Bridge(_Channel(), lambda text: "答")

    reply = bridge.handle(Incoming(user="u1", text="干活", conversation="c1"))

    # 坏文件不该让消息石沉大海：要么正常干活（退回登记表里那个唯一工作区），
    # 要么回一句说明。**唯独不能是"一个字都没有"。**
    assert sent, "一个字都没回出去——那正是要避免的症状"
    assert reply.accepted is True or "没能落到工作区" in sent[-1][1]


def test_机器级的桥不拿启动目录凑数(tmp_path: Path, monkeypatch) -> None:
    """评审的 Important-2：桥的 cwd 只是启动方式的副产品，不该参与决策。

    `use_cwd=False` 之后：没有指针可指、登记表里又说不清时**报错**，
    而不是"悄悄在启动目录里干活"。
    """
    from spoolkit.bridge.core import Bridge

    launch = _ws(tmp_path, "launch")  # 一个**是工作区**的启动目录
    other = _ws(tmp_path, "other")
    kws.register(launch)
    kws.register(other)  # 登记表里两个 → 说不清
    monkeypatch.chdir(launch)

    class _Channel:
        def send(self, text: str, conversation: str) -> None:
            pass

        def close(self) -> None:
            pass

    bridge = Bridge(_Channel(), lambda text: "答")
    import pytest

    from spoolkit.kernel.workspaces import WorkspaceError

    with pytest.raises(WorkspaceError):
        bridge.target_for("u-nobody")  # 既没 --root、也没这条聊天的指针

    # 指针一指，就按指针走（哪怕它跟启动目录不是同一个）
    from spoolkit.kernel import pointers

    pointers.set("chat:fake:u-nobody", other, "cli")
    assert bridge.target_for("u-nobody").root == other
