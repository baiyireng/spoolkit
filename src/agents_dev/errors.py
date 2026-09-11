"""项目内共用的异常类型。"""


class AgentError(Exception):
    """本项目所有自定义异常的基类。"""


class PathOutsideProjectError(AgentError):
    """请求的路径落在项目根目录之外。"""


class ToolArgumentError(AgentError):
    """工具调用参数不符合其 JSON Schema。"""

