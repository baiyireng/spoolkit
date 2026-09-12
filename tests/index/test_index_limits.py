"""索引的代价上限。

索引是可选增强，不能无上限地做。实测一台机器上的 40 万文件目录树里，
光遍历就 4.4 秒（而且跳过名单当时根本没生效），全量解析要几分钟。
这些上限保证它最坏也就是「不做」，而不是把一次运行拖垮。
"""

from pathlib import Path

from agents_dev.cli.runtime import attach_index
from agents_dev.index.indexer import (
    MAX_INDEX_FILES,
    index_project,
    iter_source_files,
)
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.store.db import init_schema, open_db
from agents_dev.tools.registry import ToolRegistry


def _many(tmp_path: Path, count: int) -> None:
    for index in range(count):
        (tmp_path / f"m{index}.py").write_text("def f():\n    return 1\n", encoding="utf-8")


def test_遍历时剪枝不进跳过目录(tmp_path: Path) -> None:
    """原先用 rglob 再过滤：整棵树都走一遍，跳过名单等于没生效。"""
    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    for name in (".venv", "node_modules", "__pycache__"):
        buried = tmp_path / name
        buried.mkdir()
        (buried / "hidden.py").write_text("x = 1\n", encoding="utf-8")
    found = [path.name for path in iter_source_files(tmp_path)]
    assert found == ["keep.py"]


def test_超过文件上限就停下并说明原因(tmp_path: Path) -> None:
    _many(tmp_path, 12)
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    stats = index_project(tmp_path, conn, max_files=5)
    assert stats.files_scanned == 5
    assert "上限" in stats.stopped
    conn.close()


def test_超过时间预算就停下(tmp_path: Path) -> None:
    _many(tmp_path, 12)
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    stats = index_project(tmp_path, conn, seconds=0.0)
    assert stats.stopped
    conn.close()


def test_半途停下时不删已有记录(tmp_path: Path) -> None:
    """清理「已不存在的文件」只能在走完整棵树时做。

    提前退出时 seen 是不全的，照删会把没走到的文件全删掉——
    那是把上限变成了数据损坏。
    """
    _many(tmp_path, 12)
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)  # 先完整建一次
    before = conn.execute("SELECT COUNT(*) FROM file").fetchone()[0]
    assert before == 12

    index_project(tmp_path, conn, max_files=3)  # 这次只走 3 个
    after = conn.execute("SELECT COUNT(*) FROM file").fetchone()[0]
    assert after == before, "半途停下不该删掉没走到的文件"
    conn.close()


def test_没有上限时才会清理消失的文件(tmp_path: Path) -> None:
    _many(tmp_path, 4)
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    (tmp_path / "m0.py").unlink()
    index_project(tmp_path, conn)
    assert conn.execute("SELECT COUNT(*) FROM file").fetchone()[0] == 3
    conn.close()


def test_大树直接跳过且不留空壳(tmp_path: Path) -> None:
    """先数一遍再决定做不做——数一遍很便宜，建索引按文件数花钱。

    顺序也必须对：开库会顺手建出 .agent 目录，于是「跳过了索引」
    反而在用户的项目里留下一个空壳。
    """
    _many(tmp_path, MAX_INDEX_FILES + 1)
    registry = ToolRegistry()
    prefetch = attach_index(tmp_path, registry, OfflineTokenCounter())

    assert registry.names() == (), "跳过了就不该注册只覆盖一半的索引工具"
    assert not (tmp_path / ".agent").exists(), "跳过时不该留下空壳"
    text = prefetch("随便什么目标")
    assert "索引不可用" in text
    assert "超过上限" in text


def test_正常大小的项目照常建索引(tmp_path: Path) -> None:
    _many(tmp_path, 3)
    registry = ToolRegistry()
    prefetch = attach_index(tmp_path, registry, OfflineTokenCounter())
    assert set(registry.names()) == {"find_symbol", "file_symbols", "find_callers"}
    assert "索引不可用" not in prefetch("随便")
