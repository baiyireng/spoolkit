import pytest

from agents_dev.index.symbols import PythonAstExtractor, Symbol


def _extract(source: str) -> list[Symbol]:
    return PythonAstExtractor().extract(source, "m.py")


def _by_name(symbols: list[Symbol], name: str) -> Symbol:
    return next(s for s in symbols if s.name == name)


def test_提取顶层函数() -> None:
    symbols = _extract("def foo(a, b):\n    return a\n")
    assert [s.name for s in symbols] == ["foo"]
    assert symbols[0].kind == "function"
    assert symbols[0].start_line == 1
    assert symbols[0].end_line == 2


def test_签名包含参数与返回类型() -> None:
    symbols = _extract("def foo(a: int, b: str = 'x') -> bool:\n    return True\n")
    assert symbols[0].signature == "def foo(a: int, b: str = 'x') -> bool"


def test_提取类及其方法并标记父子关系() -> None:
    source = "class A:\n    def m(self):\n        pass\n"
    symbols = _extract(source)
    assert _by_name(symbols, "A").kind == "class"
    method = _by_name(symbols, "m")
    assert method.kind == "method"
    assert method.parent == "A"


def test_类签名包含基类() -> None:
    symbols = _extract("class B(Base, Mixin):\n    pass\n")
    assert symbols[0].signature == "class B(Base, Mixin)"


def test_异步函数被识别() -> None:
    symbols = _extract("async def fetch():\n    pass\n")
    assert symbols[0].name == "fetch"
    assert symbols[0].signature.startswith("async def fetch")


def test_带装饰器时起始行覆盖装饰器() -> None:
    source = "@deco\ndef foo():\n    pass\n"
    assert _extract(source)[0].start_line == 1


def test_提取文档字符串() -> None:
    symbols = _extract('def foo():\n    """说明文字。"""\n    pass\n')
    assert "说明文字" in symbols[0].doc


def test_嵌套函数不进索引() -> None:
    source = "def outer():\n    def inner():\n        pass\n"
    assert [s.name for s in _extract(source)] == ["outer"]


def test_语法错误时抛出可识别的异常() -> None:
    with pytest.raises(SyntaxError):
        _extract("def broken(:\n")


def test_空文件返回空列表() -> None:
    assert _extract("") == []

