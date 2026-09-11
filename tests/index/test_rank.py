from pathlib import Path

from agents_dev.index.indexer import index_project
from agents_dev.index.rank import (
    extract_keywords,
    prefetch,
    prefetch_contents,
    rank_files,
)
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.store.db import init_schema, open_db


def _project(tmp_path: Path):
    (tmp_path / "parser.py").write_text(
        "def parse_config(path):\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "render.py").write_text(
        "def draw_screen():\n    pass\n", encoding="utf-8"
    )
    conn = open_db(tmp_path / "index.db")
    init_schema(conn)
    index_project(tmp_path, conn)
    return conn


def test_关键词提取去掉英文停用词() -> None:
    words = extract_keywords("请帮我 fix the parse_config 函数")
    assert "parse_config" in words
    assert "the" not in words


def test_关键词提取去掉重复() -> None:
    assert extract_keywords("parse_config parse_config") == ["parse_config"]


def test_命中的文件排在前面(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    ranked = rank_files(conn, ["parse_config"])
    assert ranked[0] == "parser.py"
    conn.close()


def test_文件路径命中也能被选中(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert "render.py" in rank_files(conn, ["render"])
    conn.close()


def test_无命中时返回空列表(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert rank_files(conn, ["完全不相关的词汇zzz"]) == []
    conn.close()


def test_预取结果包含相关文件符号表(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    text = prefetch(conn, "修复 parse_config", OfflineTokenCounter(), 300)
    assert "parser.py" in text
    assert "def parse_config(path)" in text
    conn.close()


def test_预取遵守token上限(tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"m{i}.py").write_text(
            "".join(f"def target_{j}():\n    pass\n" for j in range(20)),
            encoding="utf-8",
        )
    conn = _project(tmp_path)
    counter = OfflineTokenCounter()
    text = prefetch(conn, "target", counter, 150)
    assert text
    assert counter.count(text) <= 150
    conn.close()


def test_无关键词时预取返回空串(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    assert prefetch(conn, "。。。", OfflineTokenCounter(), 300) == ""
    conn.close()


def test_内容预取给出文件正文(tmp_path: Path) -> None:
    """符号表只说「有什么」，正文才说「怎么写的」。

    实测本地 7B 只有符号表时会一直查、始终不读文件；给了正文它一轮就能改对。
    """
    conn = _project(tmp_path)
    text = prefetch_contents(
        conn, tmp_path, "修复 parse_config", OfflineTokenCounter()
    )
    assert "parser.py" in text
    assert "def parse_config(path):" in text
    assert "pass" in text
    conn.close()


def test_内容预取跳过过大的文件(tmp_path: Path) -> None:
    """大文件本来就该用 read_file 按行取，不能整个塞进上下文。"""
    (tmp_path / "huge.py").write_text(
        "def target():\n    pass\n" + "# 填充\n" * 4000, encoding="utf-8"
    )
    conn = _project(tmp_path)
    text = prefetch_contents(
        conn, tmp_path, "修复 target", OfflineTokenCounter(), max_files=1
    )
    assert text == ""
    conn.close()


def test_内容预取受总数上限约束(tmp_path: Path) -> None:
    conn = _project(tmp_path)
    counter = OfflineTokenCounter()
    full = prefetch_contents(conn, tmp_path, "parse_config render", counter)
    tiny = prefetch_contents(
        conn, tmp_path, "parse_config render", counter, max_tokens=5
    )
    assert len(tiny) < len(full)
    conn.close()
