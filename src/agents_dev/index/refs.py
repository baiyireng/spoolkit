"""引用图：从抽象语法树提取调用、继承、导入关系。

静态分析有它够不到的地方，这些边界必须在输出里说清楚，不能假装全覆盖：

- getattr、反射、按字符串调用，静态解析不出来；
- 同名符号散落多处时目标无法唯一确定，此时宁可留空也不猜。

留空和「找不到」是两件事：前者是「有但定不了」，后者是「确实没有」。
把两者混为一谈，使用者会把「没找到调用方」误读成「可以放心改」。
"""

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class Ref:
    """一条引用边。src 用限定名表示，便于回填到符号表。"""

    src_qualified: str
    dst_name: str
    kind: str


def qualified(parent: str | None, name: str) -> str:
    return f"{parent}.{name}" if parent else name


class RefExtractor(ast.NodeVisitor):
    """按符号边界遍历，收集引用边。"""

    def __init__(self) -> None:
        self.refs: list[Ref] = []
        self._scope: list[str] = []

    def extract(self, source: str) -> list[Ref]:
        tree = ast.parse(source)
        self.refs = []
        self._scope = []
        self._walk_body(tree.body)
        return self.refs

    # --- 符号边界 ---

    def _walk_body(self, body) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                self._enter_class(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._enter_function(node)

    def _enter_class(self, node: ast.ClassDef) -> None:
        name = qualified(self._scope[-1] if self._scope else None, node.name)
        for base in node.bases:
            target = _dotted_name(base)
            if target:
                self.refs.append(Ref(name, target, "inherit"))
        self._scope.append(node.name)
        self._walk_body(node.body)
        self._scope.pop()

    def _enter_function(self, node) -> None:
        parent = self._scope[-1] if self._scope else None
        name = qualified(parent, node.name)
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                self._record_call(name, child)
        self._scope.append(node.name)
        self._walk_body(node.body)  # 嵌套函数单独成符号边界
        self._scope.pop()

    def _record_call(self, source_name: str, call: ast.Call) -> None:
        func = call.func
        if isinstance(func, ast.Name):
            self.refs.append(Ref(source_name, func.id, "call"))
        elif isinstance(func, ast.Attribute):
            # 属性调用只能拿到方法名，目标类型未知，标成 attr 以示区别。
            self.refs.append(Ref(source_name, func.attr, "attr"))


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def extract_refs(source: str) -> list[Ref]:
    """提取一个文件里的全部引用边。"""
    return RefExtractor().extract(source)

