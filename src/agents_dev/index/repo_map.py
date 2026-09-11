"""代码索引的分层渲染。

L0 仓库地图：全部文件路径 + 各文件顶级符号名，用于让模型知道「有什么」。
L1 文件符号表：某文件的全部符号签名与行号，不含函数体。
L2 符号源码：按行区间取出单个符号的实现。

三者都强制遵守 token 上限。超限时必须显式标注省略了多少内容——
静默截断会让模型以为自己看到了全部，从而做出错误判断。
"""

import sqlite3
from pathlib import Path

from agents_dev.index.graph import callers, callees, unresolved_count
from agents_dev.llm.tokenizer import TokenCounter


def _fit_lines(
    lines: list[str], suffix: str, counter: TokenCounter, limit: int
) -> str:
    """在 token 上限内保留尽可能多的行；发生截断时标注省略数量。"""
    total = len(lines)
    truncated = False
    while counter.count("\n".join(lines)) > limit and lines:
        lines.pop()
        truncated = True
    if not truncated:
        return "\n".join(lines)

    while lines:
        text = "\n".join(lines) + f"\n…（还有 {total - len(lines)} 项{suffix}）"
        if counter.count(text) <= limit:
            return text
        lines.pop()
    return f"…（还有 {total} 项{suffix}）"


def render_repo_map(
    conn: sqlite3.Connection, counter: TokenCounter, max_tokens: int
) -> str:
    """渲染 L0 仓库地图。"""
    rows = conn.execute(
        "SELECT f.path AS path, s.name AS name, s.start_line AS start_line"
        " FROM file f"
        " LEFT JOIN symbol s ON s.file_id = f.id AND s.parent_id IS NULL"
        " ORDER BY f.path, s.start_line"
    ).fetchall()

    grouped: dict[str, list[str]] = {}
    for row in rows:
        names = grouped.setdefault(row["path"], [])
        if row["name"]:
            names.append(row["name"])

    lines = [
        f"{path}: {', '.join(names)}" if names else path
        for path, names in grouped.items()
    ]
    return _fit_lines(lines, "文件省略", counter, max_tokens)


def render_file_symbols(
    conn: sqlite3.Connection,
    path: str,
    counter: TokenCounter,
    max_tokens: int,
) -> str:
    """渲染 L1 文件符号表。"""
    rows = conn.execute(
        "SELECT s.name AS name, s.signature AS signature,"
        " s.start_line AS start_line, s.end_line AS end_line, p.name AS parent_name"
        " FROM symbol s"
        " JOIN file f ON f.id = s.file_id"
        " LEFT JOIN symbol p ON p.id = s.parent_id"
        " WHERE f.path = ?"
        " ORDER BY s.start_line",
        (path,),
    ).fetchall()
    if not rows:
        return ""

    lines = [f"{path}:"]
    for row in rows:
        qualified = (
            f"{row['parent_name']}.{row['name']}" if row["parent_name"] else row["name"]
        )
        lines.append(
            f"  {qualified} | {row['signature']}"
            f" | L{row['start_line']}-{row['end_line']}"
        )
    return _fit_lines(lines, "符号省略", counter, max_tokens)


def load_symbol_source(
    root: Path,
    conn: sqlite3.Connection,
    path: str,
    name: str,
) -> str | None:
    """渲染 L2：按符号取源码。name 可写成 'Class.method' 或纯符号名。"""
    parent, _, leaf = name.rpartition(".")
    rows = conn.execute(
        "SELECT s.name AS name, s.start_line AS start_line, s.end_line AS end_line,"
        " p.name AS parent_name"
        " FROM symbol s"
        " JOIN file f ON f.id = s.file_id"
        " LEFT JOIN symbol p ON p.id = s.parent_id"
        " WHERE f.path = ? AND s.name = ?",
        (path, leaf),
    ).fetchall()

    if parent:
        row = next((r for r in rows if r["parent_name"] == parent), None)
    else:
        row = rows[0] if rows else None
    if row is None:
        return None

    target = root / path
    if not target.exists():
        return None
    lines = target.read_text(encoding="utf-8").splitlines()
    start = max(1, row["start_line"])
    end = min(len(lines), row["end_line"])
    return "\n".join(lines[start - 1 : end])


def render_neighborhood(
    conn: sqlite3.Connection,
    symbol_id: int,
    counter: TokenCounter,
    max_tokens: int,
) -> str:
    """渲染 L3 邻域：准备改动一个符号时，它周围有什么。

    这个层次不参与「省 token」的账：它存在的理由是正确性。
    不看引用方就动手改，是最容易把别处改坏的方式。

    同时必须报告「够不到的引用」：留空是「有但定不了」，
    和「确实没有」是两回事，混淆这两者会导致放心地改错。
    """
    row = conn.execute(
        "SELECT s.start_line AS start_line, s.signature AS signature,"
        " f.path AS path FROM symbol s JOIN file f ON f.id = s.file_id"
        " WHERE s.id = ?",
        (symbol_id,),
    ).fetchone()
    if row is None:
        return ""

    lines = [f"{row['path']}:{row['start_line']} {row['signature']}"]

    used_by = callers(conn, symbol_id)
    if used_by:
        lines.append(f"被 {len(used_by)} 处引用：")
        lines.extend(f"  {item.path}:{item.start_line} {item.name}" for item in used_by)
    else:
        lines.append("没有静态可解析的引用方。")

    depends_on = callees(conn, symbol_id)
    if depends_on:
        names = "、".join(item.name for item in depends_on)
        lines.append(f"它引用了 {len(depends_on)} 个符号：{names}")

    unresolved = unresolved_count(conn, symbol_id)
    if unresolved:
        lines.append(
            f"另有 {unresolved} 处引用指向同名符号但无法唯一确定，未计入上表。"
        )

    return _fit_lines(lines, "行省略", counter, max_tokens)
