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
from pathlib import Path
from typing import Callable

from spoolkit.bridge.channel import Channel, Incoming
from spoolkit.bridge.commands import handle as handle_command
from spoolkit.bridge.pairing import ALLOWLIST, OPEN, PAIRING, Pairings
from spoolkit.health import DisconnectWatch, down_line

DEFAULT_MAX_CHARS = 4000
OUTBOX_MAX = 8
TRANSIENT_SEND = frozenset(
    {
        "ConnectTimeout",
        "ConnectError",
        "ReadTimeout",
        "WriteTimeout",
        "TimeoutException",
        "NetworkError",
        "ProxyError",
        "ConnectionError",
        "TimeoutError",
        "OSError",
    }
)

# 陌生发送者拿到配对码时的回复模板。口令要写得像人话：这条消息会原样出现在
# 别人的聊天窗口里。
PAIRING_REPLY = (
    "这条通道还不认识你。把下面这个配对码给机器的主人，"
    "他在命令行执行 `spool approve {code}` 之后你就能用了：\n{code}"
)


def _transient_send(exc: BaseException) -> bool:
    """隧道抖、握手超时这类，通了再发还有戏。平台业务错误没有。"""
    return type(exc).__name__ in TRANSIENT_SEND


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
        pairings: Pairings | None = None,
        access: str = PAIRING,
        root: Path | str = ".",
        notify_disconnect: bool = False,
        disconnect_cooldown: float = 300.0,
    ) -> None:
        self.channel = channel
        self.runner = runner
        self.allowed_users = set(allowed_users)
        self.max_chars = max_chars
        self._note = on_note or (lambda text: None)
        self.pairings = pairings
        self.access = access
        # 斜杠命令要改的是桥自己的状态，所以桥得知道工作区在哪。
        # 文件读写边界写在工作方式里给模型看，不推给用户。
        self.root = Path(root)
        self._last_conversation = ""
        # 断开通知只发给**放过的**最近聊天，不发给还在等配对码的人。
        self._notify_conversation = ""
        self._watch = DisconnectWatch(
            enabled=notify_disconnect,
            cooldown=disconnect_cooldown,
        )
        # 发出去失败（隧道抖、握手超时）时把答复留下，通了再补发。
        # 平台业务错误（比如 11001）补发也没用，不进这里。
        self._outbox: list[tuple[str, str]] = []
        # 长任务跑完要能补一条消息回来：把"回给最近这个会话"的能力交给 runner。
        # 只有 AgentRunner 认这个接口，别的 runner（测试里的假货）不受影响。
        setter = getattr(runner, "set_notify", None)
        if callable(setter):
            setter(lambda text: self._send(text, self._last_conversation))

    def handle(self, message: Incoming) -> Reply:
        """处理一条消息。任何一条都不该让桥崩掉。"""
        gate = self._admit(message)
        if gate is not None:
            return gate
        self._notify_conversation = (
            message.conversation or self._notify_conversation
        )

        text = message.text.strip()
        if not text:
            return Reply(user=message.user, text="", accepted=False, reason="空消息")
        # 斜杠命令由桥自己处理（**在准入判定之后**：陌生人连 /help 都不该拿到）。
        command = handle_command(text, self.root)
        if command is not None:
            self._send(command, message.conversation)
            return Reply(user=message.user, text=command, accepted=True)
        if len(text) > self.max_chars:
            reply = (
                f"这条消息有 {len(text)} 字，超过上限 {self.max_chars} 字，"
                "没有交给 agent。请拆短一点再发。"
            )
            self._send(reply, message.conversation)
            return Reply(user=message.user, text=reply, accepted=False, reason="超长")

        try:
            answer = self.runner(text)
        except Exception as exc:  # noqa: BLE001 - 桥不该被 agent 的异常带崩
            answer = f"这一轮没跑起来：{type(exc).__name__}: {exc}"
            self._send(answer, message.conversation)
            return Reply(user=message.user, text=answer, accepted=False, reason="runner 异常")

        self._send(answer or "（没有产出）", message.conversation)
        return Reply(user=message.user, text=answer, accepted=True)

    def _admit(self, message: Incoming) -> Reply | None:
        """准入判定。返回 None 表示放行，否则返回一条"不放行"的回复。

        默认是**配对**：不认识的人拿码，主人批准之后才放行。原先的默认是
        "名单为空就谁都能用"，那在公网通道上是不可接受的。
        """
        user = message.user
        self._last_conversation = message.conversation or self._last_conversation
        if user in self.allowed_users:
            return None
        if self.access == OPEN:
            return None
        if self.access == ALLOWLIST or self.pairings is None:
            # 名单模式：名单外连码都不给（这是使用者的明确选择）。
            self._note(f"忽略未授权用户 {user}")
            return Reply(user=user, text="", accepted=False, reason="未授权")

        if self.pairings.is_approved(user):
            return None
        code = self.pairings.ensure_code(user)
        reply = PAIRING_REPLY.format(code=code)
        self._send(reply, message.conversation)
        self._note(f"有人要配对：{user}（码 {code}）")
        return Reply(user=user, text=reply, accepted=False, reason="待配对")

    def _send(self, text: str, conversation: str) -> None:
        """发一条消息出去。**发不出去不能把桥打死**。

        真踩过：QQ 那边回了一句 `code=11001 不支持的调用`，异常一路抛到顶层，
        整个桥进程**直接退出**——于是"手机上发了消息没人理"变成了永久状态，
        而现场只剩下一个崩栈。发失败是可能发生的事（平台没开通这个能力、被动
        回复窗口过期、消息太长……），它该是一条日志，不是一次自杀。

        真机再踩：查完天气 SOCKS 握手超时，结论已经有了但没发出去，用户一直
        等。网络抖动要留下补发；平台拒绝不要。
        """
        try:
            self.channel.send(text, conversation)
        except Exception as exc:  # noqa: BLE001 - 通道各异，这里只要"别死"
            self._note(f"回复发送失败（{type(exc).__name__}）：{exc}")
            if _transient_send(exc):
                self._keep_outbox(text, conversation)

    def flush_outbox(self) -> int:
        """把刚才没发出去的答复再试一遍。通了发几条算几条。"""
        if not self._outbox:
            return 0
        leftover: list[tuple[str, str]] = []
        sent = 0
        for text, dest in self._outbox:
            if leftover:
                leftover.append((text, dest))
                continue
            try:
                self.channel.send(text, dest)
            except Exception as exc:  # noqa: BLE001
                self._note(f"补发仍失败（{type(exc).__name__}）：{exc}")
                leftover.append((text, dest))
                continue
            sent += 1
            self._note("补发了一条刚才没发出去的回复")
        self._outbox = leftover
        return sent

    def _keep_outbox(self, text: str, conversation: str) -> None:
        self._outbox.append((text, conversation))
        overflow = len(self._outbox) - OUTBOX_MAX
        if overflow > 0:
            del self._outbox[:overflow]

    def run_once(self) -> list[Reply]:
        """把当前能取到的消息都处理一遍。"""
        replies: list[Reply] = []
        for message in self.channel.poll():
            try:
                replies.append(self.handle(message))
            except Exception as exc:  # noqa: BLE001 - 一条坏消息不该终止长驻进程
                self._note(f"处理消息失败（{type(exc).__name__}）：{exc}")
        return replies

    def notify_disconnects(self, rows: list[dict]) -> list[str]:
        """通→断时给最近聊天发一句。没开开关、没人说过话、冷却期内都不发。"""
        sent: list[str] = []
        dest = self._notify_conversation
        for row in self._watch.edges(rows):
            text = down_line(row)
            if not dest:
                self._note("断开了，但还没有最近聊天可通知")
                continue
            self._send(text, dest)
            sent.append(text)
        return sent
