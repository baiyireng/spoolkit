import sqlite3
from pathlib import Path

from agents_dev.memory.store import (
    check_binding,
    get_session,
    init_memory_schema,
    record_session,
)
from agents_dev.store.db import open_db


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def test_建表后包含会话表(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "session" in names
    conn.close()


def test_登记会话记录工作区(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_session(conn, "work", str(tmp_path), model="gemini", context_limit=8192)
    row = get_session(conn, "work")
    assert row is not None
    assert row["project_root"] == str(tmp_path)
    assert row["model"] == "gemini"
    conn.close()


def test_重复登记更新模型与时间但不改绑定(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_session(conn, "work", str(tmp_path), model="a", context_limit=100)
    record_session(conn, "work", str(tmp_path), model="b", context_limit=200)
    row = get_session(conn, "work")
    assert row["model"] == "b"
    assert row["context_limit"] == 200
    assert row["project_root"] == str(tmp_path)
    conn.close()


def test_读取不存在的会话返回空(tmp_path: Path) -> None:
    assert get_session(_conn(tmp_path), "nope") is None


def test_首次登记不产生绑定警告(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    assert check_binding(conn, "work", str(tmp_path)) is None
    conn.close()


def test_同一工作区不产生警告(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_session(conn, "work", str(tmp_path))
    assert check_binding(conn, "work", str(tmp_path)) is None
    conn.close()


def test_换了工作区会给出警告(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    other = tmp_path / "another"
    other.mkdir()
    record_session(conn, "work", str(other))
    warning = check_binding(conn, "work", str(tmp_path))
    assert warning is not None
    assert "work" in warning
    assert str(other) in warning
    conn.close()


def test_登记的模型与窗口可被覆盖(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_session(
        conn, "s", str(tmp_path), model="llamacpp", context_limit=4096
    )
    row = get_session(conn, "s")
    assert (row["model"], row["context_limit"]) == ("llamacpp", 4096)
    conn.close()

