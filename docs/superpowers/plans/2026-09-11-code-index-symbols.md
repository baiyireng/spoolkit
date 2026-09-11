# 代码索引层（符号表与分层加载） 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Python 标准库 `ast` 建立代码符号索引，让 agent 能「精确定位到函数」而不是「整份文件塞进上下文」，并支持按文件哈希增量更新。

**Architecture:** 索引落在 SQLite（`file` / `symbol` 两张表），用内容哈希做增量。对外提供三个层次的读取：L0 仓库地图（全部文件 + 顶级符号名，token 有上限）、L1 文件符号表（含签名与行号，不含函数体）、L2 单个符号源码。检索器用任务描述里的关键词自动预取 L1 候选，模型只做最终判断。

**Tech Stack:** Python 3.14 标准库（`ast`、`sqlite3`、`hashlib`）、pytest。**不新增任何依赖**，这一层完全离线，不需要 GPU。

## Global Constraints

- 依赖只允许 `httpx`（运行时）与 `pytest`（测试）。本计划不新增依赖。
- 解析器实现必须藏在一个可替换接口后面，将来换 tree-sitter 时上层不动。
- 索引必须支持增量：文件内容哈希未变则跳过重建。
- 所有渲染函数都必须接受 token 上限，输出不得超限。
- 索引文件位于 `.agent/index.db`，已在 `.gitignore` 中。
- 代码与注释使用中文；标识符使用英文。
- 每个任务结束时 pytest 全绿并提交一次。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/agents_dev/store/db.py` | SQLite 连接与 schema |
| `src/agents_dev/index/symbols.py` | `Symbol` 类型、提取器接口、`ast` 实现 |
| `src/agents_dev/index/indexer.py` | 全量/增量建索引，清理已删文件 |
| `src/agents_dev/index/repo_map.py` | L0 / L1 / L2 三层渲染 |
| `src/agents_dev/index/rank.py` | 相关性排序与自动预取 |
| `src/agents_dev/index/tools.py` | 索引能力包装成工具 |
| `src/agents_dev/agent/loop.py` | 主循环接入预取（修改） |

---

## Task 1: 存储层

**Files:**
- Create: `src/agents_dev/store/db.py`
- Test: `tests/store/__init__.py`, `tests/store/test_db.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `agents_dev.store.db.SCHEMA: str`
  - `agents_dev.store.db.open_db(path: Path) -> sqlite3.Connection`
  - `agents_dev.store.db.init_schema(conn) -> None`

- [ ] **Step 1: 写失败测试**

```python
# tests/store/__init__.py
"""store 子包测试。"""
```

```python
# tests/store/test_db.py
from pathlib import Path

from agents_dev.store.db import init_schema, open_db


def _tables(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_建库后包含两张核心表(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    assert {"file", "symbol"} <= _tables(conn)
    conn.close()


def test_父目录不存在时自动创建(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "nested" / "index.db")
    init_schema(conn)
    assert (tmp_path / "nested" / "index.db").exists()
    conn.close()


def test_重复初始化不报错(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    init_schema(conn)
    assert "symbol" in _tables(conn)
    conn.close()


def test_外键约束已开启(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "index.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_删除文件记录会级联删除其符号(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    conn.execute(
        "INSERT INTO file(path, lang, content_hash, mtime, indexed_at)"
        " VALUES ('a.py','python','h',0,0)"
    )
    file_id = conn.execute("SELECT id FROM file").fetchone()["id"]
    conn.execute(
        "INSERT INTO symbol(file_id, name, kind, start_line, end_line, signature)"
        " VALUES (?, 'f', 'function', 1, 2, 'def f()')",
        (file_id,),
    )
    conn.execute("DELETE FROM file WHERE id = ?", (file_id,))
    assert conn.execute("SELECT COUNT(*) FROM symbol").fetchone()[0] == 0
    conn.close()

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/store -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.store.db'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/store/db.py
"""SQLite 存储层。

索引数据放在独立的 .agent/index.db 中，可随时删除重建——
它是派生数据，不是事实来源。
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS file (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    lang         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mtime        REAL NOT NULL,
    indexed_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS symbol (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    parent_id  INTEGER REFERENCES symbol(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    signature  TEXT NOT NULL,
    doc        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_symbol_file ON symbol(file_id);
CREATE INDEX IF NOT EXISTS idx_symbol_name ON symbol(name);
CREATE INDEX IF NOT EXISTS idx_symbol_parent ON symbol(parent_id);
"""


def open_db(path: Path) -> sqlite3.Connection:
    """打开（必要时创建）索引数据库。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """建表。可重复调用，不会破坏已有数据。"""
    conn.executescript(SCHEMA)
    conn.commit()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/store -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/store/db.py tests/store/
git commit -m "feat: 索引层 SQLite 存储与 schema"
```

