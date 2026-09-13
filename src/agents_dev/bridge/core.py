"""桥：把通道收到的消息交给 agent，再把结果发回去。

三件事，件件都有它存在的理由：

1. **白名单按用户判**，不是按内容判。按内容判（"消息里有没有敏感词"）
   等于没判——任何人都能把那句话发进来。所以 `allowed_users` 里没有的人，
   消息直接丢掉，连 agent 都不会启动。
2. **单条长度上限**。聊天通道的消息可以是任意长，而 agent 的上下文是
   有限资源：一条一万字的消息会把这一步的预算吃光。超长就丢掉并回一句
   说明——**静默丢弃最糟**，用户以为 agent 没反应。
3. **runner 注入**。桥不认识 agent（进程内循环、HTTP、MCP 都行），
   只认一个 `Callable[[str], str]`。测试因此不需要真跑一个 agent。

回复一律走通道；`run_once()` 处理一轮 poll 到的全部消息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from agents_dev.bridge.channel import Channel, Incoming

DEFAULT_MAX_CHARS = 4000


@dataclass(frozen=True)
class Reply:
    """处理一条消息的结果（同时也是给测试看的凭据）。"""

    user: str
    text: str
    accepted: bool
    reason: str = ""


class Bridge:
    """收消息 → 判白名单与长度 → 交给 runner → 把回复发回通道。"""

    def __init__(
        self,
        channel: Channel,
        runner: Callable[[str], str],
        allowed_users: set[str] | frozenset[str] = frozenset(),
        max_chars: int = DEFAULT_MAX_CHARS,
        on_note: Callable[[str], None] | None = None,
    ) -> None:
        self.channel = channel
        self.runner = runner
        self.allowed_users = set(allowed_users)
        self.max_chars = max_chars
        self._note = on_note or (lambda text: None)

    def handle(self, message: Incoming) -> Reply:
        """处理一条消息。任何一条都不该让桥崩掉。"""
        if self.allowed_users and message.user not in self.allowed_users:
            # 谁发的都留着记录，但绝不启动 agent：白名单的意义就在这里。
            self._note(f"忽略未授权用户 {message.user}")
            return Reply(user=message.user, text="", accepted=False, reason="未授权")

        text = message.text.strip()
        if not text:
            return Reply(user=message.user, text="", accepted=False, reason="空消息")
        if len(text) > self.max_chars:
            reply = (
                f"这条消息有 {len(text)} 字，超过上限 {self.max_chars} 字，"
                "没有交给 agent。请拆短一点再发。"
            )
            self.channel.send(reply, message.conversation)
            return Reply(user=message.user, text=reply, accepted=False, reason="超长")

        try:
            answer = self.runner(text)
        except Exception as exc:  # noqa: BLE001 - 桥不该被 agent 的异常带崩
            answer = f"这一轮没跑起来：{type(exc).__name__}: {exc}"
            self.channel.send(answer, message.conversation)
            return Reply(user=message.user, text=answer, accepted=False, reason="runner 异常")

        self.channel.send(answer or "（没有产出）", message.conversation)
        return Reply(user=message.user, text=answer, accepted=True)

    def run_once(self) -> list[Reply]:
        """把当前能取到的消息都处理一遍。"""
        return [self.handle(message) for message in self.channel.poll()]
