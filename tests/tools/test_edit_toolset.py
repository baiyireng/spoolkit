"""按项目规模决定给哪几把编辑工具。

实测：把行号手术交给 7B，它会算错区间、拼错片段，留下重复行和悬空语句，
8 条失败里 5 条死在 replace_lines 上。而它的强项是整份输出。所以项目里
都是小文件时，干脆只给它 write_file。
"""

from pathlib import Path

from agents_dev.tools.edit import (
    WHOLE_FILE_LINE_LIMIT,
    PendingChanges,
    is_small_project,
    register_edit_tools,
)
from agents_dev.tools.registry import ToolRegistry


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_edit_tools(registry, tmp_path, PendingChanges(tmp_path))
    return registry


def test_小项目只给整份重写(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert is_small_project(tmp_path) is True
    # 行号手术和片段替换都不给：实测这两把都比整份重写更容易把模型带偏，
    # 而整份重写是它最有把握的形态。
    assert _registry(tmp_path).names() == ("write_file",)


def test_有文件超过阈值就两把都给(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "big.py").write_text(
        "y = 1\n" * (WHOLE_FILE_LINE_LIMIT + 10), encoding="utf-8"
    )
    assert is_small_project(tmp_path) is False
    assert set(_registry(tmp_path).names()) == {"replace_lines", "write_file"}


def test_空项目算小项目(tmp_path: Path) -> None:
    assert is_small_project(tmp_path) is True
    assert _registry(tmp_path).names() == ("write_file",)


def test_虚拟环境与索引目录不算数(tmp_path: Path) -> None:
    """别人的依赖不该把「这是个小项目」这个判断带偏。"""
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "huge.py").write_text("z = 1\n" * 2000, encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert is_small_project(tmp_path) is True
