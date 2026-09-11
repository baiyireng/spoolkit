"""把索引能力包装成工具。

模型用这些工具主动深挖：先看符号表，再决定要不要取源码。
这比「把整个文件读进来」精确得多，是省 token 的主要来源。
"""

import sqlite3
from pathlib import Path

from agents_dev.index.repo_map import load_symbol_source, render_file_symbols
from agents_dev.index.repo_map import render_neighborhood
from agents_dev.index.graph import impact
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.edit import looks_like_path
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
        # 实测模型会拿文件名当符号名来查，然后收到一句「找不到符号」就卡住，
        # 连着三次。它想要的是「这个文件里有什么」，那是另一个工具。
        if looks_like_path(name):
            return ToolResult(
                ok=False,
                content=(
                    f"没有叫 {name} 的符号——这看起来是文件路径。"
                    "要看某个文件里有哪些符号，用 file_symbols(path)。"
                ),
            )
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
                "name": {"type": "string", "description": "符号名，不是文件名"},
                "path": {"type": "string", "description": "限定在哪个文件里找"},
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
            "properties": {
                "path": {"type": "string", "description": "文件路径"}
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args: _file_symbols(conn, args),
    )


def _find_callers(
    conn: sqlite3.Connection, args: dict, counter: OfflineTokenCounter
) -> ToolResult:
    name = args["name"]
    path = args.get("path")
    sql = (
        "SELECT s.id AS id, f.path AS path FROM symbol s"
        " JOIN file f ON f.id = s.file_id WHERE s.name = ?"
    )
    params: list = [name]
    if path:
        sql += " AND f.path = ?"
        params.append(path)
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        return ToolResult(ok=False, content=f"找不到符号: {name}")
    if len(rows) > 1:
        places = "、".join(f"{r['path']}" for r in rows[:MAX_MATCHES])
        return ToolResult(
            ok=False,
            content=f"{name} 有 {len(rows)} 个同名符号（{places}），请用 path 指定",
        )

    symbol_id = rows[0]["id"]
    depth = args.get("depth", 2)
    if depth < 1:
        return ToolResult(ok=False, content="depth 必须大于等于 1")

    parts = [
        render_neighborhood(conn, symbol_id, counter, SYMBOL_LIST_BUDGET)
    ]
    affected, truncated, _ = impact(conn, symbol_id, depth=depth)
    if affected:
        listed = "、".join(f"{item.name}({item.path}:{item.start_line})" for item in affected)
        parts.append(f"改动它会波及 {len(affected)} 个符号：{listed}")
        if truncated:
            parts.append("波及面超出上限，以上只列出一部分。")
    else:
        parts.append("没有发现会被它波及的符号。")
    return ToolResult(ok=True, content="\n".join(parts))


def find_callers_spec(conn: sqlite3.Connection) -> ToolSpec:
    """查看谁引用了某符号，以及改动它会波及什么。"""
    return ToolSpec(
        name="find_callers",
        description="查看谁引用了某符号以及改动波及面；同名多个时需用 path 指定",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "符号名，不是文件名"},
                "path": {"type": "string", "description": "同名多个时用它指定"},
                "depth": {"type": "integer", "description": "波及面看几层，默认 2"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=lambda args: _find_callers(conn, args, OfflineTokenCounter()),
    )
