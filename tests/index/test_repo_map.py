from pathlib import Path

from spoolkit.index.indexer import index_project
from spoolkit.index.repo_map import (
    load_symbol_source,
    render_file_symbols,
    render_repo_map,
)
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.store.db import init_schema, open_db


def _project(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        "def alpha(x: int) -> int:\n"
        '    """返回两倍。"""\n'
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
