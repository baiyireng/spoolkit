"""符号提取：Python 走 AST，其它语言走声明扫描。

为什么必须有后者：**只认 Python 的索引等于没有索引**——工作区是什么语言，
事先不知道。一个 .js / .go / .rs 的项目里符号表为空，就意味着预取没内容、
查符号工具永远说"找不到"，而"找不到"会被读成"不存在"。

两条口径必须写清楚（宁可少给信息，也不能给错信息）：

- Python 用标准库 ast，准确；引用图（谁调用了谁）也只对 Python 成立。
- 其它语言用**行首声明扫描**：认得常见声明形态，行号是保守估计
  （花括号语言按花括号配平，缩进语言按缩进收尾，认不准就只报一行）。
  它够支撑"这个文件里有什么、大概在第几行"，不足以支撑"改这里安全吗"。

有意不索引嵌套函数：它们没有稳定的对外身份，索引它们只会让符号表
变成噪声，而噪声直接消耗上下文预算。
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


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


# --- 其它语言：行首声明扫描 ---------------------------------------------
#
# 这一层是**best effort**，所以两条纪律：
#   1) 只在行首匹配（允许缩进与常见修饰符），避免把调用点当声明；
#   2) 行号认不准就只报一行——错的区间比没有区间更有害（find_symbol 会按
#      区间切源码交给模型）。

# 结束行估计的两种口径。
BRACE = "brace"    # 花括号配平：{} 语言
INDENT = "indent"  # 缩进收尾：Ruby / Lua 这类
NONE = "none"      # 认不准就一行

MAX_SYMBOL_LINES = 400


@dataclass(frozen=True)
class LanguageSpec:
    """一种语言的索引口径。

    `code` 用来决定索引的**先后**（不是取舍）：文件数上限是硬的，代码文件
    要先占到名额，md/json 这类只在还有余量时才进——它们的价值是让路径命中
    更完整，而不是让人去查里面的"符号"。
    """

    name: str
    suffixes: tuple[str, ...]
    factory: Callable[[], "SymbolExtractor"]
    blocks: str = BRACE
    code: bool = True


@dataclass(frozen=True)
class Pattern:
    """一条声明形态。kind 是符号类别，组 1 是名字。"""

    regex: re.Pattern[str]
    kind: str


def _pattern(body: str, kind: str) -> Pattern:
    """把声明主体包成「行首 + 常见修饰符 + 主体」。"""
    modifiers = (
        r"(?:export\s+|default\s+|declare\s+|pub\s+|pub\([^)]*\)\s+|"
        r"public\s+|private\s+|protected\s+|internal\s+|static\s+|final\s+|"
        r"abstract\s+|open\s+|sealed\s+|partial\s+|async\s+|unsafe\s+|"
        r"extern\s+|inline\s+|constexpr\s+)*"
    )
    return Pattern(re.compile(r"^[ \t]*" + modifiers + body), kind)


DECLARATION_PATTERNS: tuple[Pattern, ...] = (
    _pattern(r"class\s+([A-Za-z_$][\w$]*)", "class"),
    _pattern(r"interface\s+([A-Za-z_$][\w$]*)", "interface"),
    _pattern(r"struct\s+([A-Za-z_$][\w$]*)", "struct"),
    _pattern(r"enum\s+(?:class\s+)?([A-Za-z_$][\w$]*)", "enum"),
    _pattern(r"trait\s+([A-Za-z_$][\w$]*)", "trait"),
    _pattern(r"record\s+([A-Za-z_$][\w$]*)", "record"),
    _pattern(r"impl(?:<[^>]*>)?\s+([A-Za-z_$][\w$]*)", "impl"),
    _pattern(r"namespace\s+([A-Za-z_$][\w$.]*)", "namespace"),
    _pattern(r"module\s+([A-Za-z_$][\w$]*)", "module"),
    # Go 的写法是「type X struct / interface」，不是行首 struct。
    _pattern(r"type\s+([A-Za-z_]\w*)\s+struct", "struct"),
    _pattern(r"type\s+([A-Za-z_]\w*)\s+interface", "interface"),
    _pattern(r"type\s+([A-Za-z_]\w*)\s+(?:func|map|chan|\[)", "type"),
    # 函数：各语言的写法各一条，靠修饰符前缀统一。
    _pattern(r"function\s+([A-Za-z_$][\w$]*)", "function"),
    _pattern(r"func\s+(?:\([^)]*\)\s*)?([A-Za-z_$][\w$]*)", "function"),
    _pattern(r"fn\s+([A-Za-z_][\w]*)", "function"),
    _pattern(r"def\s+([A-Za-z_][\w?!]*)", "function"),
    _pattern(r"sub\s+([A-Za-z_][\w]*)", "function"),
    # JS/TS 的箭头函数与函数表达式。
    _pattern(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
        "function",
    ),
    _pattern(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function",
        "function",
    ),
    _pattern(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*class", "class"),
    # TS 的类型别名 / 预处理器宏：不是函数，但查符号时很有用。
    _pattern(r"type\s+([A-Za-z_$][\w$]*)\s*=", "type"),
    Pattern(re.compile(r"^[ \t]*#\s*define\s+([A-Za-z_]\w*)"), "macro"),
    # C/C++ 风格的函数定义：`int foo(void) {`。排除控制关键字——
    # 否则 `if (x) {`、`for (…) {` 会被当成函数，符号表立刻变噪音。
    Pattern(
        re.compile(
            r"^[ \t]*(?!(?:if|for|while|switch|catch|return|sizeof|do|else)\b)"
            r"[A-Za-z_][\w \t*&:<>,]*?\b([A-Za-z_]\w*)\s*\([^;{()]*\)"
            # 花括号可以在下一行（K&R 风格）；行尾是 `;` 的声明不匹配。
            r"\s*(?:const\s*)?(?:\{|$)"
        ),
        "function",
    ),
)


class DeclarationExtractor:
    """行首声明扫描。给没有 AST 的语言用。"""

    def __init__(self, patterns: tuple[Pattern, ...] = DECLARATION_PATTERNS,
                 blocks: str = BRACE) -> None:
        self.patterns = patterns
        self.blocks = blocks

    def extract(self, source: str, path: str) -> list[Symbol]:
        lines = source.splitlines()
        found: list[Symbol] = []
        seen: set[tuple[str, int]] = set()
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "#", "*", "/*")):
                continue
            for pattern in self.patterns:
                match = pattern.regex.match(line)
                if match is None:
                    continue
                name = match.group(1)
                if (name, number) in seen:
                    break
                seen.add((name, number))
                found.append(
                    Symbol(
                        name=name,
                        kind=pattern.kind,
                        start_line=number,
                        end_line=self._end_line(lines, number),
                        signature=stripped[:120],
                    )
                )
                break
        return found

    def _end_line(self, lines: list[str], start: int) -> int:
        if self.blocks == BRACE:
            return _brace_end(lines, start)
        if self.blocks == INDENT:
            return _indent_end(lines, start)
        return start


def _brace_end(lines: list[str], start: int) -> int:
    """花括号配平。字符串/注释里的花括号会干扰，所以这是个估计。

    两种要在开头就判掉的形状：**单行声明**（`export type ID = string;`、
    `interface Opt { a: number }`）不能去扫后面的花括号——那样会把下一个
    类或函数的区间算到它头上；而 K&R 风格（花括号写在下一行）必须继续扫。
    """
    first = lines[start - 1]
    if "{" not in first and first.rstrip().endswith((";", "}")):
        return start
    depth = 0
    opened = False
    limit = min(len(lines), start - 1 + MAX_SYMBOL_LINES)
    for index in range(start - 1, limit):
        for char in lines[index]:
            if char == "{":
                depth += 1
                opened = True
            elif char == "}":
                depth -= 1
        if opened and depth <= 0:
            return index + 1
    return start


def _indent_end(lines: list[str], start: int) -> int:
    """缩进收尾：直到出现一个缩进不大于声明行的非空行为止。

    收尾那一行如果是块结束符（`end` / `}`），把它**算进去**：Ruby 的函数体
    少了 `end` 就不是合法代码，而 find_symbol 会把这段切给模型看。
    """
    base = len(lines[start - 1]) - len(lines[start - 1].lstrip())
    limit = min(len(lines), start - 1 + MAX_SYMBOL_LINES)
    last = start
    for index in range(start, limit):
        line = lines[index]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base:
            if line.strip().startswith(("end", "}")):
                last = index + 1
            break
        last = index + 1
    return last


# --- 语言表 --------------------------------------------------------------
#
# 这张表有两个用途，它们必须同源：**哪些后缀进索引**、以及**每个后缀用哪个
# 提取器**。分成两处写的话，迟早会出现「文件进了索引但没有提取器」——
# 那种状态下符号表是空的，而空符号表会被读成「这个文件里什么都没有」。

def _python() -> SymbolExtractor:
    return PythonAstExtractor()


def _brace() -> SymbolExtractor:
    return DeclarationExtractor(blocks=BRACE)


def _indent() -> SymbolExtractor:
    return DeclarationExtractor(blocks=INDENT)


def _none() -> SymbolExtractor:
    return DeclarationExtractor(blocks=NONE)


LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec("python", (".py", ".pyi"), _python),
    LanguageSpec(
        "javascript",
        (".js", ".mjs", ".cjs", ".jsx"),
        _brace,
    ),
    LanguageSpec("typescript", (".ts", ".tsx"), _brace),
    LanguageSpec("java", (".java",), _brace),
    LanguageSpec("kotlin", (".kt", ".kts"), _brace),
    LanguageSpec("csharp", (".cs",), _brace),
    LanguageSpec("go", (".go",), _brace),
    LanguageSpec("rust", (".rs",), _brace),
    LanguageSpec("c", (".c", ".h"), _brace),
    LanguageSpec("cpp", (".cc", ".cpp", ".cxx", ".hpp", ".hh"), _brace),
    LanguageSpec("swift", (".swift",), _brace),
    LanguageSpec("php", (".php",), _brace),
    LanguageSpec("scala", (".scala",), _brace),
    LanguageSpec("dart", (".dart",), _brace),
    LanguageSpec("ruby", (".rb",), _indent),
    LanguageSpec("lua", (".lua",), _indent),
    LanguageSpec("perl", (".pl", ".pm"), _indent),
    LanguageSpec("shell", (".sh", ".bash", ".zsh"), _none),
    LanguageSpec("powershell", (".ps1", ".psm1"), _none),
    LanguageSpec("batch", (".bat", ".cmd"), _none),
    # 结构化的配置与文档：没有"符号"，但仍然要进文件表——路径命中是
    # 排序的一个信号（"这个仓库里有没有 docs/ 之类的入口"）。
    LanguageSpec("markup", (".md", ".rst", ".txt"), _none, code=False),
    LanguageSpec(
        "data", (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"), _none, code=False
    ),
    LanguageSpec("web", (".html", ".htm", ".css", ".scss", ".vue", ".svelte"), _brace),
    LanguageSpec("sql", (".sql",), _none),
)

_BY_SUFFIX: dict[str, LanguageSpec] = {
    suffix: spec for spec in LANGUAGES for suffix in spec.suffixes
}


def indexed_suffixes() -> frozenset[str]:
    """进索引的文件后缀。索引与提取器同源，不许两处各写一份。"""
    return frozenset(_BY_SUFFIX)


def code_suffixes() -> frozenset[str]:
    """代码类后缀：索引时先给他们名额。"""
    return frozenset(
        suffix for spec in LANGUAGES if spec.code for suffix in spec.suffixes
    )


def language_of(path: str | Path) -> LanguageSpec | None:
    """按后缀判断语言。认不出来返回 None（不猜）。"""
    return _BY_SUFFIX.get(Path(str(path)).suffix.lower())


def extractor_for(path: str | Path) -> SymbolExtractor:
    """这个文件的提取器。认不出的后缀给通用扫描器，而不是空实现。"""
    spec = language_of(path)
    return spec.factory() if spec is not None else DeclarationExtractor()


def language_name(path: str | Path) -> str:
    spec = language_of(path)
    return spec.name if spec is not None else "unknown"
