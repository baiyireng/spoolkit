"""工具索引与按需展开。

每轮提示词里常驻的只有索引（名字 + 参数名 + 一句干什么），完整说明走
`tool_help`。这个文件盯的是三件事：索引里**参数名必须在**（模型靠它写调用）、
展开必须给得出参数含义、以及子智能体只能查到它自己那一套。
"""

from pathlib import Path

from spoolkit.agents.runtime import REVIEWER, restrict
from spoolkit.tools.help import tool_help_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall, ToolResult, ToolSpec


def _tool(name: str, description: str, brief: str = "", group: str = "其它", **params):
    properties = {key: {"type": "string"} for key in params}
    return ToolSpec(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": list(params)[:1],
            "additionalProperties": False,
        },
        handler=lambda args: ToolResult(ok=True, content="ok"),
        brief=brief,
        group=group,
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        _tool(
            "read_file",
            "读取项目内文件；可用 start_line/end_line 只取需要的行区间",
            brief="读文件",
            group="看",
            path=None,
        )
    )
    registry.register(
        _tool("write_file", "新建或整体覆盖一个文件", brief="整份写文件", group="改",
              path=None, content=None)
    )
    registry.register(tool_help_spec(registry))
    return registry


def test_索引里带参数名和一句干什么() -> None:
    """参数名不能省：语法约束给的是「所有工具参数的并集」，它不说谁属于谁。"""
    index = _registry().describe()
    assert "read_file(path)" in index
    assert "读文件" in index
    # 完整描述不常驻——那正是省下来的东西
    assert "start_line/end_line 只取需要的行区间" not in index


def test_索引按类别分组且顺序稳定() -> None:
    index = _registry().describe()
    assert index.index("- 看：") < index.index("- 改：") < index.index("- 其它：")


def test_没有brief时退回描述的第一句() -> None:
    """宁可多花几个字，也不要让一个工具在索引里变成光秃秃的名字。"""
    registry = ToolRegistry()
    registry.register(_tool("mystery", "做一件很复杂的事：细节一；细节二", path=None))
    assert "做一件很复杂的事" in registry.describe()
    assert "细节二" not in registry.describe()


def test_展开给出参数含义与必填标记() -> None:
    registry = ToolRegistry()
    spec = _tool("read_file", "读取项目内文件", brief="读文件", group="看", path=None)
    # 参数带用途
    spec = ToolSpec(
        name=spec.name,
        description=spec.description,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要读的文件"},
                "start_line": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=spec.handler,
        brief=spec.brief,
        group=spec.group,
    )
    registry.register(spec)
    text = registry.explain("read_file")
    assert "读取项目内文件" in text
    assert "path（要读的文件）（必填）" in text
    assert "start_line（选填）" in text


def test_查一个工具() -> None:
    registry = _registry()
    result = registry.invoke(ToolCall("tool_help", {"name": "write_file"}))
    assert result.ok
    assert "新建或整体覆盖一个文件" in result.content


def test_查一类() -> None:
    registry = _registry()
    result = registry.invoke(ToolCall("tool_help", {"name": "看"}))
    assert result.ok
    assert "read_file" in result.content
    # 这一类里没有的工具不该出现
    assert "write_file" not in result.content


def test_查全部() -> None:
    registry = _registry()
    result = registry.invoke(ToolCall("tool_help", {"name": "*"}))
    assert result.ok
    assert "read_file" in result.content and "write_file" in result.content


def test_查不到时说清有哪些() -> None:
    """只说「找不到」，模型唯一能做的就是猜——它会连着猜几次，把预算烧光。"""
    registry = _registry()
    result = registry.invoke(ToolCall("tool_help", {"name": "没这个东西"}))
    assert not result.ok
    assert "read_file" in result.content


def test_子智能体查不到自己没有的工具() -> None:
    """审查者没有写权限。给它列出写工具的说明，等于告诉它一件它做不到的事。"""
    full = _registry()
    limited = restrict(full, REVIEWER)
    assert limited.get("write_file") is None
    result = limited.invoke(ToolCall("tool_help", {"name": "*"}))
    assert result.ok
    assert "write_file" not in result.content
    assert limited.get("tool_help") is not None


def test_索引比全文小得多() -> None:
    """防止哪天有人把完整说明又写回索引——那是这次改动的全部意义。

    实测的账在 benchmarking.md：十五个工具的整份说明 665 token，索引 210，
    而每个请求都要重述一遍。
    """
    registry = _registry()
    index = registry.describe()
    full = "\n".join(registry.explain(name) for name in registry.names())
    assert len(index) * 2 < len(full)
