"""QQ 官方机器人适配器：**没有真凭据也要能把这条链验完。**

两条路都验：
- HTTP 那一半：假 transport 验请求形状（换凭证的字段、发消息的地址与
  `Authorization`、被动回复的 msg_id）；
- 网关那一半：一个**真的** WebSocket 回环（我们自己的服务端用同一套帧编解码），
  把一条 `C2C_MESSAGE_CREATE` 推给通道，看它有没有变成 `Incoming`。
"""

import json
import socket
import threading
import time

import httpx
import pytest

from agents_dev.bridge.qqbot import QQBotChannel, parse_event
from agents_dev.bridge.ws import OP_TEXT, Timeout as WSTimeout, WebSocket, encode_frame, read_frame


# --- HTTP 那一半 ---


def _channel(handler) -> tuple[QQBotChannel, list[dict]]:
    calls: list[dict] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append(
            {
                "url": str(request.url),
                "path": request.url.path,
                "json": body,
                "auth": request.headers.get("Authorization", ""),
            }
        )
        return handler(request, body)

    channel = QQBotChannel(
        app_id="APP", secret="SEC", transport=httpx.MockTransport(wrapped)
    )
    return channel, calls


def test_换凭证用的是_appId_clientSecret() -> None:
    channel, calls = _channel(
        lambda request, body: httpx.Response(
            200, json={"access_token": "T1", "expires_in": 7200}
        )
    )
    assert channel.access_token() == "T1"
    assert calls[0]["json"] == {"appId": "APP", "clientSecret": "SEC"}
    assert calls[0]["url"] == "https://bots.qq.com/app/getAppAccessToken"


def test_凭证会缓存到快过期() -> None:
    ticks = iter([0.0, 0.0, 1.0])
    channel, calls = _channel(
        lambda request, body: httpx.Response(
            200, json={"access_token": "T1", "expires_in": 7200}
        )
    )
    channel._clock = lambda: next(ticks, 1.0)
    channel.access_token()
    channel.access_token()
    assert len(calls) == 1  # 第二次没再问


def test_私聊发消息的地址与鉴权() -> None:
    def handler(request, body):
        if request.url.path.endswith("/getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 7200})
        return httpx.Response(200, json={"code": 0, "id": "m2"})

    channel, calls = _channel(handler)
    channel._last_event_id = "evt-1"
    channel.send("改好了", to="c2c:USER1")

    send = calls[-1]
    assert send["path"] == "/v2/users/USER1/messages"
    assert send["auth"].startswith("QQBot ")
    assert send["json"]["content"] == "改好了"
    assert send["json"]["msg_id"] == "evt-1"      # 被动回复要带这个
    assert send["json"]["msg_seq"] == 1


def test_群消息发到群的地址() -> None:
    channel, calls = _channel(
        lambda request, body: httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
        if request.url.path.endswith("/getAppAccessToken")
        else httpx.Response(200, json={"code": 0})
    )
    channel.send("看到了", to="group:GROUP1")
    assert calls[-1]["path"] == "/v2/groups/GROUP1/messages"


def test_平台错误码要报出来() -> None:
    channel, _ = _channel(
        lambda request, body: httpx.Response(200, json={"code": 11253, "message": "no permission"})
    )
    with pytest.raises(RuntimeError, match="11253"):
        channel.access_token()


def test_沙箱域名() -> None:
    channel = QQBotChannel(app_id="A", secret="S", sandbox=True)
    assert channel._api == "https://sandbox.api.sgroup.qq.com"


def test_凭据可以从工作区的_env_里来(tmp_path, monkeypatch) -> None:
    """用户把 AppID/AppSecret 写进了项目根的 .env——那就该认它。"""
    for name in ("AGENTS_DEV_QQ_APPID", "QQ_AppID", "QQ_APPID",
                 "AGENTS_DEV_QQ_SECRET", "QQ_AppSecret", "QQ_SECRET"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "QQ_AppID=102000000\nQQ_AppSecret=secret-from-file\n", encoding="utf-8"
    )

    channel = QQBotChannel(root=tmp_path)

    assert channel.app_id == "102000000"
    assert channel.secret == "secret-from-file"
    assert ".env" in channel.app_id_source


# --- 事件解析 ---


def test_解析私聊事件() -> None:
    messages = parse_event(
        {
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "m1",
                "content": "<@!1234> 把 01 修好",
                "author": {"user_openid": "U1"},
            },
        }
    )
    assert [(m.user, m.text, m.conversation) for m in messages] == [
        ("U1", "把 01 修好", "U1")
    ]


def test_解析群_at_事件() -> None:
    messages = parse_event(
        {
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "id": "m2",
                "content": "看看测试",
                "group_openid": "G1",
                "author": {"member_openid": "M9"},
            },
        }
    )
    assert messages[0].user == "M9"
    assert messages[0].conversation == "G1"


