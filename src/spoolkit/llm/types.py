"""模型交互的基础数据类型。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    """一条对话消息。role 取 system / user / assistant / tool。"""

    role: str
    content: str
    name: str | None = None


@dataclass(frozen=True)
class ChatRequest:
    """一次模型请求。grammar 非空时表示要求语法约束解码。"""

    messages: tuple[Message, ...]
    max_tokens: int
    response_schema: dict | None = None
    # 为真时要求服务端复用相同 token 前缀的 KV。默认关闭，但请求体里总会带上这个字段。
    cache_prompt: bool = False


@dataclass(frozen=True)
class ChatResponse:
    """一次模型响应。

    truncated 表示服务端因为输出预算耗尽而截断了内容。这个信息必须显式
    带出来：被截断的结构化输出解析后通常表现为「格式错误」，如果只看
    解析结果，就会把「预算不够」误判成「模型不会用格式」，从而修错地方。
    """

    text: str
    prompt_tokens: int
    completion_tokens: int
    truncated: bool = False

