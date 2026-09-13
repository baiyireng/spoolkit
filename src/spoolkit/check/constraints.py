"""结构约束检查器。

小模型不会遵守提示词里的软性约束。「请保持函数简短」它看一眼就忘，
或者嘴上答应然后写出 80 行的函数。但只要给出具体的、指向明确的反馈
——「_parse_file 有 62 行，超过 40 行上限，建议把第 41 行之后的分支抽出去」
——它就能改。

所以这一层的全部意义是：把小模型的软要求翻译成硬反馈。

约束加在函数级而不是文件级：决定小模型成功率的，是它一次要生成和处理的
最小单元有多大，而这个单元是函数。文件行数只是个很弱的症状指标，
所以只给警告、不阻断。
"""

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """结构约束的阈值。全部是起始值，需要靠实测调整。"""

    function_lines: int = 40
    parameters: int = 5
    nesting_depth: int = 3
    file_symbols: int = 20
    file_lines: int = 600


@dataclass(frozen=True)
class Violation:
    """一条违反项。severity 为 warning 的只提示、不要求阻断。"""

    rule: str
    target: str
    message: str
    severity: str = "error"


def _parameter_count(node) -> int:
    args = node.args
    total = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
    if args.vararg is not None:
        total += 1
    if args.kwarg is not None:
        total += 1
    # 方法定义里的 self/cls 不算实际参数
    if args.args and args.args[0].arg in ("self", "cls"):
        total -= 1
    return total


def _check_functions(source: str, limits: Limits) -> list[Violation]:
    tree = ast.parse(source)
    found: list[Violation] = []

    def walk(body, depth: int, parent: str | None) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                walk(node.body, depth, node.name if parent is None else f"{parent}.{node.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{parent}.{node.name}" if parent else node.name
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                if length > limits.function_lines:
                    found.append(
                        Violation(
                            rule="function_lines",
                            target=name,
                            message=(
                                f"{name} 有 {length} 行，超过 {limits.function_lines} 行上限。"
                                f"第 {node.lineno + limits.function_lines} 行之后的内容"
                                "可以考虑抽成独立函数。"
                            ),
                        )
                    )
                count = _parameter_count(node)
                if count > limits.parameters:
                    found.append(
                        Violation(
                            rule="parameters",
                            target=name,
                            message=(
                                f"{name} 有 {count} 个参数，超过 {limits.parameters} 个上限。"
                                "参数越多，调用时错位的概率越高。"
                            ),
                        )
                    )
                if depth > limits.nesting_depth:
                    found.append(
                        Violation(
                            rule="nesting_depth",
                            target=name,
                            message=(
                                f"{name} 的嵌套深度为 {depth}，超过 {limits.nesting_depth} 层上限。"
                            ),
                        )
                    )
                walk(node.body, depth + 1, parent)

    walk(tree.body, 1, None)
    return found


def check_source(
    source: str,
    path: str = "<memory>",
    limits: Limits | None = None,
) -> list[Violation]:
    """检查一段源码。语法错误时返回一条 error，不抛异常。"""
    active = limits or Limits()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [
            Violation(
                rule="syntax",
                target=path,
                message=f"语法错误：第 {exc.lineno} 行 {exc.msg}",
            )
        ]

    found = _check_functions(source, active)

    symbols = sum(
        1
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    if symbols > active.file_symbols:
        found.append(
            Violation(
                rule="file_symbols",
                target=path,
                message=(
                    f"文件有 {symbols} 个顶级符号，超过 {active.file_symbols} 个上限，"
                    "建议按职责拆分。"
                ),
            )
        )

    lines = len(source.splitlines())
    if lines > active.file_lines:
        found.append(
            Violation(
                rule="file_lines",
                target=path,
                message=(
                    f"文件有 {lines} 行，超过 {active.file_lines} 行上限。"
                    "这通常意味着它承担了本该属于多个模块的职责。"
                ),
                severity="warning",
            )
        )
    return found


def blocking(violations: list[Violation]) -> list[Violation]:
    """只保留需要模型修正的项。"""
    return [item for item in violations if item.severity != "warning"]


def format_violations(violations: list[Violation]) -> str:
    """格式化成可以直接回灌给模型的反馈。"""
    if not violations:
        return ""
    lines = ["结构约束检查发现以下问题，请修正："]
    lines.extend(f"- {item.message}" for item in violations)
    return "\n".join(lines)

