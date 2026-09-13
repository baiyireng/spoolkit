from pathlib import Path

from spoolkit.index.indexer import index_project
from spoolkit.index.tools import file_symbols_spec, find_symbol_spec
from spoolkit.store.db import init_schema, open_db


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
    assert find_symbol_spec(tmp_path, conn).handler({"name": "nope"}).ok is False
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

