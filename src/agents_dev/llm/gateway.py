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

    def context_window(self) -> int | None:
        """查询该模型实际可用的上下文长度。

        这个值不该由使用者猜：同一个配置文件下，换模型或换 KV cache 量化，
        窗口大小都会变。查不到时返回 None，由上层退回默认值。
        """
        ...


class CountingGateway:
    """把网关包一层，数清某一段工作花了多少。

    只拦 chat：其余属性（context_window / token_counter）原样透传，
    因为被包的代码也需要它们。

    它存在的理由和「时间拆成模型/工具两笔」一样：**账算不清就调不动**。
    拆解那一段原先只有墙钟能看，于是「拆解为什么慢」只能靠猜。
    """

    def __init__(self, inner: ModelGateway) -> None:
        self._inner = inner
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(self, request: ChatRequest) -> ChatResponse:
        response = self._inner.chat(request)
        self.calls += 1
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        return response

    def __getattr__(self, name):
        return getattr(self._inner, name)