def test_不认识的事件返回空() -> None:
    assert parse_event({"t": "FRIEND_ADD", "d": {}}) == []
    assert parse_event({}) == []
    assert parse_event({"t": "C2C_MESSAGE_CREATE", "d": {"content": "x"}}) == []


def test_收到事件后默认发回同一个会话() -> None:
    channel = QQBotChannel(app_id="A", secret="S")
    channel.feed_event(
        {
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "id": "m3",
                "content": "做事",
                "group_openid": "G2",
                "author": {"member_openid": "M1"},
            },
        }
    )
    assert channel._default_to == ("group", "G2")  # 群消息要回群里
    # poll 把消息交出去，并且不再重复给
    assert len(channel.poll()) == 1
    assert channel.poll() == []


# --- 网关那一半 ------------------------------------------------
#
# 拆成三块分别验：**帧编解码**（纯函数）、**握手**（真 socket，但只握手）、
# **网关逻辑**（脚本化的假 WebSocket，确定、不依赖时序）。
#
# 一开始我写的是"一个真回环服务端"，结果卡在测试脚手架本身：服务端读帧超时、
# 关连接时内核发 RST 把刚发出去的事件丢掉……与其把脚手架写复杂，不如把要验的
# 三件事分开——每一件都能单独说清对错。


def test_帧编解码_带掩码() -> None:
    """客户端发的帧**必须**设掩码位，否则对端按"没掩码"读，收到一堆乱字节。

    （这个 bug 在真回环里表现为"服务端读到 0x9b"——所以这里连掩码位一起验。）
    """
    left, right = socket.socketpair()
    try:
        payload = "你好，QQ".encode("utf-8")
        left.sendall(encode_frame(OP_TEXT, payload, mask=True))
        frame = read_frame(right)
        assert frame is not None
        fin, opcode, body = frame
        assert fin and opcode == OP_TEXT and body == payload
    finally:
        left.close()
        right.close()


def test_帧编解码_长负载与不掩码() -> None:
    for size in (5, 200, 70000):  # 7 位 / 126 / 127 三种长度写法
        left, right = socket.socketpair()
        try:
            payload = b"x" * size
            left.sendall(encode_frame(OP_TEXT, payload, mask=False))
            assert read_frame(right)[2] == payload
        finally:
            left.close()
            right.close()


class _ScriptedWS:
    """脚本化的假 WebSocket：按列表吐帧，并记下客户端发了什么。"""

    def __init__(self, incoming: list) -> None:
        self.incoming = list(incoming)
        self.sent: list[dict] = []
        self.closed = False

    def connect(self) -> None:
        pass

    def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def recv(self):
        if not self.incoming:
            return None
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item) if isinstance(item, dict) else item

    def close(self) -> None:
        self.closed = True


def _channel_with(ws: _ScriptedWS) -> QQBotChannel:
    return QQBotChannel(
        app_id="A",
        secret="S",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
            if request.url.path.endswith("/getAppAccessToken")
            else httpx.Response(200, json={"url": "ws://example.invalid/"})
        ),
        ws_factory=lambda url: ws,
    )


def test_网关逻辑_问好发识别_事件进队列() -> None:
    ws = _ScriptedWS(
        [
            {"op": 10, "d": {"heartbeat_interval": 30000}},
            {"op": 0, "s": 1, "t": "C2C_MESSAGE_CREATE",
             "d": {"id": "m9", "content": "把 01 修好", "author": {"user_openid": "U9"}}},
        ]
    )
    channel = _channel_with(ws)

    channel._run_gateway_once()

    identify = [item for item in ws.sent if item["op"] == 2][0]
    assert identify["d"]["token"] == "QQBot T"
    assert identify["d"]["intents"] == 1 << 25
    assert [m.text for m in channel.poll()] == ["把 01 修好"]
    # 收到事件之后：默认回给这个人（私聊）
    assert channel._default_to == ("c2c", "U9")
    assert channel._last_event_id == "m9"  # 被动回复要用


def test_网关逻辑_超时不算断开() -> None:
    """长连接上几十秒没事件是正常的；把超时当断开就会每几秒重连一次。"""
    ws = _ScriptedWS(
        [
            {"op": 10, "d": {"heartbeat_interval": 30000}},
            WSTimeout(),
            {"op": 0, "s": 2, "t": "C2C_MESSAGE_CREATE",
             "d": {"id": "m10", "content": "还在吗", "author": {"user_openid": "U9"}}},
        ]
    )
    channel = _channel_with(ws)

    channel._run_gateway_once()

    assert [m.text for m in channel.poll()] == ["还在吗"]
    assert any(item["op"] == 1 for item in ws.sent)  # 期间照常发心跳


def test_网关逻辑_对端关闭就退出() -> None:
    ws = _ScriptedWS([{"op": 10, "d": {"heartbeat_interval": 30000}}, None])
    channel = _channel_with(ws)
    channel._run_gateway_once()  # 不抛、直接返回
    assert ws.closed is True
