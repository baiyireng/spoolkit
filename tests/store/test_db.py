from pathlib import Path

from spoolkit.store.db import init_schema, open_db


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

