"""token 计数。

真实实现应向 llama.cpp 服务端的 /tokenize 端点查询以获得精确值；
离线实现用于测试与无 GPU 环境，采用保守估算。
两种实现都满足 TokenCounter 协议，上层无需区分。
"""

from typing import Protocol


class TokenCounter(Protocol):
    """token 计数接口。"""

    def count(self, text: str) -> int:
        """返回 text 的 token 数估算或精确值。"""
        ...


class OfflineTokenCounter:
    """不依赖模型的估算实现。

    规则：ASCII 字符按 4 字符 1 token，非 ASCII 按 1 字符 1 token。
    刻意偏保守（宁多不少），以免低估导致上下文溢出。
    """

    def count(self, text: str) -> int:
        if not text:
            return 0
        ascii_chars = sum(1 for ch in text if ord(ch) < 128)
        wide_chars = len(text) - ascii_chars
        return max(1, -(-ascii_chars // 4) + wide_chars)