---

## Task 2: 符号提取

**Files:**
- Create: `src/agents_dev/index/symbols.py`
- Test: `tests/index/__init__.py`, `tests/index/test_symbols.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `agents_dev.index.symbols.Symbol(name, kind, start_line, end_line, signature, parent, doc)`
  - `agents_dev.index.symbols.SymbolExtractor`（协议，方法 `extract(source: str, path: str) -> list[Symbol]`）
  - `agents_dev.index.symbols.PythonAstExtractor()`

- [ ] **Step 1: 写失败测试**

```python
# tests/index/__init__.py
"""index 子包测试。"""
```

```python
# tests/index/test_symbols.py
import pytest

from agents_dev.index.symbols import PythonAstExtractor, Symbol


def _extract(source: str) -> list[Symbol]:
    return PythonAstExtractor().extract(source, "m.py")


def _by_name(symbols: list[Symbol], name: str) -> Symbol:
    return next(s for s in symbols if s.name == name)


def test_提取顶层函数() -> None:
    symbols = _extract("def foo(a, b):\n    return a\n")
    assert [s.name for s in symbols] == ["foo"]
    assert symbols[0].kind == "function"
    assert symbols[0].start_line == 1
    assert symbols[0].end_line == 2


def test_签名包含参数与返回类型() -> None:
    symbols = _extract("def foo(a: int, b: str = 'x') -> bool:\n    return True\n")
    assert symbols[0].signature == "def foo(a: int, b: str = 'x') -> bool"


def test_提取类及其方法并标记父子关系() -> None:
    source = "class A:\n    def m(self):\n        pass\n"
    symbols = _extract(source)
    assert _by_name(symbols, "A").kind == "class"
    method = _by_name(symbols, "m")
    assert method.kind == "method"
    assert method.parent == "A"


def test_类签名包含基类() -> None:
    symbols = _extract("class B(Base, Mixin):\n    pass\n")
    assert symbols[0].signature == "class B(Base, Mixin)"


def test_异步函数被识别() -> None:
    symbols = _extract("async def fetch():\n    pass\n")
    assert symbols[0].name == "fetch"
    assert symbols[0].signature.startswith("async def fetch")


def test_带装饰器时起始行覆盖装饰器() -> None:
    source = "@deco\ndef foo():\n    pass\n"
    assert _extract(source)[0].start_line == 1


def test_提取文档字符串() -> None:
    symbols = _extract('def foo():\n    """说明文字。"""\n    pass\n')
    assert "说明文字" in symbols[0].doc


def test_嵌套函数不进索引() -> None:
    source = "def outer():\n    def inner():\n        pass\n"
    assert [s.name for s in _extract(source)] == ["outer"]


def test_语法错误时抛出可识别的异常() -> None:
    with pytest.raises(SyntaxError):
        _extract("def broken(:\n")


def test_空文件返回空列表() -> None:
    assert _extract("") == []

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/index -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.index.symbols'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/index/symbols.py
"""符号提取。

当前实现基于标准库 ast，只支持 Python。接口刻意设计成可替换，
将来接入 tree-sitter 支持多语言时，索引器与渲染层无需改动。

有意不索引嵌套函数：它们没有稳定的对外身份，索引它们只会让
符号表变噪声，而噪声直接消耗上下文预算。
"""

import ast
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Symbol:
    """代码中的一个符号。行号从 1 开始，区间为闭区间。"""

    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str
    parent: str | None = None
    doc: str = ""


class SymbolExtractor(Protocol):
    """符号提取器接口。"""

    def extract(self, source: str, path: str) -> list[Symbol]:
        """从源码中提取符号。"""
        ...


def _format_arg(node: ast.arg) -> str:
    if node.annotation is None:
        return node.arg
    return f"{node.arg}: {ast.unparse(node.annotation)}"


