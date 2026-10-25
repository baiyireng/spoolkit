"""编辑回执的信封：summary 给模型，raw 留下完整 diff。"""

from pathlib import Path

from spoolkit.tools.edit import PendingChanges, replace_lines_spec, write_file_spec


def _big(tmp_path: Path) -> tuple[PendingChanges, str]:
    text = "".join(f"y = {index}\n" for index in range(400))
    (tmp_path / "big.py").write_text(text, encoding="utf-8")
    return PendingChanges(tmp_path), text


def test_replace_lines改两行时信封分开diff(tmp_path: Path) -> None:
    pending, text = _big(tmp_path)
    lines = text.splitlines()
    result = replace_lines_spec(tmp_path, pending).handler(
        {
            "path": "big.py",
            "start_line": 10,
            "end_line": 11,
            "content": "y = 1000\ny = 1001\n",
            "expect": "y = 9\ny = 10",
        }
    )
    change = pending.items()[0]

    assert result.ok is True
    assert change.diff in result.raw
    assert change.diff in result.content
    assert "@@" in change.diff
    assert result.summary
    assert "big.py" in result.summary
    assert "400→400" in result.summary
    assert "+2 −2" in result.summary
    assert "待确认" in result.summary
    assert "下一步" in result.summary
    assert change.diff not in result.summary
    assert result.data["path"] == "big.py"
    assert result.data["start"] == 10
    assert result.data["end"] == 11
    assert result.data["lines_before"] == 400
    assert result.data["lines_after"] == 400
    assert result.artifacts == tuple(result.artifacts)
    assert result.artifacts[0].type == "diff"
    assert result.artifacts[0].path == "big.py"
    assert change.diff == pending.items()[0].diff
    assert lines[0] not in result.summary
    assert "y = 1000" not in (tmp_path / "big.py").read_text(encoding="utf-8")


def test_write_file覆盖时同样拆信封(tmp_path: Path) -> None:
    pending, text = _big(tmp_path)
    new = text.replace("y = 9\n", "y = 1000\n", 1).replace("y = 10\n", "y = 1001\n", 1)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "big.py", "content": new}
    )
    change = pending.items()[0]
    assert change.diff in result.raw
    assert change.diff in result.content
    assert change.diff not in result.summary
    assert "big.py" in result.summary
    assert "待确认" in result.summary
    assert result.data["lines_before"] == 400
    assert result.data["lines_after"] == 400
    assert "start" not in result.data
    assert result.artifacts[0].type == "diff"
    assert result.artifacts[0].path == "big.py"
