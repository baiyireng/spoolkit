"""按项目规模决定给哪几把编辑工具。

实测：把行号手术交给 7B，它会算错区间、拼错片段，留下重复行和悬空语句，
8 条失败里 5 条死在 replace_lines 上。而它的强项是整份输出。所以项目里
都是小文件时，干脆只给它 write_file。

`confirm_rewrite` 不在此列：它不交内容，是「正文已暂存、清单还没交代完」
那一回合的入口，**永远跟着 write_file 注册**（少了它，暂存下去的正文就没人
能确认）。所以下面按「交内容的写工具」比对，把确认入口单独钉住。

`delete_file` 也不在此列：它同样**不交内容**（只登记一处删除），而且它与
项目规模无关——合并/去重类任务里「把多余那一份删掉」在任何规模的项目上都
成立，所以它永远跟着写工具注册，由 `test_删除工具与写工具一起注册` 单独钉。
"""

from pathlib import Path

from spoolkit.tools.edit import (
    CONFIRM_TOOL,
    DELETE_TOOL,
    WHOLE_FILE_LINE_LIMIT,
    PendingChanges,
    is_small_project,
    register_edit_tools,
)
from spoolkit.tools.registry import ToolRegistry


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_edit_tools(registry, tmp_path, PendingChanges(tmp_path))
    return registry


def _content_tools(tmp_path: Path) -> tuple[str, ...]:
    """注册表里**交内容**的那几把（确认入口与删除不算）。"""
    return tuple(
        name
        for name in _registry(tmp_path).names()
        if name not in (CONFIRM_TOOL, DELETE_TOOL)
    )


def test_删除工具与写工具一起注册(tmp_path: Path) -> None:
    """删除不按项目规模分档：任何规模的合并/去重任务都需要它。

    实测缺它的后果很具体（merge-html 首轮）：模型把内容合并好了，却只能
    建议用户自己去 `rm pricing.html`，用户那句「以后我就看一个文件」落不了地。
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert is_small_project(tmp_path) is True
    assert DELETE_TOOL in _registry(tmp_path).names()

    (tmp_path / "big.py").write_text(
        "y = 1\n" * (WHOLE_FILE_LINE_LIMIT + 10), encoding="utf-8"
    )
    assert is_small_project(tmp_path) is False
    assert DELETE_TOOL in _registry(tmp_path).names()


def test_小项目只给整份重写(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert is_small_project(tmp_path) is True
    # 行号手术和片段替换都不给：实测这两把都比整份重写更容易把模型带偏，
    # 而整份重写是它最有把握的形态。
    assert _content_tools(tmp_path) == ("write_file",)
    assert CONFIRM_TOOL in _registry(tmp_path).names()


def test_有文件超过阈值就两把都给(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "big.py").write_text(
        "y = 1\n" * (WHOLE_FILE_LINE_LIMIT + 10), encoding="utf-8"
    )
    assert is_small_project(tmp_path) is False
    assert set(_content_tools(tmp_path)) == {"replace_lines", "write_file"}


def test_空项目算小项目(tmp_path: Path) -> None:
    assert is_small_project(tmp_path) is True
    assert _content_tools(tmp_path) == ("write_file",)


def test_虚拟环境与索引目录不算数(tmp_path: Path) -> None:
    """别人的依赖不该把「这是个小项目」这个判断带偏。"""
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "huge.py").write_text("z = 1\n" * 2000, encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert is_small_project(tmp_path) is True
