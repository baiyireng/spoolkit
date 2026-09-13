"""计算工具：算术不该由模型心算。

实测它把 5.1 GB 的目录说成 8 GB、把 1.2 GB 的一个 dll 说成整个 venv。
不是它算不对，而是**没人能核实它算得对不对**——除非数字来自程序。
"""

import pytest

from spoolkit.tools.calc import CalcError, calc_spec, evaluate


def test_基本算术() -> None:
    assert evaluate("1 + 2 * 3") == 7
    assert evaluate("(1 + 2) * 3") == 9
    assert evaluate("10 / 4") == 2.5
    assert evaluate("-3 + 1") == -2


def test_合计这类最常见的用法() -> None:
    assert evaluate("6.6 + 6.5") == pytest.approx(13.1)
    assert evaluate("sum([6.6, 6.5])") == pytest.approx(13.1)
    assert evaluate("round(sum([6.6, 6.5]), 1)") == pytest.approx(13.1)


def test_不执行任意代码() -> None:
    """求值可以，执行不行。"""
    for expression in (
        "__import__('os').system('echo hi')",
        "open('x').read()",
        "print(1)",
        "x + 1",
        "[i for i in range(3)]",
    ):
        with pytest.raises(CalcError):
            evaluate(expression)


def test_语法错误说清楚() -> None:
    with pytest.raises(CalcError) as caught:
        evaluate("1 +")
    assert "语法错误" in str(caught.value)


def test_工具返回算式与结果() -> None:
    result = calc_spec().handler({"expression": "5.1 + 3.2"})
    assert result.ok is True
    assert "= 8.3" in result.content, "要回显算式，结果才可追溯"


def test_空表达式与除零都被拒() -> None:
    assert calc_spec().handler({"expression": "  "}).ok is False
    assert calc_spec().handler({"expression": "1 / 0"}).ok is False
