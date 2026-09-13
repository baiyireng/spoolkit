"""内存通道：本地可验的那一端。

存在的理由和 FakeModel 一样：外部通道要凭据、要联网、还可能因为服务条款
不可控——而"桥"这套逻辑必须能在本机完整验一遍。`FakeChannel` 就是那根
可以随时拧的线。
"""

from __future__ import annotations

from spoolkit.bridge.channel import Incoming


class FakeChannel:
    """内存通道：`push()` 模拟收到消息，`sent` 记录发出去的消息。"""

    name = "fake"

    def __init__(self, default_to: str = "chat") -> None:
        self._inbox: list[Incoming] = []
        self.sent: list[tuple[str, str]] = []
        self._default_to = default_to
        self.closed = False

    def push(self, user: str, text: str, conversation: str = "") -> None:
        """模拟"外面来了一条消息"。"""
        self._inbox.append(
            Incoming(user=user, text=text, conversation=conversation or self._default_to)
        )

    def send(self, text: str, to: str = "") -> None:
        if self.closed:
            raise RuntimeError("通道已关闭")
        self.sent.append((to or self._default_to, text))

    def poll(self) -> list[Incoming]:
        if self.closed:
            raise RuntimeError("通道已关闭")
        messages, self._inbox = self._inbox, []
        return messages

    def close(self) -> None:
        self.closed = True
        self._inbox.clear()

    @property
    def last(self) -> str:
        """最后一条发出去的内容（测试里最常用的一句）。"""
        return self.sent[-1][1] if self.sent else ""
