"""桥给最近聊天发断开通知：默认关，通→断才发。"""

from spoolkit.bridge.channel import Incoming
from spoolkit.bridge.core import Bridge
from spoolkit.bridge.fake import FakeChannel


def _bridge(*, notify: bool = False) -> tuple[Bridge, FakeChannel]:
    channel = FakeChannel()
    bridge = Bridge(
        channel,
        lambda text: f"收到：{text}",
        access="open",
        notify_disconnect=notify,
        disconnect_cooldown=0,
    )
    return bridge, channel


def test_默认不给聊天发断开() -> None:
    bridge, channel = _bridge(notify=False)
    bridge.handle(Incoming(user="u", text="hi", conversation="c1"))
    before = list(channel.sent)
    bridge.notify_disconnects(
        [
            {"name": "model", "ok": True, "target": "http://x", "detail": ""},
        ]
    )
    bridge.notify_disconnects(
        [
            {
                "name": "model",
                "ok": False,
                "target": "http://x",
                "detail": "拒绝连接",
            },
        ]
    )
    assert channel.sent == before


def test_打开后发给最近一次放过的会话() -> None:
    bridge, channel = _bridge(notify=True)
    bridge.handle(Incoming(user="u", text="hi", conversation="c1"))
    n = len(channel.sent)
    bridge.notify_disconnects(
        [{"name": "model", "ok": True, "target": "http://x", "detail": ""}]
    )
    bridge.notify_disconnects(
        [
            {
                "name": "model",
                "ok": False,
                "target": "http://127.0.0.1:8080",
                "detail": "拒绝连接",
            },
        ]
    )
    added = channel.sent[n:]
    assert added
    dest, text = added[-1]
    assert dest == "c1"
    assert "不通" in text
    assert "8080" in text


def test_还没人说过话就不发() -> None:
    bridge, channel = _bridge(notify=True)
    bridge.notify_disconnects(
        [{"name": "model", "ok": True, "target": "http://x", "detail": ""}]
    )
    bridge.notify_disconnects(
        [
            {
                "name": "model",
                "ok": False,
                "target": "http://x",
                "detail": "拒绝连接",
            },
        ]
    )
    assert channel.sent == []
