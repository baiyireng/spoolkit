"""符号提取。

当前实现基于标准库 ast，只支持 Python。接口刻意设计成可替换，
将来接入 tree-sitter 支持多语言时，索引器与渲染层无需改动。

有意不索引嵌套函数：它们没有稳定的对外身份，索引它们只会让
符号表变成噪声，而噪声直接消耗上下文预算。
"""

import ast
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Symbol:
    """代码中的一个符号。行号从 1 开始，区间为闭区间。"""

    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str
    parent: str | None = None
    doc: str = ""


class SymbolExtractor(Protocol):
    """符号提取器接口。"""

    def extract(self, source: str, path: str) -> list[Symbol]:
        """从源码中提取符号。"""
        ...


def _format_arg(node: ast.arg) -> str:
    if node.annotation is None:
        return node.arg
    return f"{node.arg}: {ast.unparse(node.annotation)}"


def _unparse(node: ast.expr) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # 极少数节点无法反解析时退化为占位
        return "…"


def _format_params(args: ast.arguments) -> list[str]:
    parts = [_format_arg(a) for a in list(args.posonlyargs) + list(args.args)]
    defaults = [_unparse(d) for d in args.defaults]
    if defaults:
        offset = len(parts) - len(defaults)
        for index, text in enumerate(defaults):
            parts[offset + index] = f"{parts[offset + index]} = {text}"
    if args.vararg is not None:
        parts.append("*" + _format_arg(args.vararg))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        text = _format_arg(arg)
        if default is not None:
            text = f"{text} = {_unparse(default)}"
        parts.append(text)
    if args.kwarg is not None:
        parts.append("**" + _format_arg(args.kwarg))
    return parts


def _function_signature(node) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    params = ", ".join(_format_params(node.args))
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({params}){ret}"


def _class_signature(node: ast.ClassDef) -> str:
    bases = [_unparse(b) for b in node.bases]
    return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"


def _start_line(node) -> int:
    """有装饰器时从装饰器起算，保证后续按行取源码时不会漏掉它们。"""
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *[d.lineno for d in decorators]])


class PythonAstExtractor:
    """基于标准库 ast 的 Python 符号提取器。"""

    def extract(self, source: str, path: str) -> list[Symbol]:
        tree = ast.parse(source, filename=path)
        found: list[Symbol] = []
        self._walk(tree.body, None, found)
        return found

    def _walk(self, body, parent: str | None, out: list[Symbol]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                out.append(
                    Symbol(
                        name=node.name,
                        kind="class",
                        start_line=_start_line(node),
                        end_line=node.end_lineno or node.lineno,
                        signature=_class_signature(node),
                        parent=parent,
                        doc=ast.get_docstring(node) or "",
                    )
                )
                self._walk(node.body, node.name, out)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(
                    Symbol(
                        name=node.name,
                        kind="method" if parent else "function",
                        start_line=_start_line(node),
                        end_line=node.end_lineno or node.lineno,
                        signature=_function_signature(node),
                        parent=parent,
                        doc=ast.get_docstring(node) or "",
                    )
                )
                # 不递归进函数体：嵌套函数不进索引

