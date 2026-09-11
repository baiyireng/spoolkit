"""自动验证：改完自己跑一遍项目测试。

要求有两条，缺一条这个功能就是假的：
1. 测的必须是**改过之后**的代码（所以跑在试跑副本上）；
2. 没有测试的项目不要瞎跑，否则「找不到测试」会被当成失败反馈灌回去。
"""

from pathlib import Path

from agents_dev.tools.edit import PendingChanges
from agents_dev.tools.verify import detect_test_command, make_verifier

FAILING = "import mod\n\n\ndef test_v():\n    assert mod.VALUE == 2\n"


def _project(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(FAILING, encoding="utf-8")


def test_没有测试就不给验证器(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert detect_test_command(tmp_path) is None
    assert make_verifier(tmp_path, PendingChanges(tmp_path)) is None


def test_有测试才给验证器(tmp_path: Path) -> None:
    _project(tmp_path)
    assert detect_test_command(tmp_path) is not None
    assert make_verifier(tmp_path, PendingChanges(tmp_path)) is not None


def test_tests目录也算有测试(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    assert detect_test_command(tmp_path) is not None


def test_未改动时验证报告失败(tmp_path: Path) -> None:
    _project(tmp_path)
    verifier = make_verifier(tmp_path, PendingChanges(tmp_path))
    result = verifier()
    assert result.ok is False
    assert "assert" in result.content or "failed" in result.content.lower()


def test_验证跑的是改动后的代码(tmp_path: Path) -> None:
    """这条是这个功能的意义所在：不改动的话，验证只会告诉你本来就知道的事。"""
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    verifier = make_verifier(tmp_path, pending)

    assert verifier().ok is False
    pending.propose("mod.py", "VALUE = 2\n")
    assert verifier().ok is True


def test_验证不写真实文件(tmp_path: Path) -> None:
    """跑在副本上，真实工作区全程不动——失败要能重来。"""
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose("mod.py", "VALUE = 2\n")
    make_verifier(tmp_path, pending)()
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"
