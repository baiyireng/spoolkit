"""计算工具。

模型自己做算术，结果没人能验证。这不是「它不擅长」的问题，而是
**算术不该由它做**：一个 7B 模型在上下文里做加法，等于让它在没有纸笔的
情况下心算一长串数，而且错了看不出来——实测它把 5.1 GB 的目录说成 8 GB、
把 1.2 GB 的一个 dll 说成整个 venv。

数字只有两个合法来源：**工具返回的**，和**程序算出来的**。这个工具负责
后者，而且只求值表达式——用 AST 白名单，不执行任意代码。要跑真正的脚本
（遍历、聚合、格式化），用 write_file 写进 .agent/scratch/ 再 run_command
运行，那条路一样不碰真实工作区。
"""

import ast
import operator
from typing import Any

from spoolkit.tools.types import ToolResult, ToolSpec

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS: dict[str, Any] = {
    "sum": sum,
    "min": min,
    "max": max,
    "len": len,
    "abs": abs,
    "round": round,
}


class CalcError(ValueError):
    """表达式不合法或用了不允许的东西。"""


def evaluate(expression: str) -> Any:
    """求值一个纯表达式。不认识的东西一律拒绝，不做兜底。"""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"表达式语法错误: {exc.msg}") from exc
    return _eval(tree.body)


def _eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)):
            return node.value
        raise CalcError(f"不支持的常量类型: {type(node.value).__name__}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        return _BINARY[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(item) for item in node.elts]
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise CalcError("只允许调用 sum / min / max / len / abs / round")
        if node.keywords:
            raise CalcError("不支持关键字参数")
        return _FUNCTIONS[node.func.id](*[_eval(arg) for arg in node.args])
    if isinstance(node, ast.Name):
        # 不给变量：没有「上一次的结果」这种上下文，算术就该是自包含的。
        raise CalcError(f"不认识的名字: {node.id}（表达式里不能引用变量）")
    raise CalcError(f"不支持的写法: {type(node).__name__}")


def _calc(args: dict) -> ToolResult:
    expression = str(args.get("expression") or "").strip()
    if not expression:
        return ToolResult(ok=False, content="expression 不能为空")
    try:
        value = evaluate(expression)
    except CalcError as exc:
        return ToolResult(ok=False, content=str(exc))
    except ZeroDivisionError:
        return ToolResult(ok=False, content="除数为零")
    except (TypeError, ValueError, OverflowError) as exc:
        return ToolResult(ok=False, content=f"算不了: {exc}")
    return ToolResult(ok=True, content=f"{expression} = {value}")


def calc_spec() -> ToolSpec:
    """表达式求值。"""
    return ToolSpec(
        name="calc",
        description=(
            "算一个表达式，结果由程序给出而不是你心算。"
            "报告里要引用的任何合计、差值、比例，都先用它算一遍；"
            "更复杂的计算（遍历、聚合、格式化）改用 write_file 写进 "
            ".agent/scratch/ 再 run_command 运行"
        ),
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "例如 13.1 + 2.0 + 7.5，或 sum([6.6, 6.5])",
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        handler=_calc,
        brief="算数（别心算）",
        group="量",
    )