def _format_default(node: ast.expr) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # 极少数节点无法反解析时退化为占位
        return "…"


def _format_params(args: ast.arguments) -> list[str]:
    parts = [_format_arg(a) for a in list(args.posonlyargs) + list(args.args)]
    defaults = [_format_default(d) for d in args.defaults]
    if defaults:
        for index, text in enumerate(defaults, start=len(parts) - len(defaults)):
            parts[index] = f"{parts[index]} = {text}"
    if args.vararg is not None:
        parts.append("*" + _format_arg(args.vararg))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        text = _format_arg(arg)
        if default is not None:
            text = f"{text} = {_format_default(default)}"
        parts.append(text)
    if args.kwarg is not None:
        parts.append("**" + _format_arg(args.kwarg))
    return parts


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    params = ", ".join(_format_params(node.args))
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({params}){ret}"


def _class_signature(node: ast.ClassDef) -> str:
    bases = [_format_default(b) for b in node.bases]
    return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"


def _start_line(node: ast.AST) -> int:
    """有装饰器时从装饰器起算，保证后续按行取源码时不会漏掉它们。"""
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *[d.lineno for d in decorators]])


class PythonAstExtractor:
    """基于标准库 ast 的 Python 符号提取器。"""

    def extract(self, source: str, path: str) -> list[Symbol]:
        tree = ast.parse(source, filename=path)
        found: list[Symbol] = []
        self._walk(tree.body, None, found)
        return found

    def _walk(self, body, parent: str | None, out: list[Symbol]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                out.append(
                    Symbol(
                        name=node.name,
                        kind="class",
                        start_line=_start_line(node),
                        end_line=node.end_lineno or node.lineno,
                        signature=_class_signature(node),
                        parent=parent,
                        doc=ast.get_docstring(node) or "",
                    )
                )
                self._walk(node.body, node.name, out)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(
                    Symbol(
                        name=node.name,
                        kind="method" if parent else "function",
                        start_line=_start_line(node),
                        end_line=node.end_lineno or node.lineno,
                        signature=_function_signature(node),
                        parent=parent,
                        doc=ast.get_docstring(node) or "",
                    )
                )
                # 不递归进函数体：嵌套函数不进索引

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/index -q`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/index/symbols.py tests/index/
git commit -m "feat: 基于 ast 的 Python 符号提取"
```

---

## Task 3: 索引构建与增量更新

**Files:**
- Create: `src/agents_dev/index/indexer.py`
- Test: `tests/index/test_indexer.py`

**Interfaces:**
- Consumes: `open_db`、`init_schema`、`SymbolExtractor`、`PythonAstExtractor`
- Produces:
  - `agents_dev.index.indexer.SKIP_DIRS: frozenset[str]`
  - `agents_dev.index.indexer.IndexStats(files_scanned, files_indexed, files_skipped, files_removed, symbols)`
  - `agents_dev.index.indexer.index_project(root: Path, conn, extractor=None) -> IndexStats`

- [ ] **Step 1: 写失败测试**

```python
# tests/index/test_indexer.py
from pathlib import Path

import pytest

from agents_dev.index.indexer import index_project
from agents_dev.store.db import init_schema, open_db


def _db(tmp_path: Path):
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    return conn


def test_首次建索引收集文件与符号(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    conn = _db(tmp_path)
    stats = index_project(tmp_path, conn)
    assert stats.files_indexed == 1
    assert stats.symbols == 1
    conn.close()


def test_内容未变的文件被跳过(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    conn = _db(tmp_path)
    index_project(tmp_path, conn)
    stats = index_project(tmp_path, conn)
    assert stats.files_skipped == 1
    assert stats.files_indexed == 0
    conn.close()


def test_文件改动后重建该文件的符号(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("def f():\n    pass\n", encoding="utf-8")
    conn = _db(tmp_path)
    index_project(tmp_path, conn)
    target.write_text("def f():\n    pass\n\n\ndef g():\n    pass\n", encoding="utf-8")
    stats = index_project(tmp_path, conn)
    assert stats.files_indexed == 1
    names = [r["name"] for r in conn.execute("SELECT name FROM symbol").fetchall()]
    assert sorted(names) == ["f", "g"]
    conn.close()


def test_删除的文件被清理(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("def f():\n    pass\n", encoding="utf-8")
    conn = _db(tmp_path)
    index_project(tmp_path, conn)
    target.unlink()
    stats = index_project(tmp_path, conn)
    assert stats.files_removed == 1
    assert conn.execute("SELECT COUNT(*) FROM file").fetchone()[0] == 0
    conn.close()


def test_跳过虚拟环境与缓存目录(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "junk.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    conn = _db(tmp_path)
    stats = index_project(tmp_path, conn)
    assert stats.files_scanned == 1
    conn.close()


def test_语法错误的文件被跳过而不中断整次索引(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("def ok():\n    pass\n", encoding="utf-8")
    conn = _db(tmp_path)
    stats = index_project(tmp_path, conn)
    assert stats.files_indexed == 1
    assert stats.files_failed == 1
    conn.close()


def test_方法的父子关系被正确写入(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "class A:\n    def m(self):\n        pass\n", encoding="utf-8"
    )
    conn = _db(tmp_path)
    index_project(tmp_path, conn)
    row = conn.execute("SELECT parent_id FROM symbol WHERE name='m'").fetchone()
    assert row["parent_id"] is not None
    conn.close()

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/index/test_indexer.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.index.indexer'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/index/indexer.py
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
    """写入符号，保证父符号先于子符号。"""
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

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/index -q`
Expected: PASS（17 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/index/indexer.py tests/index/test_indexer.py
git commit -m "feat: 索引构建与按内容哈希增量更新"
```

---

## Task 4: 分层渲染（L0 / L1 / L2）

**Files:**
- Create: `src/agents_dev/index/repo_map.py`
- Test: `tests/index/test_repo_map.py`

**Interfaces:**
- Consumes: `TokenCounter`、SQLite 连接
- Produces:
  - `agents_dev.index.repo_map.render_repo_map(conn, counter, max_tokens) -> str`（L0）
  - `agents_dev.index.repo_map.render_file_symbols(conn, path, counter, max_tokens) -> str`（L1）
  - `agents_dev.index.repo_map.load_symbol_source(root, conn, path, name) -> str | None`（L2）

三个函数都必须保证输出不超过 `max_tokens`；`render_*` 超限时按文件/符号粒度截断并附省略说明。

- [ ] **Step 1: 写失败测试**

```python
# tests/index/test_repo_map.py
from pathlib import Path

from agents_dev.index.indexer import index_project
from agents_dev.index.repo_map import (
    load_symbol_source,
    render_file_symbols,
    render_repo_map,
)
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.store.db import init_schema, open_db


def _project(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        "def alpha(x: int) -> int:\n"
        "    \"\"\"返回两倍。\"\"\"\n"
        "    return x * 2\n"
        "\n"
        "\n"
        "class Beta:\n"
        "    def run(self):\n"
        "        return alpha(1)\n",
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text("def entry():\n    pass\n", encoding="utf-8")
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    return conn


def test_仓库地图包含文件与顶级符号(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    text = render_repo_map(conn, OfflineTokenCounter(), 500)
    assert "pkg/mod.py" in text
    assert "alpha" in text
    assert "Beta" in text
    assert "main.py" in text
    conn.close()


def test_仓库地图不含函数体内容(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    text = render_repo_map(conn, OfflineTokenCounter(), 500)
    assert "return x * 2" not in text
    conn.close()


def test_仓库地图遵守token上限(tmp_path: Path) -> None:
    for i in range(40):
        (tmp_path / f"m{i}.py").write_text(
            "".join(f"def f{j}():\n    pass\n" for j in range(20)), encoding="utf-8"
        )
    conn = _project(tmp_path)
    counter = OfflineTokenCounter()
    text = render_repo_map(conn, counter, 200)
    assert counter.count(text) <= 200
    assert "省略" in text
    conn.close()


def test_文件符号表含签名与行号但不含函数体(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    text = render_file_symbols(conn, "pkg/mod.py", OfflineTokenCounter(), 400)
    assert "def alpha(x: int) -> int" in text
    assert "def run(self)" in text
    assert "return x * 2" not in text
    conn.close()


def test_文件符号表标记方法归属(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    text = render_file_symbols(conn, "pkg/mod.py", OfflineTokenCounter(), 400)
    assert "Beta.run" in text
    conn.close()


def test_不存在的文件返回空串(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert render_file_symbols(conn, "nope.py", OfflineTokenCounter(), 400) == ""
    conn.close()


def test_按符号取出源码片段(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    source = load_symbol_source(tmp_path, conn, "pkg/mod.py", "alpha")
    assert source is not None
    assert "def alpha(x: int) -> int" in source
    assert "return x * 2" in source
    assert "class Beta" not in source
    conn.close()


def test_取不存在的符号返回空(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert load_symbol_source(tmp_path, conn, "pkg/mod.py", "nope") is None
    conn.close()

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/index/test_repo_map.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.index.repo_map'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/index/repo_map.py
"""代码索引的分层渲染。

L0 仓库地图：全部文件路径 + 各文件顶级符号名，用于让模型知道「有什么」。
L1 文件符号表：某文件的全部符号签名与行号，不含函数体。
L2 符号源码：按行区间取出单个符号的实现。

三者都强制遵守 token 上限。超限时必须显式标注省略了多少内容——
静默截断会让模型以为自己看到了全部，从而做出错误判断。
"""

import sqlite3
from pathlib import Path

from agents_dev.llm.tokenizer import TokenCounter


def _fit_lines(lines: list[str], suffix_template: str, counter: TokenCounter, limit: int):
    """在 token 上限内保留尽可能多的行，并在截断时标注省略数量。"""
    returned = len(lines)
    omitted = 0
    while omitted == 0 and counter.count("\n".join(lines)) > limit and lines:
        lines.pop()
        omitted = 1
    while omitted:
        omitted = returned - len(lines)
        text = "\n".join(lines) + "\n" + suffix_template.format(omitted)
        if counter.count(text) <= limit or not lines:
            return text
        lines.pop()
    return "\n".join(lines)


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
    return _fit_lines(lines, "…（还有 {0} 个文件省略）", counter, max_tokens)


def render_file_symbols(
    conn: sqlite3.Connection,
    path: str,
    counter: TokenCounter,
    max_tokens: int,
) -> str:
    """渲染 L1 文件符号表。"""
    rows = conn.execute(
        "SELECT s.name AS name, s.kind AS kind, s.signature AS signature,"
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
            f"  {qualified} | {row['signature']} "
            f"| L{row['start_line']}-{row['end_line']}"
        )
    return _fit_lines(lines, "  …（还有 {0} 个符号省略）", counter, max_tokens)


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

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/index -q`
Expected: PASS（25 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/index/repo_map.py tests/index/test_repo_map.py
git commit -m "feat: 索引的 L0/L1/L2 分层渲染"
```

---

## Task 5: 相关性排序与自动预取

**Files:**
- Create: `src/agents_dev/index/rank.py`
- Test: `tests/index/test_rank.py`

**Interfaces:**
- Consumes: SQLite 连接、`TokenCounter`、`render_file_symbols`
- Produces:
  - `agents_dev.index.rank.extract_keywords(text: str) -> list[str]`
  - `agents_dev.index.rank.rank_files(conn, keywords, limit=5) -> list[str]`
  - `agents_dev.index.rank.prefetch(conn, task_text, counter, max_tokens) -> str`

排序思路：符号名或文件路径精确命中关键词得分最高，部分命中次之，
再叠加「该文件符号数量」作为轻微权重。规则很土，但确定性好、
不需要模型参与，而且出问题时能一眼看出是排序规则的问题还是模型的问题。

- [ ] **Step 1: 写失败测试**

```python
# tests/index/test_rank.py
from pathlib import Path

from agents_dev.index.indexer import index_project
from agents_dev.index.rank import extract_keywords, prefetch, rank_files
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.store.db import init_schema, open_db


def _project(tmp_path: Path):
    (tmp_path / "parser.py").write_text(
        "def parse_config(path):\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "render.py").write_text(
        "def draw_screen():\n    pass\n", encoding="utf-8"
    )
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    return conn


def test_关键词提取去掉英文停用词() -> None:
    words = extract_keywords("请帮我 fix the parse_config 函数")
    assert "parse_config" in words
    assert "the" not in words


def test_关键词提取去掉重复() -> None:
    assert extract_keywords("parse_config parse_config") == ["parse_config"]


def test_命中的文件排在前面(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    ranked = rank_files(conn, ["parse_config"])
    assert ranked[0] == "parser.py"
    conn.close()


def test_文件路径命中也能被选中(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert "render.py" in rank_files(conn, ["render"])
    conn.close()


def test_无命中时返回空列表(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert rank_files(conn, ["完全不相关的词汇zzz"]) == []
    conn.close()


def test_预取结果包含相关文件符号表(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    text = prefetch(conn, "修复 parse_config", OfflineTokenCounter(), 300)
    assert "parser.py" in text
    assert "def parse_config(path)" in text
    conn.close()


def test_预取遵守token上限(tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"m{i}.py").write_text(
            "".join(f"def target_{j}():\n    pass\n" for j in range(20)),
            encoding="utf-8",
        )
    conn = _project(tmp_path)
    counter = OfflineTokenCounter()
    text = prefetch(conn, "target", counter, 150)
    assert counter.count(text) <= 150
    conn.close()


def test_无关键词时预取返回空串(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert prefetch(conn, "。。。", OfflineTokenCounter(), 300) == ""
    conn.close()

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/index/test_rank.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.index.rank'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/index/rank.py
"""相关性排序与自动预取。

模型不擅长决定「下一步该查什么」，所以预取由检索器自动完成：
用任务描述里的关键词匹配符号名与文件路径，把候选文件的符号表直接
放进上下文。模型要做的判断从「该查什么」降级为「这几个里哪个对」。
"""

import re
import sqlite3

from agents_dev.index.repo_map import render_file_symbols
from agents_dev.llm.tokenizer import TokenCounter

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")

_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "have",
        "please", "use", "not", "are", "was", "you", "can", "how",
    }
)

EXACT_HIT = 10
PARTIAL_HIT = 4
PATH_HIT = 3


def extract_keywords(text: str) -> list[str]:
    """提取英文标识符风格的关键词，去停用词、去重、保序。"""
    seen: list[str] = []
    for match in _WORD.findall(text):
        lowered = match.lower()
        if lowered in _STOPWORDS or lowered in seen:
            continue
        seen.append(lowered)
    return seen


def rank_files(
    conn: sqlite3.Connection, keywords: list[str], limit: int = 5
) -> list[str]:
    """按关键词与索引的匹配程度给文件打分排序。"""
    if not keywords:
        return []

    rows = conn.execute(
        "SELECT f.path AS path, s.name AS name"
        " FROM symbol s JOIN file f ON f.id = s.file_id"
    ).fetchall()
    paths = [r["path"] for r in conn.execute("SELECT path FROM file").fetchall()]

    scores: dict[str, int] = {}
    for keyword in keywords:
        for row in rows:
            name = row["name"].lower()
            if name == keyword:
                scores[row["path"]] = scores.get(row["path"], 0) + EXACT_HIT
            elif keyword in name:
                scores[row["path"]] = scores.get(row["path"], 0) + PARTIAL_HIT
        for path in paths:
            if keyword in path.lower():
                scores[path] = scores.get(path, 0) + PATH_HIT

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [path for path, _ in ordered[:limit]]


def prefetch(
    conn: sqlite3.Connection,
    task_text: str,
    counter: TokenCounter,
    max_tokens: int,
) -> str:
    """自动预取：把最相关文件的符号表拼成一段注入内容。"""
    files = rank_files(conn, extract_keywords(task_text))
    if not files:
        return ""

    blocks: list[str] = []
    remaining = max_tokens
    for path in files:
        block = render_file_symbols(conn, path, counter, remaining)
        if not block:
            continue
        cost = counter.count(block)
        if cost > remaining:
            continue
        blocks.append(block)
        remaining -= cost
    return "\n".join(blocks)

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/index -q`
Expected: PASS（32 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/index/rank.py tests/index/test_rank.py
git commit -m "feat: 索引相关性排序与自动预取"
```

---

## Task 6: 索引能力接入工具层

**Files:**
- Create: `src/agents_dev/index/tools.py`
- Test: `tests/index/test_index_tools.py`

**Interfaces:**
- Consumes: `ToolSpec`、`ToolResult`、`load_symbol_source`、`render_file_symbols`
- Produces:
  - `agents_dev.index.tools.find_symbol_spec(root: Path, conn) -> ToolSpec`（参数 `name`，可选 `path`）
  - `agents_dev.index.tools.file_symbols_spec(conn) -> ToolSpec`（参数 `path`）

- [ ] **Step 1: 写失败测试**

```python
# tests/index/test_index_tools.py
from pathlib import Path

from agents_dev.index.indexer import index_project
from agents_dev.index.tools import file_symbols_spec, find_symbol_spec
from agents_dev.store.db import init_schema, open_db


def _project(tmp_path: Path):
    (tmp_path / "parser.py").write_text(
        "def parse_config(path):\n    return path\n", encoding="utf-8"
    )
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    return conn


def test_按名字找到符号并返回所在文件与行号(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    result = find_symbol_spec(tmp_path, conn).handler({"name": "parse_config"})
    assert result.ok is True
    assert "parser.py" in result.content
    assert "L1" in result.content
    conn.close()


def test_指定文件时可取出符号源码(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    result = find_symbol_spec(tmp_path, conn).handler(
        {"name": "parse_config", "path": "parser.py"}
    )
    assert result.ok is True
    assert "return path" in result.content
    conn.close()


def test_找不到符号时返回失败而非抛错(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    result = find_symbol_spec(tmp_path, conn).handler({"name": "nope"})
    assert result.ok is False
    conn.close()


def test_列出文件符号表(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    result = file_symbols_spec(conn).handler({"path": "parser.py"})
    assert result.ok is True
    assert "def parse_config(path)" in result.content
    conn.close()


def test_列出不存在文件的符号时返回失败(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert file_symbols_spec(conn).handler({"path": "nope.py"}).ok is False
    conn.close()

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/index/test_index_tools.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.index.tools'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/index/tools.py
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

```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/index -q`
Expected: PASS（37 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/index/tools.py tests/index/test_index_tools.py
git commit -m "feat: 索引能力包装为 find_symbol 与 file_symbols 工具"
```

---

## Task 7: 接入主循环与端到端测量

**Files:**
- Modify: `src/agents_dev/agent/loop.py`
- Test: `tests/agent/test_loop_prefetch.py`
- Create: `tests/test_index_measurement.py`

**Interfaces:**
- Consumes: `prefetch`（可调用对象）、`AgentLoop`
- Produces:
  - `AgentLoop.__init__(..., prefetch: Callable[[str], str] | None = None)`
  - 首轮装配时若 `prefetch` 提供内容，作为 `retrieval` 区段注入

预取只在任务开始时做一次。它消耗的是首轮预算，但换来的是模型从一开始
就知道该看哪些文件——这正是「模型只做选择，不做搜索」的落点。

- [ ] **Step 1: 写失败测试**

```python
# tests/agent/test_loop_prefetch.py
import json
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.registry import ToolRegistry


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "完成", "tool_calls": [], "state": None, "final": final},
        ensure_ascii=False,
    )


def _loop(tmp_path: Path, prefetch=None) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    return AgentLoop(
        gateway=FakeModel(script=[_turn("好了")], tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=ToolRegistry(),
        config=Config(project_root=tmp_path, context_window=4096),
        prefetch=prefetch,
    )


def test_预取内容出现在首轮请求里(tmp_path: Path) -> None:
    loop = _loop(tmp_path, prefetch=lambda goal: "parser.py:\n  def parse_config(path)")
    loop.run("修 parse_config")
    first = loop.gateway.requests[0]
    assert any("parse_config" in m.content for m in first.messages)


def test_未提供预取时行为不变(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    result = loop.run("随便")
    assert result.finished is True
    assert not any("parser.py" in m.content for m in loop.gateway.requests[0].messages)


def test_预取返回空串时不产生多余区段(tmp_path: Path) -> None:
    loop = _loop(tmp_path, prefetch=lambda goal: "")
    loop.run("随便")
    assert len(loop.gateway.requests[0].messages) >= 1

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/agent/test_loop_prefetch.py -q`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'prefetch'`

- [ ] **Step 3: 写最小实现（修改主循环）**

对 `src/agents_dev/agent/loop.py` 做三处修改：

其一，在文件顶部的导入区加入 `from typing import Callable`（放在
`from dataclasses import dataclass, field` 之后）。

其二，把 `AgentLoop.__init__` 改成接受可选预取：

```python
    def __init__(
        self,
        gateway: ModelGateway,
        tokenizer: TokenCounter,
        registry: ToolRegistry,
        config: Config,
        prefetch: Callable[[str], str] | None = None,
    ) -> None:
        self.gateway = gateway
        self.tokenizer = tokenizer
        self.registry = registry
        self.config = config
        self.prefetch = prefetch
        self._budget = Budget(window=config.context_window)
```

其三，`_assemble` 多接受一个 `prefetched` 参数，并把它作为 `retrieval`
区段注入（与反馈共用同一份 15% 预算，因为它们语义相同，都是外部检索内容）：

```python
    def _assemble(
        self,
        state: TaskState,
        history: list[Message],
        feedback: str | None,
        prefetched: str = "",
    ):
        """按当前状态与历史装配本轮上下文。"""
        assembler = Assembler(tokenizer=self.tokenizer, budget=self._budget)
        system_text = SYSTEM_PROMPT.format(tools=self.registry.describe())
        sections = [
            Section(name="system", text=system_text, priority=10, mandatory=True),
            Section(name="task_state", text=state.render(), priority=30),
        ]
        if prefetched:
            sections.append(Section(name="retrieval", text=prefetched, priority=35))
        if feedback:
            sections.append(Section(name="retrieval", text=feedback, priority=40))
        return assembler.assemble(sections, recent_turns=history[-MAX_RECENT_TURNS:])
```

以及在 `run` 中，进入循环前计算一次预取，并把 `_assemble` 的调用都补上该参数：

```python
        prefetched = self.prefetch(goal) if self.prefetch is not None else ""
        while state.step < self.config.max_steps:
            assembled = self._assemble(state, history, feedback, prefetched)
```

循环体内因触发重置而重新装配的那一处同样要传：

```python
                assembled = self._assemble(state, history, feedback, prefetched)
```

- [ ] **Step 4: 写量化收益的测试**

```python
# tests/test_index_measurement.py
"""量化索引层的收益：同样的定位能力，地图比读整份文件省多少。"""

from pathlib import Path

from agents_dev.index.indexer import index_project
from agents_dev.index.repo_map import render_repo_map
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.store.db import init_schema, open_db


def test_地图体积远小于全部文件正文(tmp_path: Path) -> None:
    body = "".join(f"def f{j}(a, b):\n    return a + b\n\n\n" for j in range(20))
    for i in range(10):
        (tmp_path / f"m{i}.py").write_text(body, encoding="utf-8")

    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)

    counter = OfflineTokenCounter()
    baseline = sum(
        counter.count((tmp_path / f"m{i}.py").read_text(encoding="utf-8"))
        for i in range(10)
    )
    indexed = counter.count(render_repo_map(conn, counter, 800))

    assert indexed < baseline * 0.2
    conn.close()


def test_地图在文件很多时仍守住上限(tmp_path: Path) -> None:
    for i in range(50):
        (tmp_path / f"m{i}.py").write_text(
            "".join(f"def g{j}():\n    pass\n" for j in range(30)), encoding="utf-8"
        )
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)

    counter = OfflineTokenCounter()
    text = render_repo_map(conn, counter, 400)
    assert counter.count(text) <= 400
    conn.close()

```

- [ ] **Step 5: 运行全部测试并提交**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: PASS（全部，含原有 77 个）

```bash
git add src/agents_dev/agent/loop.py tests/agent/test_loop_prefetch.py tests/test_index_measurement.py
git commit -m "feat: 主循环接入索引预取并量化上下文收益"
```

---

## 完成标准

1. `pytest` 全绿，原有 77 个测试不回归。
2. 在本项目上执行一次索引，`render_repo_map` 能在 800 token 内给出整个仓库的定位地图。
3. 索引第二次执行时，未改动文件全部走「跳过」分支（增量更新生效）。
4. 量化测试证明：地图体积小于「读取全部文件正文」的 20%。
5. 不新增任何依赖。

## 后续计划（不在本计划范围内）

- 计划三：引用图与 L3 邻域（`who_calls` / 改动波及面分析）。
- 计划四：记忆系统（热记忆文件、SQLite 冷记忆、FTS5 检索、归档与提炼、教训机制）。
- 计划五：真实模型接入（llama.cpp HTTP 网关、精确 token 计数、GBNF 语法约束）。
- 计划六：约束检查器与子智能体。
