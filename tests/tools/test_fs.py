from pathlib import Path

from agents_dev.tools.fs import list_dir_spec, read_file_spec


def test_读取整个文件(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    result = read_file_spec(tmp_path).handler({"path": "a.txt"})
    assert result.ok is True
    assert "第一行" in result.content
    assert "第二行" in result.content


def test_按行区间读取(tmp_path: Path) -> None:
    body = "".join(f"line{i}\n" for i in range(1, 11))
    (tmp_path / "a.txt").write_text(body, encoding="utf-8")
    result = read_file_spec(tmp_path).handler(
        {"path": "a.txt", "start_line": 3, "end_line": 4}
    )
    assert result.ok is True
    assert "line3" in result.content
    assert "line4" in result.content
    assert "line5" not in result.content


def test_读取不存在的文件返回失败(tmp_path: Path) -> None:
    result = read_file_spec(tmp_path).handler({"path": "nope.txt"})
    assert result.ok is False
    assert "不存在" in result.content


def test_读取越界路径失败(tmp_path: Path) -> None:
    assert read_file_spec(tmp_path).handler({"path": "../secret.txt"}).ok is False


def test_读取目录返回失败(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    assert read_file_spec(tmp_path).handler({"path": "d"}).ok is False


def test_行号区间非法时失败(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    result = read_file_spec(tmp_path).handler(
        {"path": "a.txt", "start_line": 5, "end_line": 2}
    )
    assert result.ok is False


def test_列目录返回文件名(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    result = list_dir_spec(tmp_path).handler({"path": "pkg"})
    assert result.ok is True
    assert "m.py" in result.content


def test_列目录遇到非目录返回失败(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    assert list_dir_spec(tmp_path).handler({"path": "a.txt"}).ok is False

