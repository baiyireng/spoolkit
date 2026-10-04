from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall, ToolResult, ToolSpec

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


# ---- 拒绝话术要能照抄（用户原话：「工具不支持某个参数，但 agent 却一直尝试」）----
#
# 现场：`merge-html` 在 t=0.4 那次里让 `search_code` 带着不支持的参数 `source`
# 连发 3 次，被拦 2 次之后放弃，pricing.html 的整份导航留在原地（判据红）。
# 只说「有个参数不认识」，模型唯一能做的就是猜——所以拒绝话术必须一次给全三样：
# 错在哪、这个工具支持哪些参数（名字 + 含义）、以及一个**能整段照抄**的正确形状。


SEARCH_CODE_LIKE = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "搜索用的正则"},
        "path": {"type": "string", "description": "在哪个子目录里找"},
        "max_results": {"type": "integer", "description": "最多返回几条"},
    },
    "required": ["pattern"],
    "additionalProperties": False,
}


def _search_code_like() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="search_code",
            description="按正则在代码里搜",
            parameters=SEARCH_CODE_LIKE,
            handler=lambda args: ToolResult(ok=True, content=""),
        )
    )
    return reg


def _shape_line(text: str) -> str:
    """拒绝话术里那一行「照做的下一步」。"""
    from spoolkit.tools.types import NEXT_STEP_MARKER

    lines = [line for line in text.splitlines() if line.startswith(NEXT_STEP_MARKER)]
    assert len(lines) == 1, f"照抄形状必须恰好一行：{text}"
    return lines[0]


def test_未知参数的拒绝话术给出支持清单与可照抄形状() -> None:
    """`search_code(pattern=…, source=…)` → 端回一个能直接发出去的形状。"""
    result = _search_code_like().invoke(
        ToolCall(
            name="search_code",
            arguments={"pattern": "pricing", "source": "pricing.html"},
        )
    )
    assert result.ok is False
    # ① 错在哪：点名那个不支持的参数。
    assert "source" in result.content
    # ② 支持哪些参数：键名 + 含义，一个都不能少（工具之间参数名有重叠）。
    assert "可用参数" in result.content
    assert "pattern（搜索用的正则）" in result.content
    assert "path（在哪个子目录里找）" in result.content
    assert "max_results（最多返回几条）" in result.content
    # ③ 可照抄的形状：工具名 + 留下的合法值，不含那个不支持的参数。
    shape = _shape_line(result.content)
    assert 'search_code(pattern="pricing")' in shape
    assert "source" not in shape


def test_缺必填的拒绝话术把缺的那个补成占位符() -> None:
    """缺哪个补哪个，一眼看得出还差什么；这次给对的值原样留着。"""
    result = _search_code_like().invoke(
        ToolCall(name="search_code", arguments={"path": "src"})
    )
    assert result.ok is False
    assert "缺少必填参数: pattern" in result.content
    shape = _shape_line(result.content)
    assert 'search_code(pattern="…", path="src")' in shape


def test_类型不对的拒绝话术换掉那个参数而不是整段重来() -> None:
    result = _search_code_like().invoke(
        ToolCall(name="search_code", arguments={"pattern": 5, "path": "src"})
    )
    assert result.ok is False
    assert "pattern" in result.content and "string" in result.content
    shape = _shape_line(result.content)
    assert 'search_code(pattern="…", path="src")' in shape
    assert "5" not in shape


def test_取值越界的拒绝话术列出可选值() -> None:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="pick",
            description="选一个",
            parameters={
                "type": "object",
                "properties": {"mode": {"type": "string", "enum": ["fast", "full"]}},
                "required": ["mode"],
                "additionalProperties": False,
            },
            handler=lambda args: ToolResult(ok=True, content=""),
        )
    )
    result = reg.invoke(ToolCall(name="pick", arguments={"mode": "quick"}))
    assert result.ok is False
    assert "fast" in result.content and "full" in result.content
    assert 'pick(mode="fast")' in _shape_line(result.content)


def test_照抄形状的行首就是硬拦认的那个标记() -> None:
    """重提被硬拦时，`_retry_block_message` 端回去的正是这一行。

    标记只有一份（`tools/types.NEXT_STEP_MARKER`），`tools/edit.py` 是转出——
    两处各抄一份字面量时，改一处漏一处的症状是「被拦之后什么都没端回来」。
    """
    from spoolkit.tools.edit import NEXT_STEP_MARKER as from_edit
    from spoolkit.tools.types import NEXT_STEP_MARKER as from_types

    assert from_edit == from_types
    result = _search_code_like().invoke(
        ToolCall(name="search_code", arguments={"pattern": "x", "source": "y"})
    )
    assert _shape_line(result.content).startswith(from_edit)

