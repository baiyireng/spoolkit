"""消息通道桥：让 agent 能被聊天通道驱动。

用途：在手机上给 agent 发一条消息 → 它在工作区里跑一轮 → 把结果发回来。

为什么是"桥 + 适配器"而不是直接接某一家的 API：**通道各有各的形状**
（企业微信是回调推送、Telegram 是长轮询、Slack 是 socket mode），
而"收消息 → 交给 agent → 回消息"这件事只有一套。所以中间这层固定，
两端各自适配。

一条硬规矩写在 `channel.py` 的注释里、也在文档里说清了：
**个人微信/QQ 没有官方接口**，第三方 hook 违反服务条款、有封号风险，
还把 agent 的工具权限挂到了聊天入口上。本包只做官方通道。
"""

from agents_dev.bridge.channel import Channel, Incoming
from agents_dev.bridge.core import Bridge, Reply
from agents_dev.bridge.fake import FakeChannel

# 官方通道适配器按需导入：它们的依赖（httpx 之外什么都没有）虽然轻，
# 但没配通道的人不需要为它们付出导入成本，也不该因为某个平台库没装而 import 失败。
__all__ = ["Bridge", "Channel", "FakeChannel", "Incoming", "Reply"]
