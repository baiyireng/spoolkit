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
    grammar: str | None = None


@dataclass(frozen=True)
class ChatResponse:
    """一次模型响应，携带用量统计用于预算核算。"""

    text: str
    prompt_tokens: int
    completion_tokens: int

