"""工具层数据类型。"""

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。失败也是正常返回值，不通过异常表达。"""

    ok: bool
    content: str


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整定义。parameters 为 JSON Schema 子集。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult] = field(repr=False)


@dataclass(frozen=True)
class ToolCall:
    """模型发出的一次工具调用请求。"""

    name: str
    arguments: dict[str, Any]

