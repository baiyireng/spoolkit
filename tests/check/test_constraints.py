from pathlib import Path

from agents_dev.check.constraints import (
    Limits,
    blocking,
    check_source,
    format_violations,
)
from agents_dev.tools.edit import PendingChanges, write_file_spec


def _rules(source: str, limits: Limits | None = None) -> set[str]:
    return {v.rule for v in check_source(source, limits=limits)}


def test_合规代码没有违反项() -> None:
    source = "def f(a, b):\n    return a + b\n"
    assert check_source(source) == []


def test_函数过长被报出() -> None:
    body = "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(50))
    assert "function_lines" in _rules(body)


def test_过长反馈里包含函数名与建议位置() -> None:
    body = "def long_one():\n" + "".join(f"    x{i} = {i}\n" for i in range(50))
    message = check_source(body)[0].message
    assert "long_one" in message
    assert "抽成独立函数" in message


def test_参数过多被报出() -> None:
    assert "parameters" in _rules("def f(a, b, c, d, e, g):\n    pass\n")


def test_self不计入参数个数() -> None:
    source = "class A:\n    def m(self, a, b, c, d, e):\n        pass\n"
    assert "parameters" not in _rules(source)


def test_嵌套过深被报出() -> None:
    source = (
        "def a():\n"
        "    def b():\n"
        "        def c():\n"
        "            def d():\n"
        "                pass\n"
    )
    assert "nesting_depth" in _rules(source)


def test_顶级符号过多被报出() -> None:
    source = "".join(f"def f{i}():\n    pass\n\n\n" for i in range(25))
    assert "file_symbols" in _rules(source)


def test_文件过长只给警告() -> None:
    source = "\n".join(f"x{i} = {i}" for i in range(700)) + "\n"
    issues = check_source(source)
    assert [v.rule for v in issues] == ["file_lines"]
    assert issues[0].severity == "warning"


def test_警告不进入阻塞列表() -> None:
    source = "\n".join(f"x{i} = {i}" for i in range(700)) + "\n"
    assert blocking(check_source(source)) == []


def test_语法错误被报为一条错误() -> None:
    issues = check_source("def broken(:\n")
    assert issues[0].rule == "syntax"
    assert issues[0].severity == "error"


def test_阈值可调整() -> None:
    source = "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(10))
    assert check_source(source, limits=Limits(function_lines=5))[0].rule == "function_lines"
    assert check_source(source, limits=Limits(function_lines=99)) == []


def test_无违反项时格式化为空串() -> None:
    assert format_violations([]) == ""


def test_反馈文本可直接回灌() -> None:
    source = "def f(a, b, c, d, e, g):\n    pass\n"
    text = format_violations(check_source(source))
    assert "请修正" in text
    assert "f" in text


def test_方法限定名包含类名() -> None:
    source = (
        "class A:\n"
        "    def long_method(self):\n" + "".join(f"        x{i} = {i}\n" for i in range(50))
    )
    assert "A.long_method" in check_source(source)[0].target


def test_写操作提案附带约束反馈(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path)
    long_body = "def long_one():\n" + "".join(f"    x{i} = {i}\n" for i in range(50))
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "a.py", "content": long_body}
    )
    assert result.ok is True
    assert "结构约束检查" in result.content
    assert "long_one" in result.content


def test_合规提案不附加噪声(tmp_path: Path) -> None:
    pending = PendingChanges(tmp_path)
    result = write_file_spec(tmp_path, pending).handler(
        {"path": "a.py", "content": "def f():\n    return 1\n"}
    )
    assert "结构约束检查" not in result.content

