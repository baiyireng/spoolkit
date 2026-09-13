"""QQ 官方机器人（QQ Bot API v2）适配器。

**这是官方接口，不是逆向**：腾讯的 QQ 机器人开放平台给的就是这套——
HTTP 发消息 + WebSocket 网关收事件。所以不需要第三方 hook，也没有封号风险
（机器人本身就是平台允许的形态）。

三件事：

1. **拿凭证**：`POST https://bots.qq.com/app/getAppAccessToken`，
   用 AppID + AppSecret 换 access_token（有有效期，缓存起来）。
2. **发消息**：被动回复要带 `msg_id`（+`msg_seq`）——这是平台的规矩，
   带上才算"回复那条消息"，不带就是主动推送（有配额限制）。
   私聊走 `/v2/users/{openid}/messages`，群里 @ 走 `/v2/groups/{openid}/messages`。
3. **收事件**：WebSocket 网关（op 10 HELLO → 发 op 2 IDENTIFY → 心跳 op 1）。
   私聊事件是 `C2C_MESSAGE_CREATE`、群 @ 是 `GROUP_AT_MESSAGE_CREATE`，
   两者都要 `intents` 里的 `1<<25`（GROUP_AND_C2C_EVENT）。

凭据来源：`AGENTS_DEV_QQ_APPID` / `AGENTS_DEV_QQ_SECRET`，或直接传参。
沙箱环境加 `sandbox=True`（域名换成 `sandbox.api.sgroup.qq.com`）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import httpx

from spoolkit.bridge.channel import Incoming
from spoolkit.bridge import credentials
from spoolkit.bridge.ws import Timeout as WebSocketTimeout
from spoolkit.bridge.ws import WebSocket, WebSocketError

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
DEFAULT_API = "https://api.sgroup.qq.com"
SANDBOX_API = "https://sandbox.api.sgroup.qq.com"

# 群与单聊的消息事件（C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE / 好友添加…）
INTENT_GROUP_AND_C2C = 1 << 25

OP_HELLO = 10
OP_IDENTIFY = 2
OP_HEARTBEAT = 1
OP_RESUME = 6

# QQ 对单条消息的内容有上限（按**字节**算，中文一个字 3 字节）。超了我们这边
# 拿不到任何回应——用户那边就是"看不见结论"。所以长答复要分条发。
# 取 3000 字节：比常见的 4000 字节上限留足余量，又不至于把一条正常结论切碎。
MAX_CONTENT_BYTES = 3000


def _split_for_qq(text: str, limit: int = MAX_CONTENT_BYTES) -> list[str]:
    """把长文本切成若干条，按字节算、不切坏字符。"""
    chunks: list[str] = []
    current = ""
    size = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > limit and current:
            chunks.append(current)
            current, size = "", 0
        current += char
        size += width
    chunks.append(current)
    return chunks

# 心跳**提前**这么多秒发。刚好卡在间隔边界上有风险：平台那边也在数秒，
# 网络抖一下就成了"这一轮没心跳"。提前一点点不影响规矩（平台只查有没有按时）。
HEARTBEAT_MARGIN = 1.0


def _heartbeat_delay(interval: float) -> float:
    """下一次心跳等多久：比间隔提前 `HEARTBEAT_MARGIN` 秒。

    提前量不能超过间隔的一半——测试用的是 0.3s 这种放大过的间隔，
    若一律"提前 1 秒"就变成负数了。
    """
    return max(interval * 0.5, interval - HEARTBEAT_MARGIN)


class QQBotChannel:
    """一个 QQ 机器人 = 这条通道。"""

    name = "qqbot"

    def __init__(
        self,
        app_id: str = "",
        secret: str = "",
        sandbox: bool = False,
        root=None,
        token_url: str = TOKEN_URL,
        api: str = "",
        transport: httpx.BaseTransport | None = None,
        proxy: str = "",
        timeout: float = 20.0,
        clock=time.monotonic,
        ws_factory=None,
        on_note=None,
    ) -> None:
        # 凭据来源要能说出来（用户可能写在 .env 里，也可能挂在环境变量上）。
        self.app_id, self.app_id_source = _resolve(app_id, root, credentials.QQ_APP_ID)
        self.secret, self.secret_source = _resolve(secret, root, credentials.QQ_SECRET)
        self.sandbox = sandbox
        self._token_url = token_url
        self._api = api or (SANDBOX_API if sandbox else DEFAULT_API)
        # base_url 必须给：不然 `/gateway`、`/v2/...` 这些相对路径会被 httpx
        # 当成非法 URL（"unknown url type"），而那个报错完全看不出是缺了 base_url。
        # 代理是可选的：平台侧有 IP 白名单，借一台云主机出去（ssh -D 给的
        # SOCKS5）就能把白名单固定成那台主机的 IP。
        # 注意 httpx 走 SOCKS 需要 `httpx[socks]`（可选的加装，不是主包依赖）。
        try:
            self._client = httpx.Client(
                base_url=self._api,
                timeout=timeout,
                transport=transport,
                proxy=proxy or None,
            )
        except ImportError as exc:
            # httpx 走 SOCKS 要可选的 socksio，而它报出来的是 `No module named
            # 'socksio'`——完全看不出"该往哪装"。这里的场景几乎都是同一个
            # （借云主机出口过 QQ 的 IP 白名单），所以直接给两条装法。
            raise RuntimeError(
                "让通道走 SOCKS 代理需要 httpx 的 socks 可选依赖（socksio）：\n"
                '  uv pip install "httpx[socks]"          # 在虚拟环境里\n'
                '  uv tool install --reinstall --editable ".[socks]"   # 用 uv 工具装的\n'
                f"（原始错误：{exc}）"
            ) from exc
        self._clock = clock
        self._ws_factory = ws_factory or (
            lambda url: WebSocket(url, timeout=timeout, proxy=proxy or None)
        )
        self._token = ""
        self._token_expire_at = 0.0
        self._queue: list[Incoming] = []
        # 默认回复目标：从最后一条收到的消息来（回给同一个人/同一个群）。
        # 存成 (kind, openid) 而不是拼好的字符串——省的号段里再拆一次。
        self._default_to = ""      # "c2c:<openid>" / "group:<openid>"：最近一条消息
        self._last_event_id = ""   # 被动回复要带 msg_id，就靠它记住
        self._last_kind = ""       # "c2c" / "group"（规范化过的，不是事件类型名）
        self._seq = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._note = on_note or (lambda text: None)

    # --- 通道协议 ---

    def send(self, text: str, to: str = "") -> None:
        """`to` 的形状决定发到哪：`c2c:<openid>` / `group:<openid>` / `channel:<id>`。

        也接受**裸的 openid**（事件里的 `conversation` 就是它）：那时按最后一条消息
        的场景回。

        为什么要这条兜底——真踩过，而且是"手机发消息完全没反应"的真凶：C2C 事件里
        `conversation` 是裸 openid，`partition(":")` 于是把 openid 当成 kind、
        openid 变成空串，请求打到 `/v2/channels//messages`（频道接口、空 id），
        QQ 回 `code=11001 不支持的调用`。日志看着像"平台不允许"，其实是**发错了接口**。
        """
        target = to or self._default_to
        if not target:
            raise RuntimeError("不知道发给谁：这条通道还没收到过消息")
        kind, _, openid = target.partition(":")
        if not openid:
            kind, openid = (self._last_kind or "c2c"), kind
        if kind == "c2c":
            url = f"/v2/users/{openid}/messages"
        elif kind == "group":
            url = f"/v2/groups/{openid}/messages"
        else:
            url = f"/v2/channels/{openid}/messages"
        headers = {"Authorization": f"QQBot {self.access_token()}"}
        chunks = _split_for_qq(text)
        if len(chunks) > 1:
            self._note(f"答复太长（{len(text)} 字），分 {len(chunks)} 条发")
        for index, chunk in enumerate(chunks, start=1):
            self._send_one(url, chunk, kind, index, headers)

    def _send_one(
        self, url: str, text: str, kind: str, seq: int, headers: dict
    ) -> None:
        """发一条（不切分）。被动回复被拒时退一步试主动推送。"""
        payload: dict[str, Any] = {
            "content": text,
            "msg_type": 0,
        }
        passive = self._passive_fields(kind, seq)
        payload.update(passive)
        response = self._client.post(url, json=payload, headers=headers)
        try:
            _raise_for_error(response, "发消息")
            return
        except RuntimeError as exc:
            # 注意：`except ... as exc` 出了这个块，名字就被删掉了（Python 的
            # 规矩），所以必须先存下来再往下用——否则报的是 UnboundLocalError，
            # 离真正的失败原因十万八千里。
            passive_error = exc
            if not passive:
                raise
            # 被动回复被拒时退一步试**主动推送**（不带 msg_id）。真机上遇到过
            # `code=11001 不支持的调用`：哪天被动那条路被平台关了，主动这条
            # 至少还能把话送到；两条都不行才报错，并且把两次的原因都带上。
            self._note(f"被动回复被拒（{passive_error}），改试主动推送")
        active = self._client.post(
            url,
            json={"content": text, "msg_type": 0},
            headers=headers,
        )
        try:
            _raise_for_error(active, "发消息（主动）")
        except RuntimeError as second:
            raise RuntimeError(
                f"{passive_error}；改试主动推送也不行：{second}"
            ) from second

    def poll(self) -> list[Incoming]:
        """取走网关线程攒下的消息。**不阻塞**——没有就返回空列表。"""
        messages, self._queue = self._queue, []
        return messages

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._client.close()

    # --- 凭证 ---

    def access_token(self, refresh: bool = False) -> str:
        if not refresh and self._token and self._clock() < self._token_expire_at:
            return self._token
        response = self._client.post(
            self._token_url, json={"appId": self.app_id, "clientSecret": self.secret}
        )
        _raise_for_error(response, "getAppAccessToken")
        payload = response.json() or {}
        token = str(payload.get("access_token") or "")
        if not token:
            raise RuntimeError("拿凭证没拿到 access_token")
        expires = float(payload.get("expires_in") or 7200)
        self._token = token
        # 提前 60 秒换，免得卡在到期那一瞬间
        self._token_expire_at = self._clock() + max(30.0, expires - 60.0)
        return token

    def gateway_url(self) -> str:
        response = self._client.get(
            "/gateway", headers={"Authorization": f"QQBot {self.access_token()}"}
        )
        _raise_for_error(response, "取网关地址")
        url = str((response.json() or {}).get("url") or "")
        if not url:
            raise RuntimeError("网关地址是空的")
        return url

    # --- 接收：网关线程 ---

    def start(self) -> None:
        """起一个后台线程连网关。收事件是长期连接，桥那边只需 poll。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._gateway_loop, daemon=True)
        self._thread.start()

    def _gateway_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_gateway_once()
                self._note("QQ 网关断开，5 秒后重连")
            except (WebSocketError, OSError, RuntimeError) as exc:
                # 网络抖动不该把通道打死：退一步重连（resume 留给下一步优化——
                # 断了之后 QQ 会把没收到的事件按 op 0 重发一段时间）。
                self._note(f"QQ 网关出错（{type(exc).__name__}: {exc}），5 秒后重连")
            # 两句提示都写着"5 秒后重连"，但当初只有出错那条真的等了——正常返回
            # 那条会**立刻**重连，撞上平台侧的限流就更连不上。等待挪到这里。
            self._stop.wait(5.0)

    def _run_gateway_once(self) -> None:
        ws = self._ws_factory(self.gateway_url())
        ws.connect()
        # 单次收数据的等待上限。为什么不只是个常数：见下面算 `wait` 的地方。
        read_wait = float(getattr(ws, "timeout", 20.0) or 20.0)
        # 心跳必须**等 HELLO 给了间隔**再开始——在那之前发没有任何依据，
        # 而"建连接时立刻发一发"曾经真的发生过（`heartbeat_at` 初值是 0）。
        heartbeat_at: float | None = None
        interval = 30.0
        try:
            while not self._stop.is_set():
                if heartbeat_at is not None and self._clock() >= heartbeat_at:
                    ws.send_text(json.dumps({"op": OP_HEARTBEAT, "d": self._seq or None}))
                    heartbeat_at = self._clock() + _heartbeat_delay(interval)
                # 关键：**等到下一次心跳之前就要醒**。
                # 真机踩过的坑：心跳间隔 41.25s，而 recv 一次要阻塞 20s，心跳只在
                # 两次 recv 之间检查——于是它总在 60s 左右才发出去，平台直接掐线，
                # 表现成"每 60 秒断一次、反复重连"，看着像网络问题，其实是自己的时序。
                if heartbeat_at is None:
                    wait = read_wait
                else:
                    wait = min(read_wait, max(0.05, heartbeat_at - self._clock()))
                try:
                    text = ws.recv(timeout=wait)
                except WebSocketTimeout:
                    # 长连接上几十秒没有事件是正常的：接着等，别当断开重连。
                    continue
                if text is None:
                    # 断开的原因要留痕：没有它，"为什么断"只能靠猜。
                    detail = getattr(ws, "close_info", "") or "对端直接断开（没给关闭帧）"
                    self._note(f"QQ 网关被关闭：{detail}")
                    return
                payload = json.loads(text)
                op = payload.get("op")
                if payload.get("s") is not None:
                    self._seq = int(payload["s"])
                if op == OP_HELLO:
                    interval = float((payload.get("d") or {}).get("heartbeat_interval", 30000)) / 1000
                    ws.send_text(json.dumps(self._identify_payload()))
                    # 连上就说一声：真机排查时唯一能看到的"在线"信号。
                    self._note(f"QQ 网关已连上（心跳 {interval:.0f}s，intents={INTENT_GROUP_AND_C2C}）")
                    # 识别之后立刻发一次心跳（各家客户端都这样），之后按间隔来。
                    heartbeat_at = self._clock()
                elif op == 0:
                    # 注意传**整个**事件：`t`（事件类型）在外层，
                    # `parse_event` 靠它分辨私聊/群聊——只传 `d` 会认不出来。
                    self.feed_event(payload)
                elif op in (7, 9):
                    # 7 = 服务端要求重连，9 = 会话无效（token 或 intents 不对）
                    self._note(f"QQ 平台要求重连（op={op}）：请检查 AppID/凭据与 intents")
                    return
        finally:
            ws.close()

    def _identify_payload(self) -> dict:
        return {
            "op": OP_IDENTIFY,
            "d": {
                "token": f"QQBot {self.access_token()}",
                "intents": INTENT_GROUP_AND_C2C,
                "shard": [0, 1],
                "properties": {},
            },
        }

    # --- 事件解析（纯函数，测试直接用） ---

    def feed_event(self, event: dict) -> list[Incoming]:
        """处理一条网关事件，把消息放进队列。返回这次解析出来的消息。"""
        messages = parse_event(event)
        # 每一条事件都留痕。真机上排查过一次"手机发了消息没反应"：当时日志里
        # 只有"网关已连上"，既看不到事件到没到、也看不到事件是什么类型，
        # 只能靠猜。一行日志的成本换掉一小时的猜测。
        kind = str(event.get("t") or "(无类型)")
        if messages:
            self._note(f"收到 {kind}：{messages[-1].user} 说 {messages[-1].text[:40]!r}")
        elif kind not in ("READY",):
            self._note(f"收到 {kind}（这条事件没有解析成消息，原样略过）")
        if messages:
            latest = messages[-1]
            # 群事件回群里，私聊回私聊——回错了人比不回更糟。
            self._last_kind = "group" if kind == "GROUP_AT_MESSAGE_CREATE" else "c2c"
            self._default_to = f"{self._last_kind}:{latest.conversation}"
            self._last_event_id = latest.message_id
        self._queue.extend(messages)
        return messages

    def _passive_fields(self, kind: str, seq: int = 1) -> dict:
        """被动回复的字段：带上 msg_id 才算"回复那条消息"（有 5 分钟窗口）。

        `msg_seq` 是**同一条消息的第几条回复**：分条发的时候必须递增，否则平台
        会把后面的当重复消息丢掉（真机上试出来过 `40054005 消息被去重`）。
        """
        if not self._last_event_id or kind == "channel":
            return {}
        fields = {"msg_id": self._last_event_id, "msg_seq": seq}
        return fields


