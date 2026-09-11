from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.types import ToolCall, ToolResult, ToolSpec

SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="read_file",
            description="读取文件",
            parameters=SCHEMA,
            handler=lambda args: ToolResult(ok=True, content=f"读到 {args['path']}"),
        )
    )
    return reg


def test_注册后可按名查询() -> None:
    reg = _registry()
    assert reg.get("read_file") is not None
    assert reg.names() == ("read_file",)


def test_正常调用返回成功() -> None:
    result = _registry().invoke(ToolCall(name="read_file", arguments={"path": "a.py"}))
    assert result.ok is True
    assert result.content == "读到 a.py"


def test_未知工具返回失败而非抛错() -> None:
    result = _registry().invoke(ToolCall(name="nope", arguments={}))
    assert result.ok is False
    assert "nope" in result.content


def test_缺少必填参数被拦截() -> None:
    result = _registry().invoke(ToolCall(name="read_file", arguments={}))
    assert result.ok is False
    assert "path" in result.content


def test_参数类型错误被拦截() -> None:
    result = _registry().invoke(ToolCall(name="read_file", arguments={"path": 123}))
    assert result.ok is False


def test_未知参数被拒绝() -> None:
    result = _registry().invoke(
        ToolCall(name="read_file", arguments={"path": "a.py", "x": 1})
    )
    assert result.ok is False


def test_未知参数的报错要带上可用参数() -> None:
    """只说「不认识 x」，模型只能猜；说了可用参数，它下一轮就能改对。"""
    result = _registry().invoke(
        ToolCall(name="read_file", arguments={"path": "a.py", "timeout": 5})
    )
    assert result.ok is False
    assert "timeout" in result.content
    assert "可用参数" in result.content
    assert "path" in result.content


def test_报错要说清每个参数是干什么的() -> None:
    """工具之间参数名有重叠，只报名字模型仍然对不上号。

    实测它把 search_code 的 pattern 搬到 find_symbol 上，连撞两次。
    """
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="t",
            description="测试用",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "符号名，不是文件名"},
                    "path": {"type": "string", "description": "限定在哪个文件里找"},
                },
                "additionalProperties": False,
            },
            handler=lambda args: ToolResult(ok=True, content=""),
        )
    )
    result = reg.invoke(ToolCall(name="t", arguments={"name": "x", "pattern": "a.py"}))
    assert result.ok is False
    assert "pattern" in result.content
    assert "符号名，不是文件名" in result.content
    assert "限定在哪个文件里找" in result.content


def test_处理器抛异常被转为失败结果() -> None:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="boom",
            description="会炸",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: (_ for _ in ()).throw(RuntimeError("炸了")),
        )
    )
    result = reg.invoke(ToolCall(name="boom", arguments={}))
    assert result.ok is False
    assert "炸了" in result.content


def test_描述文本包含工具名与参数名() -> None:
    text = _registry().describe()
    assert "read_file" in text
    assert "path" in text

