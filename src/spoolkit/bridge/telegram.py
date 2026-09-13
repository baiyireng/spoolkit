"""Telegram Bot 适配器。

选它的理由：**官方 API、长轮询、不需要公网入口**——在 NAT 后面的机器上也能
收消息（企业微信那条路要一个能被回调的公网 URL）。国内用需要自备代理，
所以它和 WeCom 是两条互补的路。

只要两件事：`sendMessage`（发）与 `getUpdates`（收）。HTTP 走 httpx，
transport 可注入——**没凭据也能把这段代码验完**。

凭据来源：`AGENTS_DEV_TELEGRAM_TOKEN`，或直接传 token。
"""

from __future__ import annotations

from typing import Any

import httpx

from spoolkit.bridge import credentials
from spoolkit.bridge.channel import Incoming

DEFAULT_API = "https://api.telegram.org"


class TelegramChannel:
    """一个 Telegram 机器人 = 这条通道。"""

    name = "telegram"

    def __init__(
        self,
        token: str,
        api: str = DEFAULT_API,
        root=None,
        proxy: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        offset: int = 0,
    ) -> None:
        self.token, self.token_source = (
            (token, "命令行")
            if token
            else credentials.find(root, *credentials.TELEGRAM_TOKEN)
        )
        self._client = httpx.Client(
            base_url=f"{api.rstrip('/')}/bot{token}",
            timeout=timeout,
            transport=transport,
            proxy=proxy,
        )
        self._offset = offset
        self._default_to = ""

    # --- 通道协议 ---

    def send(self, text: str, to: str = "") -> None:
        chat_id = to or self._default_to
        if not chat_id:
            raise RuntimeError("不知道发给谁：这条通道还没收到过消息")
        response = self._client.post(
            "/sendMessage",
            json={"chat_id": chat_id, "text": text[:4096]},
        )
        _raise_for_error(response, "sendMessage")

    def poll(self) -> list[Incoming]:
        """长轮询一次（由 Telegram 那边等最多 25 秒），把新消息取回来。"""
        response = self._client.post(
            "/getUpdates",
            json={"offset": self._offset, "timeout": 25},
            timeout=40.0,
        )
        _raise_for_error(response, "getUpdates")
        updates: list[dict[str, Any]] = (response.json() or {}).get("result") or []
        messages: list[Incoming] = []
        for update in updates:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            body = update.get("message") or update.get("edited_message") or {}
            text = str(body.get("text") or "").strip()
            sender = body.get("from") or {}
            chat = body.get("chat") or {}
            if not text:
                continue
            if chat.get("id") is not None:
                self._default_to = str(chat["id"])
            messages.append(
                Incoming(
                    user=str(sender.get("id") or chat.get("id") or ""),
                    text=text,
                    conversation=str(chat.get("id") or ""),
                    raw=str(update.get("update_id", "")),
                )
            )
        return messages

    def close(self) -> None:
        self._client.close()


def _raise_for_error(response: httpx.Response, what: str) -> None:
    """Telegram 的错误藏在 JSON 的 ok/description 里，不能只看状态码。"""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 200 or not payload.get("ok", True):
        detail = payload.get("description") or response.text[:200]
        raise RuntimeError(f"{what} 失败：{detail}")
