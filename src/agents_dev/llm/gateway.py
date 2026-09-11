"""模型网关协议。"""

from typing import Protocol

from agents_dev.llm.types import ChatRequest, ChatResponse


class ModelGateway(Protocol):
    """所有模型供应商必须满足的接口。

    真实实现走 llama.cpp HTTP；测试与离线开发使用 FakeModel。
    上层代码只依赖本协议，不依赖具体实现。
    """

    def chat(self, request: ChatRequest) -> ChatResponse:
        """发送请求并返回完整响应。"""
        ...

