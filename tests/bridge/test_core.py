"""桥的三件事：白名单按用户判、超长要回话、runner 异常不崩桥。"""

import pytest

from agents_dev.bridge.channel import Incoming
from agents_dev.bridge.core import Bridge
from agents_dev.bridge.fake import FakeChannel


def _bridge(**kwargs):
    channel = FakeChannel()
    notes: list[str] = []
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
        allowed_users={"u1"}, runner=lambda text: called.append(text) or "ok"
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
    assert channel.sent == [("c1", "收到：看看 a.py")]


def test_没配白名单时谁都能用_但会在提示里说清() -> None:
    """不给白名单是"谁都能驱动这个工作区"——这是使用者的选择，
    但桥不能在这一点上含糊（CLI 启动时会打警告）。"""
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
