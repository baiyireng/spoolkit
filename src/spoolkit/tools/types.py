"""工具层数据类型。"""

from dataclasses import dataclass, field
from typing import Any, Callable

# 出路话术的标记行。循环硬拦「原样重试」时把这一行原样端回给模型——
# 复用既有的工具输出通道（trace / --events 的 tool 事件），不新造一套事件。
#
# 放在这一层（`types`）而不是 `tools/edit.py`：写下这一行的有两个地方
# ——编辑工具的拒绝话术，和**参数校验**的拒绝话术（`tools/registry.py`）。
# 后者要是从 `edit` 里 import，工具注册表就为了一个字符串常量拖上整个
# 编辑模块；而两处各抄一份字面量，改一处漏一处时症状是「硬拦时那一行
# 端不回去」，最难查。所以常量只有这一份，`edit.py` 从这儿取。
NEXT_STEP_MARKER = "照做的下一步："


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
class Usage:
    """一类工具自己花掉的模型开销。

    多数工具不花模型的钱；花的那几个（派发、诊断回流）必须**报回来**——
    主循环的用量表是调这块时唯一的账本，漏掉子智能体那一整段，
    报出来的数字就不是「这次任务花了多少」，而是「主循环自己花了多少」。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。失败也是正常返回值，不通过异常表达。"""

    ok: bool
    content: str
    # 可选的结构化事实。给核对用，不给模型看（模型看 content 就够了）。
    facts: tuple[Fact, ...] = ()
    # 这个工具自己花掉的模型开销，由循环累加进用量。
    usage: Usage | None = None
    # 判据用的**原始**证据，不给模型看。
    #
    # 为什么需要它：自动验证回灌给模型的是压缩过的短摘要
    # （`verify.condense_test_output` 只留 `ERROR tests/x.py` 那一行，
    # 真正的 `ImportError: cannot import name 'get_rows'` 被丢掉了）。
    # 而自动审查层判「这次失败是不是改名没铺完造成的」正需要那个名字——
    # 拿短摘要去判会漏，改成把整个原始输出灌给模型又太贵。所以两份分开：
    # 模型看 content，审查层看 raw。
    raw: str = ""


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
    """模型发出的一次工具调用请求。

    `intent` 是**这次调用想干什么**，来自模型自己写的 `why`（读不到时退回
    这一轮的 `thought`，见 protocol._parse_tool_calls）。它不参与任何工具
    参数校验，也不改变执行——它的用处只有两个：① 让人与督导看得到「它
    为什么按这个按钮」，从而分得清「工具不完善」还是「它理解错了」；
    ② 让失败与拒绝的话术能**回应意图**，而不是只给一条通用替代方案。

    默认空串：老检查点与直接构造 ToolCall 的调用方（测试、MCP）都不受影响。
    """

    name: str
    arguments: dict[str, Any]
    intent: str = ""

