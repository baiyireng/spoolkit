"""通道协议。

一个通道只要会两件事：**收**（把外面来的消息交出来）与**发**（把回复送出去）。
两者的方向容易写反，所以这里把名字定死：

- `send(text, to)`  —— 往通道里发（对外发消息）；
- `poll()`          —— 取外面进来的消息（没有就返回空列表）。

拉取式（poll）而不是回调式：回调要求通道在自己的线程里叫我们，
而"桥"这边是单线程顺序处理——把并发留给通道实现，桥这边保持简单。
长轮询（Telegram）与"回调攒进队列再被 poll 取走"（企业微信）都能落进来。

**关于个人微信/QQ**：它们没有官方接口。第三方 hook（itchat / wechaty /
NapCat 之流）违反服务条款、有封号风险，而且等于把 agent 的工具权限挂在
一个不设防的入口上。这个包只提供官方通道的适配器；想让手机上的消息驱动
agent，用企业微信（官方、免费、国内可用）或 Telegram。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Incoming:
    """一条进来的消息。`user` 是通道给的用户标识（白名单按它判）。"""

    user: str
    text: str
    # 通道自己的会话标识（Telegram 的 chat_id、企业微信的会话 ID）。
    # 回复要发回这里；为空时由通道自己决定默认发到哪。
    conversation: str = ""
    # 通道自带的元数据（原始报文等），调试时有用，桥本身不看。
    raw: str = ""


class Channel(Protocol):
    """通道协议。实现方至少要能收与发。"""

    name: str

    def send(self, text: str, to: str = "") -> None:
        """发一条消息出去。`to` 为空表示发到通道的默认目的地。"""
        ...

    def poll(self) -> list[Incoming]:
        """取走当前能拿到的消息。没有就返回空列表（不要阻塞）。"""
        ...

    def close(self) -> None:
        """关掉通道，释放资源。"""
        ...
