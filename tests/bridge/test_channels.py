"""两个官方通道适配器：**没有凭据也要能把代码验完。**

所以两个适配器都把 httpx 的 transport 做成可注入——测试里给它一个假服务，
就能验完整的请求形状（参数名、报错解析）与收消息的解析。
"""

import json

import httpx
import pytest

from spoolkit.bridge.telegram import TelegramChannel
from spoolkit.bridge.wecom import WeComChannel, parse_callback


# --- Telegram ---


def _telegram(handler) -> tuple[TelegramChannel, list[dict]]:
    calls: list[dict] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append({"url": request.url.path, "json": payload})
        return handler(request, payload)

    channel = TelegramChannel("T", transport=httpx.MockTransport(wrapped))
    return channel, calls


def test_telegram_收消息() -> None:
    def handler(request, payload):
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 7,
                        "message": {
                            "from": {"id": 42},
                            "chat": {"id": 42},
                            "text": "把 01 修好",
                        },
                    }
                ],
            },
        )

    channel, calls = _telegram(handler)
    messages = channel.poll()

    assert calls[0]["url"].endswith("/getUpdates")
    assert [m.user for m in messages] == ["42"]
    assert messages[0].text == "把 01 修好"
    assert messages[0].conversation == "42"


def test_telegram_回复发回同一个会话() -> None:
    def handler(request, payload):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    channel, calls = _telegram(handler)
    channel.send("改好了", to="42")

    assert calls[0]["url"].endswith("/sendMessage")
    assert calls[0]["json"] == {"chat_id": "42", "text": "改好了"}


def test_telegram_没收到过消息时不知道发给谁() -> None:
    channel, _ = _telegram(
        lambda request, payload: httpx.Response(200, json={"ok": True})
    )
    with pytest.raises(RuntimeError, match="不知道发给谁"):
        channel.send("hi")


def test_telegram_错误在_body_里也要报出来() -> None:
    """状态码 200 但 ok=false 是它常见的报错方式。"""
    channel, _ = _telegram(
        lambda request, payload: httpx.Response(
            200, json={"ok": False, "description": "chat not found"}
        )
    )
    with pytest.raises(RuntimeError, match="chat not found"):
        channel.send("hi", to="1")


# --- 企业微信 ---


def test_企业微信_先拿_token_再发消息() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={"errcode": 0, "access_token": "T1"})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    channel = WeComChannel(
        corp_id="corp", secret="sec", agent_id="1001",
        transport=httpx.MockTransport(handler),
    )
    channel.send("跑一轮", to="zhangsan")

    assert seen == ["/cgi-bin/gettoken", "/cgi-bin/message/send"]


def test_企业微信_token_会复用() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={"errcode": 0, "access_token": "T1"})
        return httpx.Response(200, json={"errcode": 0})

    channel = WeComChannel(
        corp_id="c", secret="s", agent_id="1", transport=httpx.MockTransport(handler)
    )
    channel.send("一", to="u")
    channel.send("二", to="u")

    # 两次发送只拿一次 token（有效期 7200 秒，不该每次都换）
    assert calls.count("/cgi-bin/gettoken") == 1


def test_企业微信_errcode_非零要报出来() -> None:
    channel = WeComChannel(
        corp_id="c", secret="s", agent_id="1",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"errcode": 60011, "errmsg": "no privilege"}
            )
        ),
    )
    with pytest.raises(RuntimeError, match="60011"):
        channel.access_token()


def test_企业微信_解析回调_文本消息() -> None:
    body = (
        "<xml><ToUserName><![CDATA[corp]]></ToUserName>"
        "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[@bot 把 01 修好]]></Content></xml>"
    )
    messages = parse_callback(body)
    assert [m.user for m in messages] == ["zhangsan"]
    assert messages[0].text == "把 01 修好"  # @提及被去掉


def test_企业微信_回调_非文本先不接() -> None:
    body = (
        "<xml><FromUserName><![CDATA[u]]></FromUserName>"
        "<MsgType><![CDATA[image]]></MsgType></xml>"
    )
    assert parse_callback(body) == []


def test_企业微信_解析不了的回调返回空_不抛() -> None:
    assert parse_callback("这不是 XML") == []
    assert parse_callback("") == []


def test_企业微信_带前缀的回调也能解() -> None:
    """真实回调前面会带签名与随机串，解析要能跳过它们。"""
    body = "abc123\n<xml><FromUserName>u</FromUserName><MsgType>text</MsgType><Content>hi</Content></xml>"
    assert [m.text for m in parse_callback(body)] == ["hi"]
