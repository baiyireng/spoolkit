"""索引构建与增量更新。

以文件内容哈希为键：内容未变则跳过，只有改动过的文件重新解析。
单个文件解析失败不会中断整次索引——真实仓库里总会有语法不完整的
文件，让一个坏文件挡住全量索引是不可接受的。
"""

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from agents_dev.index.symbols import PythonAstExtractor, SymbolExtractor

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".agent",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)


@dataclass
class IndexStats:
    """一次索引的结果统计。"""

    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    files_failed: int = 0
    symbols: int = 0


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iter_source_files(root: Path):
    """遍历需要索引的 Python 文件，跳过虚拟环境与缓存目录。"""
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield path


def _store_symbols(conn: sqlite3.Connection, file_id: int, symbols) -> int:
    """写入符号。提取器保证父符号先于子符号出现。"""
    id_by_name: dict[str, int] = {}
    count = 0
    for sym in symbols:
        parent_id = id_by_name.get(sym.parent) if sym.parent else None
        cursor = conn.execute(
            "INSERT INTO symbol"
            "(file_id, parent_id, name, kind, start_line, end_line, signature, doc)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                file_id,
                parent_id,
                sym.name,
                sym.kind,
                sym.start_line,
                sym.end_line,
                sym.signature,
                sym.doc,
            ),
        )
        id_by_name[sym.name] = cursor.lastrowid
        count += 1
    return count


def index_project(
    root: Path,
    conn: sqlite3.Connection,
    extractor: SymbolExtractor | None = None,
) -> IndexStats:
    """扫描并增量更新索引。"""
    engine = extractor or PythonAstExtractor()
    stats = IndexStats()
    seen: set[str] = set()

    for path in iter_source_files(root):
        rel = path.relative_to(root).as_posix()
        seen.add(rel)
        stats.files_scanned += 1

        source = path.read_text(encoding="utf-8")
        digest = _hash(source)
        row = conn.execute(
            "SELECT id, content_hash FROM file WHERE path = ?", (rel,)
        ).fetchone()

        if row is not None and row["content_hash"] == digest:
            stats.files_skipped += 1
            continue

        try:
            symbols = engine.extract(source, rel)
        except SyntaxError:
            stats.files_failed += 1
            continue

        mtime = path.stat().st_mtime
        now = time.time()
        if row is not None:
            file_id = row["id"]
            conn.execute("DELETE FROM symbol WHERE file_id = ?", (file_id,))
            conn.execute(
                "UPDATE file SET content_hash=?, mtime=?, indexed_at=? WHERE id=?",
                (digest, mtime, now, file_id),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO file(path, lang, content_hash, mtime, indexed_at)"
                " VALUES (?,?,?,?,?)",
                (rel, "python", digest, mtime, now),
            )
            file_id = cursor.lastrowid

        stats.symbols += _store_symbols(conn, file_id, symbols)
        stats.files_indexed += 1

    for row in conn.execute("SELECT id, path FROM file").fetchall():
        if row["path"] not in seen:
            conn.execute("DELETE FROM file WHERE id = ?", (row["id"],))
            stats.files_removed += 1

    conn.commit()
    return stats

