"""量化索引层的收益：同样的定位能力，地图比读整份文件省多少。"""

from pathlib import Path

from spoolkit.index.indexer import index_project
from spoolkit.index.repo_map import render_repo_map
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.store.db import init_schema, open_db


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

