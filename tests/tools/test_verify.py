"""自动验证：改完自己跑一遍项目测试。

要求有两条，缺一条这个功能就是假的：
1. 测的必须是**改过之后**的代码（所以跑在试跑副本上）；
2. 没有测试的项目不要瞎跑，否则「找不到测试」会被当成失败反馈灌回去。
"""

from pathlib import Path

from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.verify import (
    condense_test_output,
    detect_test_command,
    exit_code,
    is_test_path,
    looks_environmental,
    make_verifier,
    mentions_workspace,
    scope_for_change,
)
from spoolkit.tools.types import ToolResult

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


# --- 验证范围跟着改动走 -----------------------------------------------


def _two_tasks(tmp_path: Path) -> None:
    """一个工作区里两件事，各有各的测试：一件会过、一件没过。"""
    for name, value in (("13_case_insensitive", 2), ("14_inclusive_range", 1)):
        home = tmp_path / name
        home.mkdir()
        (home / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        (home / "test_acceptance.py").write_text(
            f"import mod\n\n\ndef test_v():\n    assert mod.VALUE == {value}\n",
            encoding="utf-8",
        )


def test_范围取最近的带测试的那一层(tmp_path: Path) -> None:
    _two_tasks(tmp_path)
    assert scope_for_change(tmp_path, "13_case_insensitive/mod.py") == "13_case_insensitive"
    # 单项目工作区：根目录自己有测试 → 还是根
    (tmp_path / "test_root.py").write_text("def test_ok():\n    assert True\n")
    assert scope_for_change(tmp_path, "anything/deep/mod.py") == "."


def test_只验改动那一处_别人的失败不报给它(tmp_path: Path) -> None:
    """这条是这次改动的全部意义：改 A 时不该收到 B 的失败。

    实测在 50 题的铺盘里，自动验证跑的是整条命令，于是每改完一道就报
    「失败」（另外 41 道还没做），反馈与它刚做的事无关。
    """
    _two_tasks(tmp_path)
    pending = PendingChanges(tmp_path)
    verifier = make_verifier(tmp_path, pending)

    # 只改会过的那一件
    pending.propose("13_case_insensitive/mod.py", "VALUE = 2\n")
    result = verifier(["13_case_insensitive/mod.py"])
    assert result.ok is True, result.content
    assert "13_case_insensitive" in result.content

    # 只改不会过的那一件：报的是它自己的失败，而且指明范围
    pending.propose("14_inclusive_range/mod.py", "VALUE = 9\n")
    result = verifier(["14_inclusive_range/mod.py"])
    assert result.ok is False
    assert "14_inclusive_range" in result.content
    assert "13_case_insensitive" not in result.content


def test_一次改多处就逐处报(tmp_path: Path) -> None:
    """对得上因果：哪一处通过、哪一处没过，一眼看得出是哪一个猜错了。"""
    _two_tasks(tmp_path)
    pending = PendingChanges(tmp_path)
    verifier = make_verifier(tmp_path, pending)
    pending.propose("13_case_insensitive/mod.py", "VALUE = 2\n")
    pending.propose("14_inclusive_range/mod.py", "VALUE = 9\n")

    result = verifier(
        ["13_case_insensitive/mod.py", "14_inclusive_range/mod.py"]
    )
    assert result.ok is False
    assert "改了 2 处" in result.content
    assert "1 处通过" in result.content
    assert "14_inclusive_range" in result.content


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


def test_退出码能被取出来() -> None:
    assert exit_code("退出码 1（失败）\n$ pytest\n") == 1
    assert exit_code("退出码 0（成功）\n$ pytest\n") == 0
    assert exit_code("没有退出码的一行") is None


def test_退出码分不出测试没过与没跑成() -> None:
    """实测：收集阶段被权限错误打断时，pytest 返回的也是 1。

    曾经按「非 1 即环境问题」判过，那条判据根本不会触发——记在这里，
    免得再走一遍。
    """
    assert exit_code("退出码 1（失败）\n") == 1
    assert exit_code("退出码 5（失败）\n") == 5


def test_报错指向工作区外的算环境问题(tmp_path: Path) -> None:
    outside = (
        "退出码 1（失败）\n$ pytest\n"
        "ERROR tests/x.py::test_a - PermissionError: [WinError 5] "
        "拒绝访问。: 'C:\\Users\\someone\\AppData\\Local\\Temp\\pytest-of-x'\n"
    )
    assert looks_environmental(outside, tmp_path) is True


def test_报错指向工作区内的算代码问题(tmp_path: Path) -> None:
    inside = (
        "退出码 1（失败）\n$ pytest\n"
        f"{tmp_path}\\mod.py:1: in <module>\n"
        "E   ModuleNotFoundError: No module named 'helpers'\n"
    )
    assert looks_environmental(inside, tmp_path) is False


def test_没有环境特征时不算环境问题(tmp_path: Path) -> None:
    plain = "退出码 1（失败）\n$ pytest\nFAILED test_x.py::test_a - AssertionError\n"
    assert looks_environmental(plain, tmp_path) is False


def test_一个测试都没收集到算环境问题(tmp_path: Path) -> None:
    assert looks_environmental("退出码 5（失败）\n$ pytest\n", tmp_path) is True


def test_工作区路径识别(tmp_path: Path) -> None:
    assert mentions_workspace(f"see {tmp_path}\\a.py:3", tmp_path) is True
    assert mentions_workspace("see C:\\Windows\\Temp\\a.py:3", tmp_path) is False


def test_环境问题与代码问题给出不同反馈(tmp_path: Path) -> None:
    """环境问题要明确说「不是你的代码」，否则模型会去查自己的改动。

    实测踩过：环境坏了导致 pytest 收集失败，反馈却说「测试失败」，
    模型于是拿着 PermissionError 去查自己的改动，白烧好几步。
    """
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(
        "import mod\n\n\ndef test_v():\n    assert mod.VALUE == 2\n", encoding="utf-8"
    )

    import spoolkit.tools.verify as module

    original = module.run_once
    pending = PendingChanges(tmp_path)

    module.run_once = lambda *a, **k: ToolResult(
        ok=False, content="退出码 1（失败）\n$ pytest\nFAILED test_mod.py::test_v - AssertionError\n"
    )
    try:
        result = make_verifier(tmp_path, pending)()
        assert "测试没有通过" in result.content
        assert "不要改测试" in result.content
    finally:
        module.run_once = original

    module.run_once = lambda *a, **k: ToolResult(
        ok=False,
        content=(
            "退出码 1（失败）\n$ pytest\n"
            "ERROR tests/t.py::test_a - PermissionError: [WinError 5] 拒绝访问。\n"
        ),
    )
    try:
        result = make_verifier(tmp_path, pending)()
        assert "没能跑起来" in result.content
        assert "不是你的代码造成的" in result.content
    finally:
        module.run_once = original


def test_环境坏了之后不再重跑(tmp_path: Path) -> None:
    """结论不会变，而每跑一次都要往上下文里再灌一遍同样的报错。

    实测一次运行里它被重报了 7 次——模型明知道不可行动，还是每次都收到。
    """
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(
        "import mod\n\n\ndef test_v():\n    assert mod.VALUE == 2\n", encoding="utf-8"
    )
    import spoolkit.tools.verify as module

    calls = []
    original = module.run_once

    def fake(*args, **kwargs):
        calls.append(1)
        return ToolResult(
            ok=False,
            content=(
                "退出码 1（失败）\n$ pytest\n"
                "ERROR tests/t.py::test_a - PermissionError: 拒绝访问。\n"
            ),
        )

    module.run_once = fake
    try:
        verify = make_verifier(tmp_path, PendingChanges(tmp_path))
        first = verify()
        assert "没能跑起来" in first.content
        second = verify()
        assert "依然是坏的" in second.content
        assert len(calls) == 1, "环境确认坏了之后不该再跑命令"
    finally:
        module.run_once = original
