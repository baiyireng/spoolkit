"""项目内共用的异常类型。"""


class AgentError(Exception):
    """本项目所有自定义异常的基类。"""


class PathOutsideProjectError(AgentError):
    """请求的路径落在项目根目录之外。"""


class ToolArgumentError(AgentError):
    """工具调用参数不符合其 JSON Schema。"""


class ContextOverflowError(AgentError):
    """提示词超过服务端能容纳的上下文。

    它和「输出被截断」是两件事：截断是生成没写完，这个是请求根本没进去。
    单独成类是因为处置完全不同——后者只能把提示词改小再发一次，
    而且它必须能被接住：接不住就是一次运行直接崩掉。
    """

