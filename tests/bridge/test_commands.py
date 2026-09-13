"""/allow-read 与桥的斜杠命令：聊天里能授权读目录。

用户的原话："我在这里授权你访问那个目录也不行吗？"——当时的答案是**不行**
（只能重启桥加 `--allow-read`）。这组用例把"现在行"钉住，并且钉住两条边界：
陌生人不该拿到命令面、命令只改桥自己的状态。
"""

import argparse
from pathlib import Path

from spoolkit import readroots
from spoolkit.bridge.core import Bridge
from spoolkit.bridge.fake import FakeChannel
from spoolkit.bridge.pairing import Pairings
from spoolkit.cli.commands.bridge import run_extra_args


def _bridge(tmp_path: Path, *, access: str = "open", allowed=()) -> tuple[Bridge, FakeChannel, list[str]]:
    channel = FakeChannel()
    notes: list[str] = []
    bridge = Bridge(
        channel,
        lambda text: "agent 跑了",
        allowed_users=set(allowed),
        access=access,
        on_note=notes.append,
        pairings=Pairings(tmp_path / ".agent" / "bridge-pairings.json"),
        root=tmp_path,
    )
    return bridge, channel, notes


def test_allow_read_记下来并回话(tmp_path: Path, capsys) -> None:
    target = tmp_path / "novels"
    target.mkdir()
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", f"/allow-read {target}")

    bridge.run_once()

    assert readroots.load(tmp_path) == [str(target.resolve())]
    assert "已允许读取" in channel.last
    assert str(target.resolve()) in channel.last


def test_记下的目录会出现在下一轮参数里(tmp_path: Path) -> None:
    """授权必须**持续生效**，否则用户以为放开了、其实只有那一刻。"""
    target = tmp_path / "novels"
    target.mkdir()
    readroots.add(tmp_path, str(target))
    args = argparse.Namespace(
        provider="", model="", base_url="", script="", proxy="",
        policy="ask", scope="src",
    )

    extra = run_extra_args(args, tmp_path)

    assert "--allow-read" in extra
    assert str(target.resolve()) in extra


def test_撤销之后不再带(tmp_path: Path) -> None:
    target = tmp_path / "novels"
    target.mkdir()
    readroots.add(tmp_path, str(target))
    readroots.remove(tmp_path, str(target))
    args = argparse.Namespace(
        provider="", model="", base_url="", script="", proxy="",
        policy="", scope="",
    )
    assert "--allow-read" not in run_extra_args(args, tmp_path)


def test_目录不存在时不记_并说清楚(tmp_path: Path) -> None:
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", "/allow-read D:\\这个目录不存在")

    bridge.run_once()

    assert readroots.load(tmp_path) == []
    assert "没有这个目录" in channel.last


def test_陌生人不该拿到命令面(tmp_path: Path) -> None:
    """公网通道 + 免鉴权命令 = 谁都能改权限。命令在准入之后才处理。"""
    target = tmp_path / "novels"
    target.mkdir()
    bridge, channel, _ = _bridge(tmp_path, access="pairing")
    channel.push("陌生人", f"/allow-read {target}")

    bridge.run_once()

    assert readroots.load(tmp_path) == []
    assert "配对码" in channel.last  # 拿到的是配对提示，不是执行结果


def test_help_不会去跑_agent(tmp_path: Path) -> None:
    called: list[str] = []
    channel = FakeChannel()
    bridge = Bridge(
        channel, lambda text: called.append(text) or "跑完了",
        access="open", root=tmp_path,
    )
    channel.push("me", "/help")

    bridge.run_once()

    assert called == []
    assert "/allow-read" in channel.last


def test_不认识的命令要说清楚(tmp_path: Path) -> None:
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", "/whatever")
    bridge.run_once()
    assert "不认识的命令" in channel.last


def test_status_能看现状(tmp_path: Path) -> None:
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", "/status")
    bridge.run_once()
    assert "工作区" in channel.last
    assert "额外可读目录" in channel.last


def test_普通消息不会被当成命令(tmp_path: Path) -> None:
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", "帮我看看 src 里的东西")
    bridge.run_once()
    assert channel.last == "agent 跑了"
