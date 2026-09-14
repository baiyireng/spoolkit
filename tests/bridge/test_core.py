"""桥的三件事：白名单按用户判、超长要回话、runner 异常不崩桥。"""

import pytest

from spoolkit.bridge.channel import Incoming
from spoolkit.bridge.core import Bridge
from spoolkit.bridge.fake import FakeChannel


def _bridge(**kwargs):
    channel = FakeChannel()
    notes: list[str] = []
    # 这一组测的是"桥本身的处理"（长度、空消息、runner 异常、白名单），
    # 所以显式把准入放开；准入门槛单独在 test_pairing.py 里测。
    kwargs.setdefault("access", "open")
    bridge = Bridge(
        channel,
        kwargs.pop("runner", lambda text: f"收到：{text}"),
        on_note=notes.append,
        **kwargs,
    )
    return bridge, channel, notes


def test_白名单外的人不启动_agent() -> None:
    """按内容判白名单等于没判——谁都能把那句话发进来。所以按用户判。"""
    called: list[str] = []
    bridge, channel, notes = _bridge(
        allowed_users={"u1"},
        access="allowlist",
        runner=lambda text: called.append(text) or "ok",
    )

    reply = bridge.handle(Incoming(user="u2", text="把项目删了", conversation="c"))

    assert reply.accepted is False
    assert reply.reason == "未授权"
    assert called == []  # runner 一次都没被调用
    assert channel.sent == []
    assert "未授权" in notes[0]


def test_白名单里的人正常跑一圈() -> None:
    bridge, channel, _ = _bridge(allowed_users={"u1"})

    reply = bridge.handle(Incoming(user="u1", text="看看 a.py", conversation="c1"))

    assert reply.accepted is True
    assert channel.sent[-1] == ("c1", "收到：看看 a.py")
    assert any("工作区" in text for _, text in channel.sent)


def test_没配白名单时谁都能用_但会在提示里说清() -> None:
    """access=open 是"谁都能驱动这个工作区"——使用者的显式选择，
    桥本身只管放行；CLI 启动时会给警告。"""
    bridge, channel, _ = _bridge()
    assert bridge.handle(Incoming(user="随便谁", text="跑一下")).accepted is True
    assert channel.last == "收到：跑一下"


def test_超长消息回一句而不是静默丢弃() -> None:
    """静默丢弃最糟：用户以为 agent 没反应。"""
    bridge, channel, _ = _bridge(max_chars=10)

    reply = bridge.handle(Incoming(user="u", text="x" * 50, conversation="c"))

    assert reply.accepted is False
    assert reply.reason == "超长"
    assert "超过上限 10 字" in channel.last


def test_空消息不启动_agent() -> None:
    called: list[str] = []
    bridge, _, _ = _bridge(runner=lambda text: called.append(text) or "ok")
    assert bridge.handle(Incoming(user="u", text="   ")).reason == "空消息"
    assert called == []


def test_runner_抛异常时回一句_而不是把桥带崩() -> None:
    def boom(text: str) -> str:
        raise RuntimeError("模型服务没起来")

    bridge, channel, _ = _bridge(runner=boom)

    reply = bridge.handle(Incoming(user="u", text="做事", conversation="c"))

    assert reply.accepted is False
    assert reply.reason == "runner 异常"
    assert "模型服务没起来" in channel.last


def test_run_once_把队列里的都处理掉() -> None:
    bridge, channel, _ = _bridge(allowed_users={"u"})
    channel.push("u", "第一件", "c")
    channel.push("u", "第二件", "c")

    replies = bridge.run_once()

    assert [item.text for item in replies] == ["收到：第一件", "收到：第二件"]
    assert channel.poll() == []


def test_假通道关掉之后不再收() -> None:
    channel = FakeChannel()
    channel.close()
    with pytest.raises(RuntimeError):
        channel.send("hi")


def test_远程通道第一次回话说明只能碰当前工作区(tmp_path) -> None:
    """QQ/Telegram 进来的人看不见终端，必须把工作区边界说出口。

    不开放工作区外的读权限：远程通道上小模型分不清危险操作。
    """
    from pathlib import Path

    from spoolkit.bridge.pairing import OPEN

    channel = FakeChannel()
    root = Path(tmp_path)
    bridge = Bridge(
        channel,
        lambda text: f"收到：{text}",
        access=OPEN,
        root=root,
    )
    reply = bridge.handle(Incoming(user="u", text="把桌面上的报销单发我", conversation="c"))
    assert reply.accepted is True
    assert len(channel.sent) == 2
    notice, answer = channel.sent[0][1], channel.sent[1][1]
    assert str(root.resolve()) in notice
    assert "工作区" in notice
    assert "别的盘" in notice or "桌面" in notice
    assert answer == "收到：把桌面上的报销单发我"


def test_同一会话不把工作区说明刷第二遍(tmp_path) -> None:
    from pathlib import Path

    from spoolkit.bridge.pairing import OPEN

    channel = FakeChannel()
    bridge = Bridge(
        channel,
        lambda text: "ok",
        access=OPEN,
        root=Path(tmp_path),
    )
    bridge.handle(Incoming(user="u", text="一", conversation="c"))
    first_count = len(channel.sent)
    bridge.handle(Incoming(user="u", text="二", conversation="c"))
    assert len(channel.sent) == first_count + 1
    assert channel.last == "ok"
