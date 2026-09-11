from pathlib import Path

from agents_dev.tools.search import search_code_spec


def test_含非GBK字符的文件也能被搜索(tmp_path: Path) -> None:
    """Windows 默认用本地编码解码子进程输出，超出该编码的字符会让整个搜索挂掉。"""
    (tmp_path / "a.py").write_text(
        "TARGET_MARKER = '带特殊字符的内容 ✦ 〇'\n", encoding="utf-8"
    )
    result = search_code_spec(tmp_path).handler({"pattern": "TARGET_MARKER"})
    assert result.ok is True
    assert "TARGET_MARKER" in result.content


def test_搜索中文内容不会挂掉(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("# 中文注释里的目标词\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "目标词"})
    assert result.ok is True
    assert "目标词" in result.content

