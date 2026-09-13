"""目录统计：先量再判断。

Agent 原先只能一个一个看，没有"量一下"的能力。问到「哪个目录最大」时它
只能顺口编一个——实测就这么干了。没有测量工具时，任何模型都只能猜。
"""

from pathlib import Path

from spoolkit.tools.stats import dir_stats_spec


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "big").mkdir(parents=True)
    (root / "small").mkdir()
    (root / "big" / "a.bin").write_bytes(b"x" * 5000)
    (root / "big" / "b.py").write_text("x = 1\n", encoding="utf-8")
    (root / "small" / "c.txt").write_text("hi\n", encoding="utf-8")
    return root


def test_给出总量与子项分布(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    result = dir_stats_spec(tmp_path).handler({"path": str(root)})
    assert result.ok is True
    assert "3 个文件" in result.content
    # 大的子项排在前面
    assert result.content.index("big") < result.content.index("small")


def test_按类型与最大文件(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    content = dir_stats_spec(tmp_path).handler({"path": str(root)}).content
    assert ".bin" in content and ".py" in content
    assert "a.bin" in content


def test_工作区之外需要授权(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.txt").write_text("hi\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    blocked = dir_stats_spec(project).handler({"path": str(outside)})
    assert blocked.ok is False
    assert "--allow-read" in blocked.content

    allowed = dir_stats_spec(project, (outside,)).handler({"path": str(outside)})
    assert allowed.ok is True


def test_不是目录时拒绝(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    result = dir_stats_spec(tmp_path).handler({"path": "f.txt"})
    assert result.ok is False


def test_撞到预算时明说不完整(tmp_path: Path) -> None:
    """不完整的统计被当成完整的用，比没有统计更糟。"""
    from spoolkit.tools.stats import _collect, _render

    root = _tree(tmp_path)
    # 用负数而不是 0.0：判定是 `elapsed > seconds`，而 Windows 上 time.time()
    # 的精度按 Python 版本不同（3.12 上第一次迭代可能还是 0.0 秒），
    # 用 0.0 会让这条断言变成"看时钟精度"。
    data = _collect(root, seconds=-1.0)
    assert data["truncated"] is True
    assert "不完整" in _render(root, data, 5)
