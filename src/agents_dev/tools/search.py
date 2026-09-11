"""代码搜索工具，基于 ripgrep。

未安装 rg 时返回失败结果并给出明确提示，不静默降级。
"""

import shutil
import subprocess
from pathlib import Path

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_within
from agents_dev.tools.types import ToolResult, ToolSpec

DEFAULT_MAX_RESULTS = 50
TIMEOUT_SECONDS = 20


def _search_code(root: Path, args: dict) -> ToolResult:
    pattern = args["pattern"]
    max_results = args.get("max_results", DEFAULT_MAX_RESULTS)
    if max_results < 1:
        return ToolResult(ok=False, content="max_results 必须大于等于 1")

    if shutil.which("rg") is None:
        return ToolResult(ok=False, content="未找到 rg（ripgrep），无法执行搜索")

    try:
        base = resolve_within(root, args.get("path", "."))
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    proc = subprocess.run(
        [
            "rg",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            str(max_results),
            "--",
            pattern,
            str(base),
        ],
        capture_output=True,
        text=True,
        # 必须显式指定 UTF-8。默认走 Windows 本地编码（GBK），
        # 源码里一旦出现超出 GBK 的字符，解码失败会让 stdout 变成 None，
        # 报错信息却是 "NoneType has no attribute splitlines"——离真正原因很远。
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        cwd=str(root),
    )

    if proc.returncode not in (0, 1):
        return ToolResult(ok=False, content=f"搜索失败: {proc.stderr.strip()}")

    root_prefix = str(root)
    lines = proc.stdout.splitlines()[:max_results]
    cleaned = [
        line.replace(root_prefix + "\\", "").replace(root_prefix + "/", "")
        for line in lines
    ]
    return ToolResult(ok=True, content="\n".join(cleaned))


def search_code_spec(root: Path) -> ToolSpec:
    """构造代码搜索工具的规格。"""
    return ToolSpec(
        name="search_code",
        description="在项目内按正则搜索代码，返回 文件:行号:内容",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        handler=lambda args: _search_code(root, args),
    )

