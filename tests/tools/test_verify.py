"""自动验证：改完自己跑一遍项目测试。

要求有两条，缺一条这个功能就是假的：
1. 测的必须是**改过之后**的代码（所以跑在试跑副本上）；
2. 没有测试的项目不要瞎跑，否则「找不到测试」会被当成失败反馈灌回去。
"""

from pathlib import Path

from agents_dev.tools.edit import PendingChanges
from agents_dev.tools.verify import (
    condense_test_output,
    detect_test_command,
    is_test_path,
    make_verifier,
)

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


def test_改测试骗不过验证(tmp_path: Path) -> None:
    """模型实测会这么干：把断言改成 assert True，验证就「通过」了。

    验证若按模型改过的测试判定，测的就是它希望看到的结论，
    而不是代码的真实表现。
    """
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose(
        "test_mod.py",
        "import mod\n\n\ndef test_v():\n    assert True\n",
    )
    verifier = make_verifier(tmp_path, pending)
    result = verifier()
    assert result.ok is False


def test_改测试时报告里要说明(tmp_path: Path) -> None:
    """悄悄按原始测试判定，模型会以为自己改测试生效了。"""
    _project(tmp_path)
    pending = PendingChanges(tmp_path)
    pending.propose("mod.py", "VALUE = 2\n")
    pending.propose(
        "test_mod.py",
        "import mod\n\n\ndef test_v():\n    assert True\n",
    )
    result = make_verifier(tmp_path, pending)()
    assert result.ok is True
    assert "原来的内容" in result.content


def test_测试路径识别() -> None:
    assert is_test_path("test_mod.py")
    assert is_test_path("mod_test.py")
    assert is_test_path("tests/unit/test_x.py")
    assert is_test_path("test/helpers.py")
    assert not is_test_path("mod.py")
    assert not is_test_path("src/testing.py")


def test_短摘要优先() -> None:
    """一行摘要里同时有文件、用例名和差异，是最能行动的形态。"""
    raw = (
        "退出码 1（失败）\n$ python -m pytest -q\n"
        ".F                                                                       [100%]\n"
        "=================================== FAILURES ===================================\n"
        "_________________________________ test_chinese _________________________________\n"
        "\n"
        "    def test_chinese():\n"
        '>       assert greet("Ann", "zh") == "你好，Ann！"\n'
        "E       AssertionError: assert '你好, Ann!' == '你好，Ann！'\n"
        "\n"
        "test_acceptance.py:9: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED test_acceptance.py::test_chinese - AssertionError: assert '你好, Ann!' ==\n"
        "============================== 1 failed in 0.05s ==============================\n"
    )
    condensed = condense_test_output(raw)
    assert condensed.startswith("FAILED test_acceptance.py::test_chinese")
    assert "你好, Ann!" in condensed
    # 分隔线和进度点不该混进来
    assert "=====" not in condensed


def test_没有短摘要时退回报错行() -> None:
    raw = (
        "退出码 1（失败）\n$ python -m pytest -q\n"
        "==== ERRORS ====\n"
        "E   ModuleNotFoundError: No module named 'helpers'\n"
    )
    assert "ModuleNotFoundError" in condense_test_output(raw)


def test_崩溃输出也能给出点东西() -> None:
    raw = "退出码 2（失败）\n$ python -m pytest -q\npython: can't open file 'x'\n"
    assert "can't open file" in condense_test_output(raw)
