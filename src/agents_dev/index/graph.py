"""引用图的存储、消歧与查询。

消歧策略是分级降级的：
1. 同文件内有同名符号 —— 最强证据，直接采用；
2. 全库范围内恰好只有一个同名符号 —— 可以采用；
3. 其余情况一律留空，如实记为「无法确定」。

第 3 条是刻意的。一个错误的「这个函数只有一个调用方」比「有三个可能」
危险得多：前者会让人放心地改下去。
"""

import sqlite3
from dataclasses import dataclass

from agents_dev.index.refs import Ref


@dataclass(frozen=True)
class Related:
    """引用关系里的一个符号。"""

    id: int
    name: str
    path: str
    start_line: int
    signature: str
    kind: str


def store_refs(
    conn: sqlite3.Connection,
    file_id: int,
    refs: list[Ref],
    id_by_qualified: dict[str, int],
) -> int:
    """写入引用边。目标解析留到全库符号都就绪之后统一做。"""
    count = 0
    for ref in refs:
        src_id = id_by_qualified.get(ref.src_qualified)
        if src_id is None:
            continue
        conn.execute(
            "INSERT INTO ref(src_symbol_id, dst_name, dst_symbol_id, kind)"
            " VALUES (?,?,NULL,?)",
            (src_id, ref.dst_name, ref.kind),
        )
        count += 1
    return count


def resolve_refs(conn: sqlite3.Connection) -> int:
    """尽可能把引用目标解析到具体符号，返回成功解析的条数。"""
    rows = conn.execute(
        "SELECT id, dst_name, src_symbol_id FROM ref WHERE dst_symbol_id IS NULL"
    ).fetchall()
    resolved = 0
    for row in rows:
        source = conn.execute(
            "SELECT file_id FROM symbol WHERE id = ?", (row["src_symbol_id"],)
        ).fetchone()
        if source is None:
            continue

        same_file = conn.execute(
            "SELECT id FROM symbol WHERE name = ? AND file_id = ?",
            (row["dst_name"], source["file_id"]),
        ).fetchall()
        if len(same_file) == 1:
            target = same_file[0]["id"]
        else:
            anywhere = conn.execute(
                "SELECT id FROM symbol WHERE name = ?", (row["dst_name"],)
            ).fetchall()
            if len(anywhere) != 1:
                continue
            target = anywhere[0]["id"]

        conn.execute(
            "UPDATE ref SET dst_symbol_id = ? WHERE id = ?", (target, row["id"])
        )
        resolved += 1
    conn.commit()
    return resolved


_SELECT_RELATED = (
    "SELECT s.id AS id, s.name AS name, f.path AS path,"
    " s.start_line AS start_line, s.signature AS signature, s.kind AS kind"
    " FROM {table} x"
    " JOIN symbol s ON s.id = x.other_id"
    " JOIN file f ON f.id = s.file_id"
    " WHERE x.anchor_id = ?"
    " ORDER BY f.path, s.start_line"
)


def _related(conn: sqlite3.Connection, symbol_id: int, direction: str) -> list[Related]:
    if direction == "callers":
        sql = _SELECT_RELATED.format(table="(SELECT src_symbol_id AS other_id,"
                                     " dst_symbol_id AS anchor_id FROM ref)")
    else:
        sql = _SELECT_RELATED.format(table="(SELECT dst_symbol_id AS other_id,"
                                     " src_symbol_id AS anchor_id FROM ref)")
    rows = conn.execute(sql, (symbol_id,)).fetchall()
    return [
        Related(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            start_line=row["start_line"],
            signature=row["signature"],
            kind=row["kind"],
        )
        for row in rows
        if row["id"] is not None
    ]


def callers(conn: sqlite3.Connection, symbol_id: int) -> list[Related]:
    """谁引用了这个符号。"""
    return _related(conn, symbol_id, "callers")


def callees(conn: sqlite3.Connection, symbol_id: int) -> list[Related]:
    """这个符号引用了谁。"""
    return _related(conn, symbol_id, "callees")


def impact(
    conn: sqlite3.Connection,
    symbol_id: int,
    depth: int = 2,
    limit: int = 20,
) -> tuple[list[Related], bool, int]:
    """反向传递闭包：改动这个符号会波及谁。

    返回（受影响的符号，是否因上限被截断，未解析的引用条数）。
    闭包规模会随底层工具的普适程度爆炸，所以必须同时限深度和限数量。
    """
    seen: set[int] = {symbol_id}
    frontier = [symbol_id]
    found: list[Related] = []
    truncated = False

    for _ in range(max(0, depth)):
        next_frontier: list[int] = []
        for current in frontier:
            for item in callers(conn, current):
                if item.id in seen:
                    continue
                if len(found) >= limit:
                    truncated = True
                    break
                seen.add(item.id)
                found.append(item)
                next_frontier.append(item.id)
            if truncated:
                break
        if truncated or not next_frontier:
            break
        frontier = next_frontier

    return found, truncated, unresolved_count(conn, symbol_id)


def unresolved_count(conn: sqlite3.Connection, symbol_id: int) -> int:
    """可能指向这个符号、却无法唯一确定的引用条数。

    这个数字必须暴露出来：它代表「分析够不到的地方」，而不是「没有」。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ref"
        " WHERE dst_symbol_id IS NULL"
        "   AND dst_name = (SELECT name FROM symbol WHERE id = ?)",
        (symbol_id,),
    ).fetchone()
    return int(row["n"]) if row else 0
