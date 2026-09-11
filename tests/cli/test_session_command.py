from pathlib import Path

from agents_dev.cli.app import main
from agents_dev.memory.store import init_memory_schema, record_session
from agents_dev.store.db import open_db


def _seed(tmp_path: Path, *names: str) -> None:
    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    conn = open_db(tmp_path / ".agent" / "memory.db")
    init_memory_schema(conn)
    for index, name in enumerate(names):
        record_session(
            conn, name, str(tmp_path), model=f"gemini:m{index}", context_limit=8192
        )
    conn.close()


def test_列出会话(tmp_path: Path, capsys) -> None:
    _seed(tmp_path, "morning", "bugfix")
    assert main(["session", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "morning" in out
    assert "bugfix" in out
    assert "共 2 个会话" in out


def test_没有数据库时明确报错(tmp_path: Path, capsys) -> None:
    assert main(["session", "--root", str(tmp_path)]) == 2
    assert "还没有任何会话" in capsys.readouterr().err


def test_有库但没有会话时提示(tmp_path: Path, capsys) -> None:
    (tmp_path / ".agent").mkdir(parents=True, exist_ok=True)
    conn = open_db(tmp_path / ".agent" / "memory.db")
    init_memory_schema(conn)
    conn.close()
    assert main(["session", "--root", str(tmp_path)]) == 2
    assert "还没有会话记录" in capsys.readouterr().err


def test_显示模型与窗口(tmp_path: Path, capsys) -> None:
    _seed(tmp_path, "work")
    main(["session", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "gemini:m0" in out
    assert "8192" in out

