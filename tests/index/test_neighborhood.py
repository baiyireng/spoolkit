from pathlib import Path

from spoolkit.index.indexer import index_project
from spoolkit.index.repo_map import render_neighborhood
from spoolkit.index.tools import find_callers_spec
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.store.db import init_schema, open_db


def _project(tmp_path: Path, files: dict[str, str]):
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    return conn


def _symbol_id(conn, name: str) -> int:
    return conn.execute("SELECT id FROM symbol WHERE name = ?", (name,)).fetchone()["id"]


def test_邻域列出引用方(tmp_path: Path) -> None:
    conn = _project(
        tmp_path, {"m.py": "def b():\n    pass\n\n\ndef a():\n    b()\n"}
    )
    text = render_neighborhood(
        conn, _symbol_id(conn, "b"), OfflineTokenCounter(), 400
    )
    assert "def b()" in text
    assert "a" in text
    conn.close()


def test_无引用方时明确说明(tmp_path: Path) -> None:
    conn = _project(tmp_path, {"m.py": "def lonely():\n    pass\n"})
    text = render_neighborhood(
        conn, _symbol_id(conn, "lonely"), OfflineTokenCounter(), 400
    )
    assert "没有静态可解析的引用方" in text
    conn.close()


def test_邻域列出它引用的符号(tmp_path: Path) -> None:
    conn = _project(
        tmp_path, {"m.py": "def b():\n    pass\n\n\ndef a():\n    b()\n"}
    )
    text = render_neighborhood(
        conn, _symbol_id(conn, "a"), OfflineTokenCounter(), 400
    )
    assert "它引用了" in text
    conn.close()


def test_无法确定的引用会被说明(tmp_path: Path) -> None:
    conn = _project(
        tmp_path,
        {
            "a.py": "def dup():\n    pass\n",
            "b.py": "def dup():\n    pass\n",
            "c.py": "def caller():\n    dup()\n",
        },
    )
    text = render_neighborhood(
        conn, _symbol_id(conn, "dup"), OfflineTokenCounter(), 400
    )
    assert "无法唯一确定" in text
    conn.close()


def test_邻域遵守token上限(tmp_path: Path) -> None:
    body = "def target():\n    pass\n\n\n"
    body += "".join(f"def f{i}():\n    target()\n\n\n" for i in range(40))
    conn = _project(tmp_path, {"m.py": body})
    counter = OfflineTokenCounter()
    text = render_neighborhood(conn, _symbol_id(conn, "target"), counter, 60)
    assert counter.count(text) <= 60
    assert "省略" in text
    conn.close()


def test_工具返回引用方与波及面(tmp_path: Path) -> None:
    conn = _project(
        tmp_path,
        {
            "m.py": (
                "def c():\n    pass\n\n\ndef b():\n    c()\n\n\ndef a():\n    b()\n"
            )
        },
    )
    result = find_callers_spec(conn).handler({"name": "c"})
    assert result.ok is True
    assert "波及" in result.content
    assert "a" in result.content
    conn.close()


def test_工具对同名符号要求指定路径(tmp_path: Path) -> None:
    conn = _project(
        tmp_path, {"a.py": "def dup():\n    pass\n", "b.py": "def dup():\n    pass\n"}
    )
    result = find_callers_spec(conn).handler({"name": "dup"})
    assert result.ok is False
    assert "path" in result.content
    conn.close()


def test_工具找不到符号时明确失败(tmp_path: Path) -> None:
    conn = _project(tmp_path, {"m.py": "def a():\n    pass\n"})
    assert find_callers_spec(conn).handler({"name": "nope"}).ok is False
    conn.close()

