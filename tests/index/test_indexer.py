from pathlib import Path

from spoolkit.index.indexer import index_project
from spoolkit.store.db import init_schema, open_db


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

