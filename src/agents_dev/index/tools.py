"""把索引能力包装成工具。

模型用这些工具主动深挖：先看符号表，再决定要不要取源码。
这比「把整个文件读进来」精确得多，是省 token 的主要来源。
"""

import sqlite3
from pathlib import Path

from agents_dev.index.repo_map import load_symbol_source, render_file_symbols
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.types import ToolResult, ToolSpec

SYMBOL_LIST_BUDGET = 600
MAX_MATCHES = 20


def _find_symbol(root: Path, conn: sqlite3.Connection, args: dict) -> ToolResult:
    name = args["name"]
    path = args.get("path")

    if path:
        source = load_symbol_source(root, conn, path, name)
        if source is None:
            return ToolResult(ok=False, content=f"在 {path} 中找不到符号: {name}")
        return ToolResult(ok=True, content=source)

    rows = conn.execute(
        "SELECT f.path AS path, s.start_line AS start_line, s.end_line AS end_line,"
        " s.signature AS signature"
        " FROM symbol s JOIN file f ON f.id = s.file_id"
        " WHERE s.name = ? ORDER BY f.path, s.start_line LIMIT ?",
        (name, MAX_MATCHES),
    ).fetchall()
    if not rows:
        return ToolResult(ok=False, content=f"找不到符号: {name}")

    lines = [
        f"{row['path']}:{row['start_line']} L{row['start_line']}-{row['end_line']}"
        f" {row['signature']}"
        for row in rows
    ]
    return ToolResult(ok=True, content="\n".join(lines))


def _file_symbols(conn: sqlite3.Connection, args: dict) -> ToolResult:
    text = render_file_symbols(
        conn, args["path"], OfflineTokenCounter(), SYMBOL_LIST_BUDGET
    )
    if not text:
        return ToolResult(ok=False, content=f"索引中没有该文件: {args['path']}")
    return ToolResult(ok=True, content=text)


def find_symbol_spec(root: Path, conn: sqlite3.Connection) -> ToolSpec:
    """查符号：只给名字则列出所有匹配位置，给出 path 则返回该符号源码。"""
    return ToolSpec(
        name="find_symbol",
        description="按名字查找符号；给出 path 时直接返回该符号的源码",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=lambda args: _find_symbol(root, conn, args),
    )


def file_symbols_spec(conn: sqlite3.Connection) -> ToolSpec:
    """列出某文件的全部符号签名，不含函数体。"""
    return ToolSpec(
        name="file_symbols",
        description="列出某文件里全部符号的签名与行号，不含函数体",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args: _file_symbols(conn, args),
    )

