from pathlib import Path

from agents_dev.tools.search import search_code_spec


def test_搜索命中并返回文件名(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "def target"})
    assert result.ok is True
    assert "a.py" in result.content


def test_无命中时返回空结果而非失败(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "绝不存在的标识符xyzzy"})
    assert result.ok is True
    assert result.content.strip() == ""


def test_搜索不逃出项目根(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_probe.py"
    outside.write_text("SECRET_MARKER = 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "SECRET_MARKER"})
    assert "SECRET_MARKER" not in result.content

