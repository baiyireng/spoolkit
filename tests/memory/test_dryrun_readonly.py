"""dry-run 的只读性：不动 `memory.md` / `memory.db`，也不建库。

为什么单独一组：`spool memory migrate` 是**唯一**会改热记忆文件的命令，而它
默认就是预演。预演只要用了"可写打开"那条既有路径（`store/db.py:open_db` 会
`mkdir` 再 `connect`），跑一次就会创建空的 `memory.db`、或在库里留下 `-wal`
——"看一眼就把库动了一遍"。这组用例把只读这件事钉在**字节与目录清单**上，
不钉实现细节：谁把 `open_db_readonly` 换回 `open_db`，这里就红。
"""

import hashlib
import os
from pathlib import Path

from spoolkit.cli.app import main
from spoolkit.memory.store import init_memory_schema, open_db_readonly
from spoolkit.store.db import open_db

from tests.memory.test_migrate_command import FACT, _workspace


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mtime(path: Path) -> float:
    return path.stat().st_mtime


def _listing(root: Path) -> list[str]:
    return sorted(item.name for item in (root / ".agent").iterdir())


def _fresh_db(path: Path) -> None:
    """造一个结构完整的库（`memory` 表 + `memory_fts` 都在）。"""
    conn = open_db(path)
    init_memory_schema(conn)
    conn.close()


def test_open_db_readonly_库不存在时不创建(tmp_path: Path) -> None:
    path = tmp_path / "nope" / "memory.db"
    assert not path.exists()

    assert open_db_readonly(path) is None

    assert not path.exists()
    assert not path.parent.exists()  # 连目录都不建


def test_open_db_readonly_不开出wal和shm(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    _fresh_db(path)
    before = sorted(p.name for p in tmp_path.iterdir())

    conn = open_db_readonly(path)
    assert conn is not None
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()

    assert "memory" in tables
    # 只读打开**不新增任何文件**（`-wal`/`-shm` 也算新增）。
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / "memory.db-shm").exists()


def test_open_db_readonly_拒绝写入(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    _fresh_db(path)

    conn = open_db_readonly(path)
    assert conn is not None
    # 只读靠 **URI 的 `mode=ro`**（SQLite 层彻底拒绝写），不是靠 `query_only`
    # 那个开关：`mode=ro` 下 SQLite 不会把 `PRAGMA query_only` 读成 1，所以
    # 那条断言是错的（实测返回 0）。这里钉的是真正生效的那一层——写操作
    # 必须失败，且失败原因能读出"readonly"。
    try:
        conn.execute("CREATE TABLE nope (id INTEGER)")
    except Exception as exc:  # sqlite3.OperationalError: attempt to write a readonly database
        assert "readonly" in str(exc).lower() or "read-only" in str(exc).lower()
    else:  # pragma: no cover - 真发生就是"只读"没生效
        raise AssertionError("只读连接居然写成功了")
    finally:
        conn.close()


def test_dry_run不改动memory_db(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    db = root / ".agent" / "memory.db"
    _fresh_db(db)
    before = (_sha(db), _mtime(db))
    before_listing = _listing(root)

    assert main(["memory", "migrate", "--root", str(root)]) == 0

    assert (_sha(db), _mtime(db)) == before
    # 目录里没有新增文件（尤其没有 -wal / -shm / .bak）。
    assert _listing(root) == before_listing


def test_dry_run不改动memory_md_与mtime(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    md = root / ".agent" / "memory.md"
    before = (_sha(md), _mtime(md), os.stat(md).st_size)

    assert main(["memory", "migrate", "--dry-run", "--root", str(root)]) == 0

    assert (_sha(md), _mtime(md), os.stat(md).st_size) == before
    assert _listing(root) == ["memory.md"]


def test_dry_run不带yes时读的库是只读连接(tmp_path: Path, monkeypatch) -> None:
    """把 `open_db`（可写）挂上探针：dry-run 一次都不该用到它。"""
    from spoolkit.cli.commands import memory as memory_cmd
    import spoolkit.store.db as db_mod

    root = _workspace(tmp_path)
    calls: list[str] = []
    real_open = db_mod.open_db

    def spy_open(path):
        calls.append(str(path))
        return real_open(path)

    monkeypatch.setattr(db_mod, "open_db", spy_open)
    monkeypatch.setattr(memory_cmd, "open_db_readonly", memory_cmd.open_db_readonly)

    assert main(["memory", "migrate", "--root", str(root)]) == 0

    assert calls == []
    assert not (root / ".agent" / "memory.db").exists()


def test_库里有同一条时不重复收进热记忆_且不写库(tmp_path: Path) -> None:
    """「库里已有」这一栏是**只读**核对：它不许 touch、不许写行。"""
    from spoolkit.memory.store import add_memory

    root = _workspace(tmp_path)
    db = root / ".agent" / "memory.db"
    conn = open_db(db)
    init_memory_schema(conn)
    add_memory(conn, kind="fact", text=FACT)
    conn.close()
    before = (_sha(db), _mtime(db))

    assert main(["memory", "migrate", "--root", str(root)]) == 0

    assert (_sha(db), _mtime(db)) == before
    assert "memory.db-wal" not in _listing(root)
