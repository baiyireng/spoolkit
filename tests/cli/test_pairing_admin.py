"""配对的管理面：看得见、能批准、能解绑。

起因是用户的两个问题：

1. 「本机正开着会话，突然有人来配对，AI 会通知我并出示识别码吗」——
   原先**不会**：码只出现在桥自己的终端上，你在另一个窗口跑 chat 什么都看不到。
2. 「绑定识别码就是在绑定工作区，那要换绑怎么办」——原先没有出口：名单只能增
   不能减，也没有地方看现在放了谁。
"""

import argparse
from pathlib import Path

from spoolkit.bridge.pairing import Pairings
from spoolkit.cli.commands.bridge import (
    approve_command,
    approve_entry,
    pending_notice,
    revoke_command,
)


def _pairings(root: Path) -> Pairings:
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    return Pairings(root / ".agent" / "bridge-pairings.json")


def test_待批准时给出提示(tmp_path, capsys) -> None:
    root = tmp_path / "ws"
    code = _pairings(root).ensure_code("openid-a")

    notice = pending_notice(root)

    assert code in notice and "openid-a" in notice
    assert "/approve" in notice  # 会话里能就地批准


def test_没有待批准时不出声(tmp_path) -> None:
    root = tmp_path / "ws"
    _pairings(root)
    assert pending_notice(root) == ""


def test_批准之后就撤下提示(tmp_path) -> None:
    root = tmp_path / "ws"
    code = _pairings(root).ensure_code("openid-a")
    assert approve_command(root, code) == 0
    assert pending_notice(root) == ""


def test_解绑之后那个人要重新配对(tmp_path, capsys) -> None:
    root = tmp_path / "ws"
    pairings = _pairings(root)
    code = pairings.ensure_code("openid-a")
    approve_command(root, code)
    capsys.readouterr()

    assert revoke_command(root, "openid-a") == 0
    assert "已撤销" in capsys.readouterr().out
    assert Pairings(root / ".agent" / "bridge-pairings.json").approved == ()
    assert pending_notice(root) == ""


def test_解绑不在名单里的人要报清楚(tmp_path, capsys) -> None:
    root = tmp_path / "ws"
    _pairings(root)
    assert revoke_command(root, "openid-x") == 2
    assert "本来就不在" in capsys.readouterr().err


def test_LIST_看得见放行了谁和谁在等(tmp_path, capsys) -> None:
    root = tmp_path / "ws"
    pairings = _pairings(root)
    code = pairings.ensure_code("openid-a")
    approve_command(root, code)
    waiting = pairings.ensure_code("openid-b")
    capsys.readouterr()

    args = argparse.Namespace(
        root=str(root), list=True, revoke="", forget="", code=""
    )
    assert approve_entry(args) == 0

    out = capsys.readouterr().out
    assert "openid-a" in out and "openid-b" in out and waiting in out


def test_丢掉一个码不会放行谁(tmp_path, capsys) -> None:
    root = tmp_path / "ws"
    code = _pairings(root).ensure_code("openid-a")
    args = argparse.Namespace(root=str(root), list=False, revoke="", forget=code, code="")

    assert approve_entry(args) == 0
    assert Pairings(root / ".agent" / "bridge-pairings.json").approved == ()
    assert "谁都没被放行" in capsys.readouterr().out


def test_既没给码也没给选项时要说清楚(tmp_path, capsys) -> None:
    root = tmp_path / "ws"
    _pairings(root)
    args = argparse.Namespace(root=str(root), list=False, revoke="", forget="", code="")
    assert approve_entry(args) == 2
    assert "--list" in capsys.readouterr().err
