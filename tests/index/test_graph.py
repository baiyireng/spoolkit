from pathlib import Path

from agents_dev.index.graph import (
    callers,
    callees,
    impact,
    resolve_refs,
    unresolved_count,
)
from agents_dev.index.indexer import index_project
from agents_dev.index.refs import extract_refs
from agents_dev.store.db import init_schema, open_db


def _project(tmp_path: Path, files: dict[str, str]):
    for name, body in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    return conn


def _symbol_id(conn, name: str, path: str | None = None) -> int:
    if path:
        row = conn.execute(
            "SELECT s.id AS id FROM symbol s JOIN file f ON f.id = s.file_id"
            " WHERE s.name = ? AND f.path = ?",
            (name, path),
        ).fetchone()
    else:
        row = conn.execute("SELECT id FROM symbol WHERE name = ?", (name,)).fetchone()
    assert row is not None, f"找不到符号 {name}"
    return row["id"]


def test_提取同文件函数调用() -> None:
    refs = extract_refs("def a():\n    b()\n\n\ndef b():\n    pass\n")
    assert [r for r in refs if r.kind == "call"] == [Ref_of("a", "b")]


def Ref_of(src: str, dst: str):  # noqa: N802 - 测试辅助，保持可读
    from agents_dev.index.refs import Ref

    return Ref(src, dst, "call")


def test_提取继承边() -> None:
    refs = extract_refs("class A:\n    pass\n\n\nclass B(A):\n    pass\n")
    assert any(r.kind == "inherit" and r.dst_name == "A" for r in refs)


def test_提取方法调用标记为attr() -> None:
    refs = extract_refs("class A:\n    def m(self):\n        self.helper()\n")
    assert any(r.kind == "attr" and r.dst_name == "helper" for r in refs)


def test_同文件调用被解析到具体符号(tmp_path: Path) -> None:
    conn = _project(
        tmp_path,
        {"m.py": "def b():\n    pass\n\n\ndef a():\n    b()\n"},
    )
    assert [r.name for r in callers(conn, _symbol_id(conn, "b"))] == ["a"]
    conn.close()


def test_反向也能查到调用目标(tmp_path: Path) -> None:
    conn = _project(
        tmp_path,
        {"m.py": "def b():\n    pass\n\n\ndef a():\n    b()\n"},
    )
    assert [r.name for r in callees(conn, _symbol_id(conn, "a"))] == ["b"]
    conn.close()


def test_同文件优先于全局同名(tmp_path: Path) -> None:
    conn = _project(
        tmp_path,
        {
            "one.py": "def helper():\n    pass\n\n\ndef caller():\n    helper()\n",
            "two.py": "def helper():\n    pass\n",
        },
    )
    target = _symbol_id(conn, "helper", "one.py")
    assert [r.name for r in callers(conn, target)] == ["caller"]
    conn.close()


def test_全局唯一时可解析跨文件调用(tmp_path: Path) -> None:
    conn = _project(
        tmp_path,
        {"util.py": "def helper():\n    pass\n", "main.py": "def run():\n    helper()\n"},
    )
    assert [r.name for r in callers(conn, _symbol_id(conn, "helper"))] == ["run"]
    conn.close()


def test_多义时留空而不猜(tmp_path: Path) -> None:
    conn = _project(
        tmp_path,
        {
            "a.py": "def dup():\n    pass\n",
            "b.py": "def dup():\n    pass\n",
            "c.py": "def caller():\n    dup()\n",
        },
    )
    assert callers(conn, _symbol_id(conn, "dup", "a.py")) == []
    assert unresolved_count(conn, _symbol_id(conn, "dup", "a.py")) == 1
    conn.close()


def test_波及面按深度限制(tmp_path: Path) -> None:
    conn = _project(
        tmp_path,
        {
            "m.py": (
                "def c():\n    pass\n\n\ndef b():\n    c()\n\n\ndef a():\n    b()\n"
            )
        },
    )
    found, _, _ = impact(conn, _symbol_id(conn, "c"), depth=1)
    assert [r.name for r in found] == ["b"]
    found2, _, _ = impact(conn, _symbol_id(conn, "c"), depth=2)
    assert [r.name for r in found2] == ["b", "a"]
    conn.close()


def test_波及面受数量上限约束(tmp_path: Path) -> None:
    body = "def target():\n    pass\n\n\n"
    body += "".join(f"def f{i}():\n    target()\n\n\n" for i in range(10))
    conn = _project(tmp_path, {"m.py": body})
    found, truncated, _ = impact(conn, _symbol_id(conn, "target"), depth=1, limit=3)
    assert len(found) == 3
    assert truncated is True
    conn.close()


def test_无引用时报未解析为零(tmp_path: Path) -> None:
    conn = _project(tmp_path, {"m.py": "def lonely():\n    pass\n"})
    assert unresolved_count(conn, _symbol_id(conn, "lonely")) == 0
    conn.close()


def test_重复解析不产生重复边(tmp_path: Path) -> None:
    conn = _project(
        tmp_path, {"m.py": "def b():\n    pass\n\n\ndef a():\n    b()\n"}
    )
    before = conn.execute("SELECT COUNT(*) AS n FROM ref").fetchone()["n"]
    resolve_refs(conn)
    after = conn.execute("SELECT COUNT(*) AS n FROM ref").fetchone()["n"]
    assert before == after
    conn.close()

