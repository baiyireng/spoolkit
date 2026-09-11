"""文件系统工具。

所有路径都经 resolve_within 处理，模型无法逃出项目根目录。
工具失败一律返回 ok=False，不抛异常。
"""

from pathlib import Path

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_within
from agents_dev.tools.types import ToolResult, ToolSpec


def _read_file(root: Path, args: dict) -> ToolResult:
    try:
        target = resolve_within(root, args["path"])
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    if not target.exists():
        return ToolResult(ok=False, content=f"文件不存在: {args['path']}")
    if target.is_dir():
        return ToolResult(ok=False, content=f"目标是目录而非文件: {args['path']}")

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult(ok=False, content=f"文件不是 UTF-8 文本: {args['path']}")

    start = args.get("start_line")
    end = args.get("end_line")
    if start is None and end is None:
        return ToolResult(ok=True, content=text)

    start = 1 if start is None else start
    if start < 1:
        return ToolResult(ok=False, content="start_line 必须大于等于 1")

    lines = text.splitlines()
    end = len(lines) if end is None else end
    if end < start:
        return ToolResult(ok=False, content="end_line 不能小于 start_line")

    chunk = lines[start - 1 : end]
    numbered = "\n".join(f"{start + i}\t{line}" for i, line in enumerate(chunk))
    return ToolResult(ok=True, content=numbered)


def _list_dir(root: Path, args: dict) -> ToolResult:
    try:
        target = resolve_within(root, args["path"])
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    if not target.exists():
        return ToolResult(ok=False, content=f"目录不存在: {args['path']}")
    if not target.is_dir():
        return ToolResult(ok=False, content=f"目标不是目录: {args['path']}")

    entries = [f"{c.name}/" if c.is_dir() else c.name for c in sorted(target.iterdir())]
    return ToolResult(ok=True, content="\n".join(entries))


def read_file_spec(root: Path) -> ToolSpec:
    """构造读文件工具的规格。"""
    return ToolSpec(
        name="read_file",
        description="读取项目内文件；可用 start_line/end_line 只取需要的行区间",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args: _read_file(root, args),
    )


def list_dir_spec(root: Path) -> ToolSpec:
    """构造列目录工具的规格。"""
    return ToolSpec(
        name="list_dir",
        description="列出项目内某个目录的直接子项",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args: _list_dir(root, args),
    )

