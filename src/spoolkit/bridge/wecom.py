"""企业微信（WeCom）自建应用适配器。

为什么是它：手机在手边、国内可用、官方 API、免费档就够用——**这是"私人微信"
在合规前提下最接近的替代**（企业微信能把消息转到微信里看）。

两件事分开说清：

- **发**：`gettoken` 拿 access_token，再 `message/send`。可以本机直接验
  （给个假 transport）。
- **收**：企业微信是**回调推送**——它要求一个公网可达的 URL，并且带
  URL 验证（`echostr`）与 AES 加密。这一版只实现"把回调报文解析成
  `Incoming`"（`parse_callback`），**不改动 agent 侧的任何东西**；
  把它接到公网入口是部署问题（文档里写了形状）。

凭据来源：`AGENTS_DEV_WECOM_CORP_ID` / `AGENTS_DEV_WECOM_SECRET` /
`AGENTS_DEV_WECOM_AGENT_ID`，或直接传参。
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET

import httpx

from spoolkit.bridge import credentials
from spoolkit.bridge.channel import Incoming

DEFAULT_API = "https://qyapi.weixin.qq.com"
TOKEN_TTL = 7000.0  # 企业微信的 access_token 有效期 7200 秒，留点余量


class WeComChannel:
    """企业微信自建应用：能发消息，能把回调报文解析成消息。"""

    name = "wecom"

    def __init__(
        self,
        corp_id: str = "",
        secret: str = "",
        agent_id: str = "",
        root=None,
        api: str = DEFAULT_API,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        clock=time.monotonic,
    ) -> None:
        self.corp_id, self.corp_id_source = _resolve(corp_id, root, credentials.WECOM_CORP_ID)
        self.secret, self.secret_source = _resolve(secret, root, credentials.WECOM_SECRET)
        self.agent_id, self.agent_id_source = _resolve(
            agent_id, root, credentials.WECOM_AGENT_ID
        )
        self._client = httpx.Client(
            base_url=api.rstrip("/"), timeout=timeout, transport=transport
        )
        self._clock = clock
        self._token = ""
        self._token_at = 0.0
        self._pending: list[Incoming] = []
        self._default_to = ""

    # --- 通道协议 ---

    def send(self, text: str, to: str = "") -> None:
        target = to or self._default_to
        if not target:
            raise RuntimeError("不知道发给谁：这条通道还没收到过消息（缺 userid）")
        response = self._client.post(
            "/cgi-bin/message/send",
            params={"access_token": self.access_token()},
            json={
                "touser": target,
                "msgtype": "text",
                "agentid": int(self.agent_id or 0),
                "text": {"content": text[:2000]},
                "safe": 0,
            },
        )
        _raise_for_error(response, "message/send")

    def poll(self) -> list[Incoming]:
        """回调是被推过来的：`feed_callback()` 收下，poll 取走。"""
        messages, self._pending = self._pending, []
        return messages

    def close(self) -> None:
        self._client.close()

    # --- 企业微信特有的两件事 ---

    def access_token(self, refresh: bool = False) -> str:
        if not refresh and self._token and self._clock() - self._token_at < TOKEN_TTL:
            return self._token
        response = self._client.get(
            "/cgi-bin/gettoken",
            params={"corpid": self.corp_id, "corpsecret": self.secret},
        )
        _raise_for_error(response, "gettoken")
        payload = response.json() or {}
        self._token = str(payload.get("access_token") or "")
        self._token_at = self._clock()
        if not self._token:
            raise RuntimeError("gettoken 没给出 access_token")
        return self._token

    def feed_callback(self, body: str) -> list[Incoming]:
        """把一条回调报文收下（返回解析出来的消息）。

        注意：**企业微信的正式回调是加密的**，解密要用它给的 EncodingAESKey，
        这一步留在部署层（文档里写了）。这里解析的是明文的 XML 形状——
        形状一样，所以解析与桥这一段是现在就能验的。
        """
        messages = parse_callback(body)
        for message in messages:
            self._default_to = message.conversation or message.user
        self._pending.extend(messages)
        return messages


def parse_callback(body: str) -> list[Incoming]:
    """解析企业微信回调 XML。认不出来的报文返回空列表，不抛。"""
    text = (body or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # 有些部署会在 XML 前面带上随机串与签名（密文报文就是这样），
        # 这里退一步：从第一个 '<' 开始再试一次。
        start = text.find("<")
        if start < 0:
            return []
        try:
            root = ET.fromstring(text[start:])
        except ET.ParseError:
            return []

    def field(name: str) -> str:
        node = root.find(name)
        return (node.text or "").strip() if node is not None else ""

    if field("MsgType") not in ("text", ""):
        # 图片/语音/事件等先不接：显式返回空，别让桥拿到一条读不懂的消息。
        return []
    content = field("Content") or field("Event")
    user = field("FromUserName")
    if not content or not user:
        return []
    return [
        Incoming(
            user=user,
            text=_strip_mentions(content),
            conversation=user,
            raw=text[:500],
        )
    ]


def _strip_mentions(text: str) -> str:
    """去掉 @机器人 那类前缀（企业微信会在群里带上它）。"""
    return re.sub(r"^@\S+\s*", "", text).strip()


def _raise_for_error(response: httpx.Response, what: str) -> None:
    """企业微信的错误在 JSON 的 errcode/errmsg 里，状态码永远是 200。"""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code = payload.get("errcode", 0)
    if response.status_code != 200 or code not in (0, None):
        detail = payload.get("errmsg") or response.text[:200]
        raise RuntimeError(f"{what} 失败（errcode={code}）：{detail}")


def _resolve(given: str, root, names) -> tuple[str, str]:
    if given:
        return given, "命令行"
    return credentials.find(root, *names)
