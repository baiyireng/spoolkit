"""代码搜索工具，基于 ripgrep。

未安装 rg 时返回失败结果并给出明确提示，不静默降级。
"""

import shutil
import re
import subprocess
from pathlib import Path

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_readable
from agents_dev.tools.types import ToolResult, ToolSpec
from agents_dev.tools.view import WorkspaceView

DEFAULT_MAX_RESULTS = 50
TIMEOUT_SECONDS = 20


def _search_code(root: Path, args: dict, pending=None, read_roots=()) -> ToolResult:
    pattern = args["pattern"]
    max_results = args.get("max_results", DEFAULT_MAX_RESULTS)
    if max_results < 1:
        return ToolResult(ok=False, content="max_results 必须大于等于 1")

    if shutil.which("rg") is None:
        return ToolResult(ok=False, content="未找到 rg（ripgrep），无法执行搜索")

    try:
        base = resolve_readable(root, args.get("path", "."), read_roots)
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

    # 待确认的改动不在磁盘上，rg 搜到的是旧内容。改过的文件整份改用
    # 「改之后」的内容重搜，否则模型会搜到自己刚删掉的东西。
    view = WorkspaceView(root, pending)
    if view.overridden:
        stale = set(view.overridden)
        merged = [line for line in cleaned if line.split(":", 1)[0] not in stale]
        merged.extend(_search_overlay(view, pattern, base, max_results))
        cleaned = merged[:max_results]
    return ToolResult(ok=True, content="\n".join(cleaned))


def _search_overlay(
    view: WorkspaceView, pattern: str, base: Path, max_results: int
) -> list[str]:
    """在待确认改动的内容里搜。只处理改动涉及的那几个文件。"""
    try:
        expression = re.compile(pattern)
    except re.error:
        return []  # 正则本身不合法时，rg 已经报过错了

    try:
        base_rel = view.relative(base)
    except ValueError:
        base_rel = "."

    def in_scope(path: str) -> bool:
        if base_rel in (".", ""):
            return True  # 全项目搜索
        if base.is_file():
            return path == base_rel
        return path.startswith(base_rel + "/")

    hits: list[str] = []
    for path in view.overridden:
        if not in_scope(path):
            continue
        text = view.overridden_text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if expression.search(line):
                hits.append(f"{path}:{number}:{line}")
                if len(hits) >= max_results:
                    return hits
    return hits


def search_code_spec(root: Path, pending=None, read_roots=()) -> ToolSpec:
    """构造代码搜索工具的规格。"""
    return ToolSpec(
        name="search_code",
        description="在项目内按正则搜索代码，返回 文件:行号:内容",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "在哪个目录或文件里搜"},
                "max_results": {"type": "integer", "description": "最多返回几条"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        handler=lambda args: _search_code(root, args, pending, read_roots),
        brief="正则搜代码",
        group="看",
    )

