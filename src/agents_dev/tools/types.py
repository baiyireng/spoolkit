"""工具层数据类型。"""

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Fact:
    """一条可核对的事实：某个对象的值。

    工具把「谁是多少」结构性地交出来，核对器才有办法判断一处引用是不是
    配错了对象。光给一段渲染好的文本，核对只能做到「这个数有没有出现过」
    ——而实测里有一半的错误恰恰是「数出现过，但配到了别的对象上」。
    """

    subject: str
    value: float
    unit: str


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。失败也是正常返回值，不通过异常表达。"""

    ok: bool
    content: str
    # 可选的结构化事实。给核对用，不给模型看（模型看 content 就够了）。
    facts: tuple[Fact, ...] = ()


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整定义。parameters 为 JSON Schema 子集。

    `brief` 与 `group` 是给**索引**用的：每轮提示词里常驻的只有「名字 +
    参数名 + 一句干什么」，完整说明（`description`）放在 `tool_help` 后面
    按需取。这两个字段为空时会退化——brief 取描述的第一句，group 归到
    「其它」——退化得难看，但比让一个工具在索引里变成光秃秃的名字好。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult] = field(repr=False)
    brief: str = ""
    group: str = "其它"


@dataclass(frozen=True)
class ToolCall:
    """模型发出的一次工具调用请求。"""

    name: str
    arguments: dict[str, Any]

