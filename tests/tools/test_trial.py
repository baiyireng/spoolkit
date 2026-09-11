from pathlib import Path

from agents_dev.tools.edit import PendingChanges, replace_lines_spec
from agents_dev.tools.exec import run_command_spec
from agents_dev.tools.trial import copy_project, trial_workspace


def _project(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    for heavy in (".venv", ".git", "__pycache__"):
        target = tmp_path / heavy
        target.mkdir()
        (target / "junk.txt").write_text("x", encoding="utf-8")


def test_副本排除虚拟环境与缓存(tmp_path: Path) -> None:
    _project(tmp_path)
    target = tmp_path / "_copy"
    copy_project(tmp_path, target)
    assert (target / "calc.py").exists()
    assert not (target / ".venv").exists()
    assert not (target / ".git").exists()
    assert not (target / "__pycache__").exists()


def test_副本里已应用未落盘的改动(tmp_path: Path) -> None:
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    replace_lines_spec(tmp_path, pending).handler(
        {"path": "calc.py", "start_line": 2, "end_line": 2, "content": "    return a + b\n"}
    )
    with trial_workspace(tmp_path, pending.items()) as work:
        assert "a + b" in (work / "calc.py").read_text(encoding="utf-8")
    # 真实文件不受影响
    assert "a - b" in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_试跑目录退出后被清理(tmp_path: Path) -> None:
    _project(tmp_path)
    with trial_workspace(tmp_path, []) as work:
        base = work.parent
    assert not base.exists()


def test_有未落盘改动时测试跑在副本上(tmp_path: Path) -> None:
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    replace_lines_spec(tmp_path, pending).handler(
        {"path": "calc.py", "start_line": 2, "end_line": 2, "content": "    return a + b\n"}
    )
    result = run_command_spec(tmp_path, pending).handler(
        {"command": ["python", "-m", "pytest", "-q"], "timeout": 120}
    )
    # 改动尚未落盘，但试跑环境里已经生效，所以测试应当通过
    assert result.ok is True, result.content
    assert "1 passed" in result.content


def test_真实文件在试跑后仍是旧内容(tmp_path: Path) -> None:
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    replace_lines_spec(tmp_path, pending).handler(
        {"path": "calc.py", "start_line": 2, "end_line": 2, "content": "    return a + b\n"}
    )
    run_command_spec(tmp_path, pending).handler(
        {"command": ["python", "-m", "pytest", "-q"], "timeout": 120}
    )
    assert "a - b" in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_没有待落盘改动时在原地执行(tmp_path: Path) -> None:
    _project(tmp_path)
    result = run_command_spec(tmp_path, PendingChanges(tmp_path)).handler(
        {"command": ["python", "-m", "pytest", "-q"], "timeout": 120}
    )
    # 原文件有 bug，测试应当失败——说明确实跑在原地
    assert result.ok is False
    assert "1 failed" in result.content

