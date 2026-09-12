"""索引构建与增量更新。

以文件内容哈希为键：内容未变则跳过，只有改动过的文件重新解析。
单个文件解析失败不会中断整次索引——真实仓库里总会有语法不完整的
文件，让一个坏文件挡住全量索引是不可接受的。
"""

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from agents_dev.index.symbols import PythonAstExtractor, SymbolExtractor
from agents_dev.index.refs import extract_refs
from agents_dev.index.graph import resolve_refs, store_refs

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

# 索引是有代价的增强，不能无上限地做。
#
# 实测：40 万文件的目录树，光遍历就 4.4 秒（rglob 会走完整棵树再过滤）；
# 8.5 万个 .py 全量解析则要几分钟，外加一个巨大的库。而它对「用一个 7B 模型
# 改一处代码」这件事的边际价值，在超过一定规模后是负的——模型读不完，
# 预取也选不准。所以到量就停，并且**明说没建**，而不是悄悄给一个残的。
MAX_INDEX_FILES = 2000
INDEX_SECONDS = 20.0


@dataclass
class IndexStats:
    """一次索引的结果统计。"""

    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    files_failed: int = 0
    symbols: int = 0
    # 到量停下了就记下原因。残的索引比没有更糟：查不到会被当成「不存在」。
    stopped: str = ""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iter_source_files(root: Path):
    """遍历需要索引的 Python 文件。

    必须在遍历时剪枝。原先用 rglob 再过滤：它会先把整棵树走一遍，
    跳过 .venv / node_modules 的名单等于没生效——只在读文件那一步省。
    实测 40 万文件的树，仅遍历就 4.4 秒，其中绝大多数是被跳过的目录。
    """
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(current) / name


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
    max_files: int = MAX_INDEX_FILES,
    seconds: float = INDEX_SECONDS,
) -> IndexStats:
    """扫描并增量更新索引。超过上限就停下，并在 stats.stopped 里说明原因。"""
    engine = extractor or PythonAstExtractor()
    stats = IndexStats()
    seen: set[str] = set()
    started = time.time()

    for path in iter_source_files(root):
        if stats.files_scanned >= max_files:
            stats.stopped = f"文件数超过上限 {max_files}"
            break
        if time.time() - started > seconds:
            stats.stopped = f"索引耗时超过 {seconds:.0f} 秒预算"
            break
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
        id_by_qualified = {
            (f"{row['parent']}.{row['name']}" if row["parent"] else row["name"]): row["id"]
            for row in conn.execute(
                "SELECT s.id AS id, s.name AS name, p.name AS parent"
                " FROM symbol s LEFT JOIN symbol p ON p.id = s.parent_id"
                " WHERE s.file_id = ?",
                (file_id,),
            ).fetchall()
        }
        try:
            refs = extract_refs(source)
        except SyntaxError:
            refs = []
        store_refs(conn, file_id, refs, id_by_qualified)
        stats.files_indexed += 1

    # 只有走完整棵树才能清理「已经不存在」的记录。提前退出时 seen 是不全的，
    # 照删会把没走到的文件全删掉——那是把上限变成了数据损坏。
    if not stats.stopped:
        for row in conn.execute("SELECT id, path FROM file").fetchall():
            if row["path"] not in seen:
                conn.execute("DELETE FROM file WHERE id = ?", (row["id"],))
                stats.files_removed += 1

    conn.commit()
    resolve_refs(conn)
    return stats
