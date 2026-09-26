"""远程聊天通道的斜杠命令。

远程通道只说明工作区边界，不在聊天里开放工作区外的读权限——
小模型分不清危险操作。
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


def test_allow_read_在远程通道上拒绝并说明原因(tmp_path: Path) -> None:
    target = tmp_path / "novels"
    target.mkdir()
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", f"/allow-read {target}")

    bridge.run_once()

    assert readroots.load(tmp_path) == []
    assert "工作区" in channel.last
    assert "不开放" in channel.last or "危险" in channel.last


def test_本机关联的目录会传给下一轮(tmp_path: Path) -> None:
    """聊天里加不了目录；主人在服务器上 `spool extra add` 过的要带上。"""
    target = tmp_path / "novels"
    target.mkdir()
    readroots.add(tmp_path, str(target))
    args = argparse.Namespace(
        provider="", model="", base_url="", script="", proxy="",
        policy="ask", scope="src",
    )

    extra = run_extra_args(args, tmp_path)

    assert "--from-channel" in extra
    assert "--web" in extra
    assert "--allow-read" in extra
    assert str(target.resolve()) in extra


def test_撤销之后本来就不带(tmp_path: Path) -> None:
    target = tmp_path / "novels"
    target.mkdir()
    readroots.add(tmp_path, str(target))
    readroots.remove(tmp_path, str(target))
    args = argparse.Namespace(
        provider="", model="", base_url="", script="", proxy="",
        policy="", scope="",
    )
    assert "--allow-read" not in run_extra_args(args, tmp_path)


def test_远程通道默认开联网取用() -> None:
    args = argparse.Namespace(
        provider="", model="", base_url="", script="", proxy="",
        policy="", scope="",
    )
    extra = run_extra_args(args, Path("."))
    assert "--from-channel" in extra
    assert "--web" in extra


def test_no_web关掉通道联网() -> None:
    args = argparse.Namespace(
        provider="", model="", base_url="", script="", proxy="",
        policy="", scope="", web=False,
    )
    extra = run_extra_args(args, Path("."))
    assert "--web" not in extra
    assert "--no-web" in extra


def test_陌生人不该拿到命令面(tmp_path: Path) -> None:
    """公网通道 + 免鉴权命令 = 谁都能改权限。命令在准入之后才处理。"""
    target = tmp_path / "novels"
    target.mkdir()
    bridge, channel, _ = _bridge(tmp_path, access="pairing")
    channel.push("陌生人", f"/allow-read {target}")

    bridge.run_once()

    assert readroots.load(tmp_path) == []
    assert "配对码" in channel.last  # 拿到的是配对提示，不是执行结果


def test_help_不会去跑_agent_并说明工作区边界(tmp_path: Path) -> None:
    called: list[str] = []
    channel = FakeChannel()
    bridge = Bridge(
        channel, lambda text: called.append(text) or "跑完了",
        access="open", root=tmp_path,
    )
    channel.push("me", "/help")

    bridge.run_once()

    assert called == []
    assert "工作区" in channel.last
    assert "文件" in channel.last
    assert "查资料" in channel.last
    assert "不开放" in channel.last


def test_不认识的命令要说清楚(tmp_path: Path) -> None:
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", "/whatever")
    bridge.run_once()
    assert "不认识的命令" in channel.last


def test_status_能看现状并写上工作区边界(tmp_path: Path) -> None:
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", "/status")
    bridge.run_once()
    assert "工作区" in channel.last
    assert str(tmp_path.resolve()) in channel.last


def test_普通消息不会被当成命令(tmp_path: Path) -> None:
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", "帮我看看 src 里的东西")
    bridge.run_once()
    assert channel.last == "agent 跑了"


# --- Task 6：会话那几条命令（`/sessions` `/use` `/new`）----------------------


def test_sessions_列出会话并标出当前(tmp_path: Path) -> None:
    from spoolkit.kernel import sessions

    root = tmp_path.resolve()
    made = sessions.create_session(root, title="修 calc.py")
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", "/sessions")
    bridge.run_once()
    assert made in channel.last
    assert "修 calc.py" in channel.last


def test_use_切会话并写这条聊天的指针(tmp_path: Path) -> None:
    from spoolkit.kernel import pointers, sessions

    root = tmp_path.resolve()
    first = sessions.create_session(root, title="甲")
    sessions.create_session(root, title="乙")
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", f"/use {first}")
    bridge.run_once()

    assert first in channel.last
    pointer = pointers.get("chat:fake:me")
    assert pointer is not None and pointer.session == first


def test_new_开一条会话并写指针(tmp_path: Path) -> None:
    from spoolkit.kernel import pointers, sessions

    root = tmp_path.resolve()
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", "/new 修 calc.py")
    bridge.run_once()

    pointer = pointers.get("chat:fake:me")
    assert pointer is not None and pointer.session
    assert sessions.read_meta(root, pointer.session)["title"] == "修 calc.py"
    assert "新会话" in channel.last


def test_ws_列出工作区并标出现在这个(tmp_path: Path) -> None:
    from spoolkit.kernel import workspaces as kws

    root = tmp_path.resolve()
    kws.register(root)
    bridge, channel, _ = _bridge(tmp_path)
    channel.push("me", "/ws")
    bridge.run_once()
    assert str(root) in channel.last
    assert "现在" in channel.last


def test_没有聊天身份时切不了(tmp_path: Path) -> None:
    """防御性：拿不到 client_key 就不要偷偷写别人的指针。"""
    from spoolkit.bridge import commands
    from spoolkit.kernel.target import Target

    target = Target(
        root=tmp_path.resolve(),
        session="s",
        workspace_source="explicit",
        session_source="default",
    )
    assert "聊天身份" in commands.handle("/new 一", target)
    assert "聊天身份" in commands.handle("/ws use 别的", target)
    assert "聊天身份" in commands.handle("/use 1", target)
