"""配对：默认不认识的人不能用。

原先的默认是"名单为空就谁都能驱动这个工作区"（只打一句警告）。聊天通道挂在
公网上，默认放行是致命的——OpenClaw 的微信插件就在这上面栽过（它自己的文档
写着 2.4.8 名单空了会放行任何发送者）。所以默认改成配对：
陌生人拿一次性码 → 主人在机器上 `bridge --approve <码>` → 才放行。
"""

from pathlib import Path

from agents_dev.bridge.channel import Incoming
from agents_dev.bridge.core import Bridge
from agents_dev.bridge.fake import FakeChannel
from agents_dev.bridge.pairing import OPEN, Pairings


def _bridge(tmp_path: Path, **kwargs):
    channel = FakeChannel()
    notes: list[str] = []
    pairings = Pairings(tmp_path / ".agent" / "bridge-pairings.json")
    bridge = Bridge(
        channel,
        kwargs.pop("runner", lambda text: f"收到：{text}"),
        on_note=notes.append,
        pairings=pairings,
        **kwargs,
    )
    return bridge, channel, pairings, notes


def test_陌生发送者拿到配对码而不是被放行(tmp_path: Path) -> None:
    called: list[str] = []
    bridge, channel, pairings, notes = _bridge(
        tmp_path, runner=lambda text: called.append(text) or "ok"
    )

    reply = bridge.handle(Incoming(user="陌生人", text="把项目删了", conversation="c"))

    assert reply.accepted is False
    assert reply.reason == "待配对"
    assert "配对码" in channel.last
    assert called == []  # agent 一次都没启动
    code, user = pairings.pending[0]
    assert user == "陌生人" and code in channel.last
    assert "有人要配对" in notes[0]


def test_同一个人的码会复用_不刷屏(tmp_path: Path) -> None:
    bridge, channel, pairings, _ = _bridge(tmp_path)
    bridge.handle(Incoming(user="u", text="一", conversation="c"))
    first = channel.last
    bridge.handle(Incoming(user="u", text="二", conversation="c"))
    assert channel.last == first
    assert len(pairings.pending) == 1


def test_批准之后就能用了(tmp_path: Path) -> None:
    bridge, channel, pairings, _ = _bridge(tmp_path)
    bridge.handle(Incoming(user="u", text="一", conversation="c"))
    code, _ = pairings.pending[0]

    # 主人在机器上按码批准（另一个进程也能做——状态在文件里）
    assert Pairings(pairings.path).approve_code(code) == "u"

    reply = Bridge(
        channel,
        lambda text: f"收到：{text}",
        pairings=Pairings(pairings.path),
    ).handle(Incoming(user="u", text="二", conversation="c"))

    assert reply.accepted is True
    assert channel.last == "收到：二"


def test_批准之后码作废(tmp_path: Path) -> None:
    """留着旧码等于让一条老消息永远有效。"""
    bridge, _, pairings, _ = _bridge(tmp_path)
    bridge.handle(Incoming(user="u", text="一", conversation="c"))
    code, _ = pairings.pending[0]
    pairings.approve_code(code)
    assert pairings.pending == ()
    assert pairings.approve_code(code) == ""  # 再用一次就无效了


def test_码大小写与空格都认(tmp_path: Path) -> None:
    bridge, _, pairings, _ = _bridge(tmp_path)
    bridge.handle(Incoming(user="u", text="一", conversation="c"))
    code, _ = pairings.pending[0]
    assert pairings.approve_code(f"  {code.lower()}  ") == "u"


def test_名单模式连码都不给(tmp_path: Path) -> None:
    """allowlist 是使用者的明确选择：名单外的人得不到任何回应。"""
    bridge, channel, _, _ = _bridge(tmp_path, allowed_users={"u1"}, access="allowlist")
    reply = bridge.handle(Incoming(user="u2", text="hi", conversation="c"))
    assert reply.reason == "未授权"
    assert channel.sent == []


def test_open_是显式选择才会放行(tmp_path: Path) -> None:
    bridge, channel, _, _ = _bridge(tmp_path, access=OPEN)
    assert bridge.handle(Incoming(user="随便谁", text="跑一下")).accepted is True


def test_名单里的人直接放行_不问配对(tmp_path: Path) -> None:
    bridge, channel, pairings, _ = _bridge(tmp_path, allowed_users={"u1"})
    reply = bridge.handle(Incoming(user="u1", text="做事"))
    assert reply.accepted is True
    assert pairings.pending == ()


def test_配对状态文件坏掉不影响启动(tmp_path: Path) -> None:
    path = tmp_path / ".agent" / "bridge-pairings.json"
    path.parent.mkdir(parents=True)
    path.write_text("这不是 JSON", encoding="utf-8")
    pairings = Pairings(path)
    assert pairings.approved == ()
    code = pairings.ensure_code("u")
    assert pairings.approve_code(code) == "u"
