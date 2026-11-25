"""当前这次工具调用的稳定 ID。

工具规格和 handler 参数里不放 call_id。循环在执行周围设上，
待确认清单和命令授权从这里读。离开这次调用必须清掉，避免串到下一次。
"""

from contextlib import contextmanager
from contextvars import ContextVar

_tool_call_id: ContextVar[str] = ContextVar("tool_call_id", default="")


def current_tool_call_id() -> str:
    """这次工具调用的 ID。没设时是空串，不当成上一次的调用。"""
    return _tool_call_id.get()


@contextmanager
def tool_call_context(call_id: str):
    """设置当前 tool_call_id，离开时恢复进入前的值。"""
    token = _tool_call_id.set(call_id or "")
    try:
        yield
    finally:
        _tool_call_id.reset(token)