def parse_event(event: dict) -> list[Incoming]:
    """把网关事件翻成 `Incoming`。认不出来的返回空列表（不抛）。

    私聊：`C2C_MESSAGE_CREATE`（`author.user_openid`、`content`）
    群聊：`GROUP_AT_MESSAGE_CREATE`（`group_openid` + `author.member_openid`）
    """
    kind = str(event.get("t") or "")
    data = event.get("d") or {}
    content = _clean(str(data.get("content") or ""))
    author = data.get("author") or {}
    message_id = str(data.get("id") or "")
    if kind == "C2C_MESSAGE_CREATE":
        user = str(author.get("user_openid") or "")
        if not user or not content:
            return []
        return [
            Incoming(
                user=user,
                text=content,
                conversation=user,
                message_id=message_id,
                raw=kind,
            )
        ]
    if kind == "GROUP_AT_MESSAGE_CREATE":
        group = str(data.get("group_openid") or "")
        member = str(author.get("member_openid") or "")
        if not group or not content:
            return []
        return [
            Incoming(
                user=member or group,
                text=content,
                conversation=group,
                message_id=message_id,
                raw=kind,
            )
        ]
    return []


def _clean(text: str) -> str:
    """去掉 <@!123> 这类提及标记与首尾空白。"""
    out = text.strip()
    while out.startswith("<@") and ">" in out:
        out = out[out.index(">") + 1 :].strip()
    return out


def _raise_for_error(response: httpx.Response, what: str) -> None:
    """QQ 平台的错误码在 JSON 里（`code`/`message`），状态码可能还是 200。"""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code = payload.get("code")
    if response.status_code >= 400 or code not in (0, None):
        detail = payload.get("message") or response.text[:200]
        # trace_id 是平台侧排查的凭据：拿着它去提工单/查文档才有得对。原先只打
        # code+message，真出事时（11001 那次）少了这条线索。
        trace = payload.get("trace_id")
        suffix = f"，trace_id={trace}" if trace else ""
        raise RuntimeError(f"{what} 失败（code={code}）：{detail}{suffix}")


def _resolve(given: str, root, names) -> tuple[str, str]:
    if given:
        return given, "命令行"
    return credentials.find(root, *names)
