"""把索引能力包装成工具。

模型用这些工具主动深挖：先看符号表，再决定要不要取源码。
这比「把整个文件读进来」精确得多，是省 token 的主要来源。
"""

import sqlite3
from pathlib import Path

from agents_dev.index.repo_map import load_symbol_source, render_file_symbols
from agents_dev.index.repo_map import render_neighborhood
from agents_dev.index.graph import impact
from agents_dev.index.refs import extract_refs
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.edit import looks_like_path
from agents_dev.tools.types import ToolResult, ToolSpec
from agents_dev.tools.view import WorkspaceView
from agents_dev.index.symbols import PythonAstExtractor

SYMBOL_LIST_BUDGET = 600
MAX_MATCHES = 20


def _pending_symbols(view: WorkspaceView, name: str):
    """在待确认改动的文本里找符号。

    索引是按磁盘内容建的，改动还没落盘，所以新加的函数在索引里查不到——
    实测审查者因此报「找不到符号: clean_text」，进而判定改动没做。
    这里用同一套 AST 提取器扫一遍改动内容，把结果补进去。
    """
    if view is None:
        return []
    engine = PythonAstExtractor()
    found = []
    for path in view.overridden:
        if not path.endswith(".py"):
            continue
        text = view.overridden_text(path)
        if not text:
            continue
        try:
            symbols = engine.extract(text, path)
        except SyntaxError:
            continue
        for sym in symbols:
            if sym.name == name:
                found.append(
                    f"{path}:{sym.start_line} L{sym.start_line}-{sym.end_line}"
                    f" {sym.signature}"
                )
    return found


def _find_symbol(root: Path, conn: sqlite3.Connection, args: dict, view=None) -> ToolResult:
    name = args["name"]
    path = args.get("path")

    if path:
        source = load_symbol_source(root, conn, path, name)
        if source is None and view is not None and view.is_overridden(path):
            # 索引里的还是旧内容，用改动后的文本重新取一次源码。
            source = _source_from_text(view.overridden_text(path) or "", name)
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
    pending_hits = _pending_symbols(view, name)
    if not rows and not pending_hits:
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
    lines.extend(pending_hits)
    return ToolResult(ok=True, content="\n".join(lines))


def _source_from_text(text: str, name: str) -> str | None:
    """从一段源码文本里取出某个符号的源码（按行号切）。"""
    engine = PythonAstExtractor()
    try:
        symbols = engine.extract(text, "inline")
    except SyntaxError:
        return None
    lines = text.splitlines()
    for sym in symbols:
        if sym.name == name:
            return "\n".join(lines[sym.start_line - 1 : sym.end_line])
    return None


def _file_symbols(conn: sqlite3.Connection, args: dict, view=None) -> ToolResult:
    path = args["path"]
    if view is not None and view.is_overridden(path):
        text = view.overridden_text(path) or ""
        rendered = _render_symbols_from_text(text, path)
        return ToolResult(ok=bool(rendered), content=rendered or f"索引中没有该文件: {path}")
    text = render_file_symbols(
        conn, path, OfflineTokenCounter(), SYMBOL_LIST_BUDGET
    )
    if not text:
        return ToolResult(ok=False, content=f"索引中没有该文件: {path}")
    return ToolResult(ok=True, content=text)


def _render_symbols_from_text(text: str, path: str) -> str:
    """用改动后的文本渲染一份符号表，格式与 render_file_symbols 一致。"""
    engine = PythonAstExtractor()
    try:
        symbols = engine.extract(text, path)
    except SyntaxError:
        return ""
    if not symbols:
        return ""
    lines = [f"{path}:"]
    lines.extend(
        f"  {sym.name} | {sym.signature} | L{sym.start_line}-{sym.end_line}"
        for sym in symbols
    )
    return "\n".join(lines)


def find_symbol_spec(root: Path, conn: sqlite3.Connection, pending=None) -> ToolSpec:
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
        handler=lambda args: _find_symbol(
            root, conn, args, WorkspaceView(root, pending)
        ),
        brief="按名字找符号",
        group="看",
    )


def file_symbols_spec(conn: sqlite3.Connection, pending=None) -> ToolSpec:
    """列出某文件的全部符号签名，不含函数体。"""
    # 这里只用视图的叠加部分（按相对路径取改动后的文本），不做路径解析，
    # 所以 root 传占位值即可。
    view = WorkspaceView(Path("."), pending)
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
        handler=lambda args: _file_symbols(conn, args, view),
        brief="列文件的符号",
        group="看",
    )


def _find_callers(
    conn: sqlite3.Connection, args: dict, counter: OfflineTokenCounter, view=None
) -> ToolResult:
    name = args["name"]
    path = args.get("path")

    # 索引是按磁盘内容建的。改动没落盘时，新符号查不到、旧引用还在，
    # 所以先用改动后的文本补一份定义与引用。
    defs, pending_refs = _pending_mentions(view, name)

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
        if defs:
            return ToolResult(
                ok=True,
                content=_pending_only_report(name, defs, pending_refs),
            )
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
    if pending_refs:
        parts.append(
            "尚未落盘的改动里还引用了它：" + "、".join(pending_refs)
        )
    return ToolResult(ok=True, content="\n".join(parts))


def _pending_only_report(name: str, defs: list[str], refs: list[str]) -> str:
    lines = [f"{name} 只出现在尚未落盘的改动里："]
    lines.extend(f"  {item}" for item in defs)
    if refs:
        lines.append("改动里引用它的地方：" + "、".join(refs))
    else:
        lines.append("改动里还没有地方引用它。")
    lines.append("（这些改动还没写入磁盘，索引里当然查不到。）")
    return "\n".join(lines)


def _pending_mentions(view, name: str) -> tuple[list[str], list[str]]:
    """在待确认改动里找这个符号的定义与引用。

    find_callers 原先只查索引，于是「刚改完名去查新名字」必然报
    「找不到符号」——而调用方其实已经在改动里跟着改了。
    """
    if view is None:
        return [], []
    engine = PythonAstExtractor()
    defs: list[str] = []
    refs: list[str] = []
    for path in view.overridden:
        if not path.endswith(".py"):
            continue
        text = view.overridden_text(path)
        if not text:
            continue
        try:
            symbols = engine.extract(text, path)
        except SyntaxError:
            continue
        for sym in symbols:
            if sym.name == name:
                defs.append(f"{path}:{sym.start_line} {sym.signature}")
        try:
            extracted = extract_refs(text)
        except SyntaxError:
            continue
        for ref in extracted:
            if ref.dst_name == name:
                refs.append(f"{path} 的 {ref.src_qualified}（{ref.kind}）")
    return defs, refs


def find_callers_spec(conn: sqlite3.Connection, pending=None, root: Path | None = None) -> ToolSpec:
    """查看谁引用了某符号，以及改动它会波及什么。"""
    view = WorkspaceView(root or Path("."), pending)
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
        handler=lambda args: _find_callers(
            conn, args, OfflineTokenCounter(), view
        ),
        brief="查谁引用了它",
        group="看",
    )
