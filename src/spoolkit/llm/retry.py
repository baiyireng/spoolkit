"""输出被截断时的处理。

结构化输出的截断是一种静默失败：JSON 被拦腰切断后解析必然失败，
而错误信息只会说「不是合法 JSON」，很难定位到真正的原因是预算不够。

处理顺序是：显式检测 → 翻倍预算重试。至于继续烧预算还是降级范围，
由调用方决定——归纳会话在仍然截断时会把条目数降到 1，
因为「少提炼几条」远好过「一条都没有」。

刻意不做「续接输出」：请求级续接会丢失服务端的语法约束状态，
模型多半会从头重写，产出的东西即使能解析也未必接得上语义。
"""

from dataclasses import replace

from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.types import ChatRequest, ChatResponse

DEFAULT_ATTEMPTS = 2
DEFAULT_MULTIPLIER = 2.0


def chat_with_escalation(
    gateway: ModelGateway,
    request: ChatRequest,
    attempts: int = DEFAULT_ATTEMPTS,
    multiplier: float = DEFAULT_MULTIPLIER,
) -> ChatResponse:
    """调用模型；若输出被截断则放大预算重试。"""
    response = gateway.chat(request)
    for _ in range(max(0, attempts - 1)):
        if not response.truncated:
            return response
        request = replace(
            request, max_tokens=max(1, int(request.max_tokens * multiplier))
        )
        response = gateway.chat(request)
    return response

