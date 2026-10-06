"""写操作与 diff 预览。

写操作默认**不落盘**，只产生 diff。理由很直接：小模型出错率高，如果错误
直接污染真实代码，用户会丧失信任并最终弃用；而「先看 diff 再应用」几乎
零成本，既能在出错时一眼识破，又不打断思路。

所以全部写操作先进入 PendingChanges，由用户确认后才真正落盘。
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import time
from typing import Any, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from spoolkit import limits
from spoolkit import runlog
from spoolkit.errors import PathOutsideProjectError
from spoolkit.paths import resolve_readable, resolve_within
from spoolkit.tools.types import NEXT_STEP_MARKER, ToolResult, ToolSpec
from spoolkit.check.constraints import check_source, format_violations
from spoolkit.tools.view import WorkspaceView

# 项目里所有代码文件都不超过这个行数时，只提供整份重写。
#
# 实测把行号手术交给 7B 的代价：8 条失败里有 5 条死在 replace_lines 上——
# 它算错区间、拼错片段，留下重复行和悬空语句，而且看不出来自己错了。
# 同一批题里它的强项是「把整份文件交出来」：单发对照 25/25 用的就是那个形态。
# 选项少一个，它反而更容易做对。
WHOLE_FILE_LINE_LIMIT = 150

# ---- 大重写的声明式通道 ---------------------------------------------------
#
# dogfood 那次：snake.html 是两份文档首尾相接，正确产出是合并成单文档
# （303 行 → 约 150–300 行）。护栏把「合法合并」和「偷懒截断」当成同一件事
# 一律硬拦，模型被拒后原样重试，10 步预算耗尽、文件零改动。
#
# 所以给 write_file / replace_lines 开一条**显式声明**的路：模型必须说明
# 为什么大重写（rewrite_reason），并把工具算出来的「消失清单」逐项确认
# （acked_disappeared）。没声明的大重写照旧硬拦（护栏不松），声明了但
# 清单没确认完同样不放行。
REWRITE_REASON_PARAM = "rewrite_reason"
ACK_PARAM = "acked_disappeared"

# 按行替换的内容锚点（用户点名的第 5 条发现）。
#
# 名与形都刻意贴近「你看到的原文」：它只能从 read_file 的结果里抄。纯行号在
# 一次编辑之后就失效了，而失效时**不一定报错**——区间还落在文件里时会静默
# 改错一段，那就是「多出来的结束标签」的来源。锚点把这件事变成硬校验。
EXPECT_PARAM = "expect"

# ---- 确认清单拆成独立小回合：正文只发一次、确认只发项 id --------------------
#
# dogfood 复跑（r4：11 步 / 15 次调用 / 315.9s / fail / 文件零改动）暴露的真实形状：
# 声明式通道要求「第一次调用就把清单凑齐」，而清单是**工具算出来的**——模型在
# 发出第一次调用之前看不到它。于是那次调用必然被拒，模型只好把 120 行正文再发
# 一遍（这回带上了确认项）。输出预算 2457 token 下，一份 300 行正文都装不下
# （r1–r3 死在输出截断，`write_file` 连 `path` 都没发出来），两份就是稳死。
#
# 所以把「正文」与「举证」拆成两个回合：
#   ① 第一次带 rewrite_reason 的写调用 → 正文**暂存**，返回消失清单（每项一个
#      稳定项 id）+ 一个短令牌；文件尚未落盘。
#   ② confirm_rewrite(token, acked=[id...], dropped=["id：理由"...]) → **只发项 id**，
#      绝不重发正文。清单里每一项二选一：确认保留（acked）或声明有意丢弃
#      （dropped + 理由）。两者都没给的项仍在 → 这次重写不落盘（硬要求不松）。
CONFIRM_TOOL = "confirm_rewrite"
TOKEN_PARAM = "token"
ACKED_PARAM = "acked"
DROPPED_PARAM = "dropped"

# 暂存结果的内容前缀。循环靠它认两件事：这次调用没有落盘（别当产出），以及
# 「同一份正文再发一遍」是纯浪费（进失败表，原样重提会被硬拦）。
STAGE_MARKER = "⏸ 已暂存待确认的重写"

# 丢弃理由的分隔符。`dropped` 每一项写成「项 id：理由」——理由不许省，
# 省了就不算「有意丢弃」，只是「没交代」。
DROP_REASON_SEPARATOR = "："

# 拒绝整文件重写的判据（两条硬判据原来的数值没变，只是给了名字）。
REWRITE_MIN_OLD_LINES = 30
REWRITE_SHRINK_RATIO = 0.5

# 拒绝话术的开头。循环靠它认出「这次调用是被护栏拦下的，不是执行后失败」，
# 于是这一步不占步数预算（见 agent/loop.py 的 BLOCKED 判定）。
REFUSAL_MARKER = "⚠ 拒绝整文件重写"

# ---- 删除文件的声明式通道 -------------------------------------------------
#
# 为什么要有它：merge-html 那条探针里，模型把内容合并好了，用户那句「以后我
# 就看一个文件」却落不了地——工具集里**没有删除文件的工具**，它只能让用户
# 自己去 `rm pricing.html`（两次输出一字不差），而判据要求 pricing.html 要么
# 不存在、要么不再是那份带完整导航的重复页面。缺的不是能力，是一只手。
DELETE_TOOL = "delete_file"
DELETE_REASON_PARAM = "reason"

# 删除也走「拒绝」那条前缀：**没有执行**的动作不该白吃一步预算，而且
# 模型需要从话术里认出「这是一次被拦下的调用，不是执行失败」。
DELETED_NOT_FOUND = "文件不存在"

# 出路话术的标记行。**从 `tools/types.py` 取**：写下这一行的有两个地方——编辑
# 工具的拒绝话术，和参数校验的拒绝话术（`tools/registry.py`）；字面量只留一份。
# 这里是转出，老调用方 `from spoolkit.tools.edit import NEXT_STEP_MARKER`
# 一个字都不用改。

_SKIP_DIRS = frozenset({".agent", ".git", ".venv", "__pycache__", "node_modules"})

_TEST_DIRS = frozenset({"test", "tests"})


@dataclass(frozen=True)
class Disappeared:
    """旧文本里有、新文本里没有的一样东西。"""

    kind: str
    # 清单里显示的条目。逐字照抄它，或只写 key，都算确认。
    token: str
    # 标识符本身。按它判定「还在不在」：`const canvas` 改写成 `let canvas`
    # 时名字仍在，不该报成消失。
    key: str


# 函数 / 类 / 关键字声明。Python 与 JS 的口径放在一起：这个工具面对的是
# 「一个文件」，不问它是哪种语言。
_DECL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("函数", re.compile(r"(?m)^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)")),
    ("函数", re.compile(r"\bfunction[ \t]+([A-Za-z_$][\w$]*)")),
    ("类", re.compile(r"\bclass[ \t]+([A-Za-z_$][\w$]*)")),
    ("声明", re.compile(r"\b(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)")),
)

# id 与事件处理器。`on*= ` 抓属性式绑定，addEventListener 抓脚本式绑定——
# 实测 dogfood 丢的正是这两类（标题、overlay、重启按钮的绑定）。
_ATTR_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("id", re.compile(r"""\bid[ \t]*=[ \t]*["']([^"'\s]+)["']"""), "id={0}"),
    ("事件", re.compile(r"(?i)\bon([a-z]+)[ \t]*="), "on{0}="),
    (
        "事件",
        re.compile(r"""addEventListener\([ \t]*["']([^"']+)["']"""),
        "addEventListener:{0}",
    ),
)

# CSS 选择器。只认「行首（或逗号后）+ 紧跟 `,`/`{`」的写法，这样 JS 里
# 缩进的 `.then(` 不会被误当成选择器；误报会让模型被迫确认不存在的消失项。
_CSS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?m)^[ \t]*([.#][A-Za-z_-][\w-]*)[ \t]*[,{]"),
    re.compile(r",[ \t]*([.#][A-Za-z_-][\w-]*)[ \t]*[,{]"),
)



def is_test_path(path: str) -> bool:
    """这个路径看起来是不是测试文件。

    放在这里而不是验证模块：写工具和验证器都要用它，而反过来引会形成
    循环导入。
    """
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return False
    if pure.name.startswith("test_") or pure.name.endswith("_test.py"):
        return True
    return any(part.lower() in _TEST_DIRS for part in pure.parts[:-1])


def looks_like_path(text: str) -> bool:
    """这个字符串看起来是不是路径，而不是符号名。"""
    return "/" in text or "\\" in text or text.endswith(
        (".py", ".md", ".json", ".toml", ".txt")
    )


# ---- 跨文件改名的残留检测 --------------------------------------------------
#
# 实测（rename-cross-file 探针首轮）：模型把 `get_rows` 改成了 `load_rows`，
# 四个 .py 文件全改对了、测试全过，却**宣称「整个项目都统一了」**——
# 而 README.md 与 docs/design.md 里那两处旧名一个字没动。
#
# 「说没统一」这件事是可机械判定的：改过的那份正文里**消失的顶层定义名**，
# 如果在别处还在，那就是残留。检测放在工具层（改完立刻算），报告跟着工具
# 输出回到模型面前——它在写「已统一」那句话之前看得到反例。
#
# ---- 这次补的三个盲区（P0，全部只报不阻断）----------------------------------
#
# 1. **被改的文件自己**：旧口径要求名字在新正文里「以标识符形态彻底消失」，
#    于是同一文件里留下的旧名调用（定义改了、调用点没改）永远进不了候选。
#    现在定义一消失就进候选，是不是真的悬空由 `ast` 判（见 `_module_index`）。
# 2. **删掉的文件**：删除走的是同一份台账，但旧代码一进门就
#    `if ... or change.deleted: return []`——「删了这个文件，别人还在用它」。
# 3. **引用闭合**：改过的 `.py` 里导入一个**本仓并不存在**的模块，
#    以及删掉的文件仍在被 `import` / 属性访问引用（`ast` 判，不看字符串）。
_TOP_DEF_PATTERN = re.compile(
    r"(?m)^(?:async[ \t]+)?(?:def|class)[ \t]+([A-Za-z_]\w*)"
)
LINGERING_MARKER = "旧名仍有残留"

#: 事件流里「残留引用」这一类 note 的 kind。探针按它统计 `stale_references`。
STALE_REFERENCES_KIND = "stale_references"

#: 机械发现的判据名。写在常量里是为了事件流、探针与用例都按它取，
#: 不靠散文措辞去认（设计稿 §4：判据必须机械）。
STALE_REFERENCE_KIND = "stale_reference"
DELETED_REFERENCE_KIND = "deleted_reference"
BROKEN_IMPORT_KIND = "broken_import"
# 扫描是有界的：单文件字符数上限、整次扫描字符总量上限、报告文件条数上限。
# 没有上限的话，一次改名会在一个大仓库上把每一步都拖慢，而收尾报告的读者是人。
LINGER_SCAN_MAX_FILE_CHARS = 200_000
LINGER_SCAN_BUDGET = 400_000
LINGER_MAX_FILES = 8
LINGER_MAX_LINES_PER_FILE = 4
LINGER_SKIP_DIRS = frozenset(
    {"__pycache__", ".git", ".venv", "venv", ".agent", "node_modules", ".mypy_cache"}
)
# 只扫这些后缀：残留报告是给人看的，扫二进制只会白花时间。
LINGER_TEXT_SUFFIXES = frozenset(
    {
        ".py", ".md", ".txt", ".rst", ".html", ".htm", ".js", ".ts", ".tsx",
        ".jsx", ".json", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".css",
        ".sh", ".bat", ".ps1", ".sql", ".java", ".go", ".rs", ".c", ".h",
        ".cpp", ".rb", ".php",
    }
)


@dataclass(frozen=True)
class MechanicalHit:
    """一条机械发现：判据 + `文件:行` + 事实。**没有 file:line 不上报**（§4）。

    `line` 是第一条命中的行，`lines` 是全部命中的行。分开是因为用途不同：
    事件流与探针要一个可回指的坐标，报告要「还有几处」。
    """

    kind: str
    path: str
    line: int
    name: str
    detail: str = ""
    lines: tuple[int, ...] = ()

    def where(self) -> str:
        return f"{self.path}:{self.line}"

    def render(self) -> str:
        more = f" 等 {len(self.lines)} 处" if len(self.lines) > 1 else ""
        tail = f"——{self.detail}" if self.detail else ""
        return f"{self.where()}（{self.name}{more}）{tail}"


def _name_lines(text: str, name: str) -> list[int]:
    """`name` 在这个文件里出现的行号（按标识符边界，1 起）。

    边界必须判：`load_rows` 里含 `rows`、`get_rows_v2` 里含 `get_rows`，
    不做边界检查就会把「已经改好了」的文件报成残留。
    """
    found: list[int] = []
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])"
    )
    for index, line in enumerate(text.splitlines(), start=1):
        if pattern.search(line):
            found.append(index)
    return found


def _literal_lines(text: str, needle: str) -> list[int]:
    """`needle` 作为**字面量**出现的行号。路径与点号模块名走这一条。"""
    found: list[int] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if needle and needle in line:
            found.append(index)
    return found


def _module_index(text: str) -> tuple[set[str], dict[str, list[int]]] | None:
    """这份 Python 正文里「被绑定过的名字」与「每个名字的读用行号」。

    两者的差别只能靠语法树看出来，这是本轮补盲的核心判据：

    - `get_rows = load_rows` 是一次**绑定**（兼容别名）——名字还接着，
      引用不会坏，**不是**残留；
    - `return get_rows(...)` 是一次**读用**——定义已经没了，它就是悬空引用。

    纯文本比对做不到这个区分：旧口径为了躲开别名误报（实测那次误报把模型
    引去删自己刚留的别名，连着 4 步打转），把「名字还在新正文里」整个划掉，
    于是同一文件里的悬空引用也一起漏掉了。

    解析不了（语法错、不是 Python）返回 `None`——**判不了就不报**，
    乱报的代价比漏报大（§4）。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    bound: set[str] = set()
    loaded: dict[str, list[int]] = {}

    def bind(name: str) -> None:
        if name:
            bound.add(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                bind(node.id)
            else:
                loaded.setdefault(node.id, []).append(node.lineno)
        elif isinstance(node, ast.arg):
            bind(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name)
        elif isinstance(node, ast.alias):
            # `import a.b` 绑的是 `a`；`from x import y as z` 绑的是 `z`。
            bind((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler):
            bind(node.name or "")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for one in node.names:
                bind(one)
        elif isinstance(node, ast.MatchAs):  # pragma: no cover - 3.10+ 才有
            bind(node.name or "")
        elif isinstance(node, ast.MatchStar):  # pragma: no cover - 3.10+ 才有
            bind(node.name or "")
        elif isinstance(node, ast.MatchMapping):  # pragma: no cover - 3.10+ 才有
            bind(node.rest or "")
    return bound, loaded


def vanished_definitions(old_text: str, new_text: str) -> list[str]:
    """改过的那份正文里**顶层定义消失**的名字（不管它还在不在正文里出现）。

    与旧口径 `vanished_def_names` 的差别就是这次的补盲：旧口径额外要求
    名字在新正文里「以标识符形态彻底消失」，于是**被改的文件自己**那一处
    永远进不了候选——定义改名了、同一文件里还留着一处旧名调用时，
    名字当然还出现在新正文里。现在定义消失就进候选，它到底是不是真的
    悬空（还是留了兼容别名）交给 `_module_index` 判。
    """
    before = {name for name in _TOP_DEF_PATTERN.findall(old_text)}
    if not before:
        return []
    after = {name for name in _TOP_DEF_PATTERN.findall(new_text)}
    return sorted(before - after)


def vanished_def_names(old_text: str, new_text: str) -> list[str]:
    """（旧口径）名字在新正文里**彻底不见了**的那几个定义名。

    保留它是因为 `pending_renames` 那条判据（「改名只落了一半」）就是按
    「旧名彻底消失 + 新名多出一个」的形状算的。残留检测不再用它——
    见 `vanished_definitions`。
    """
    return [
        name
        for name in vanished_definitions(old_text, new_text)
        if not _name_lines(new_text, name)
    ]


def _scan_texts(root: Path, pending, skip: set[str]) -> list[tuple[str, str]]:
    """可以拿来扫的正文：待确认改动的新正文 + 磁盘上的文本文件（有界）。

    「先看待确认改动」这一条不能省：改名常常是一轮里连着改好几个文件，
    磁盘上还是旧内容；只看磁盘，同一轮里刚写下的
    `from pipeline.fetch import get_rows` 就会从清单上漏掉。

    上界沿用 `LINGER_SCAN_*`（单文件 200k / 合计 400k），一次改名不会把
    每一步都拖慢。
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set(skip)
    for item in pending.items() if pending is not None else ():
        if item.path in skip or item.deleted:
            continue
        try:
            text = WorkspaceView(root, pending).read_text(pending.disk_path(item.path))
        except (OSError, UnicodeDecodeError, FileNotFoundError):
            continue
        seen.add(item.path)
        candidates.append((item.path, text))

    budget = LINGER_SCAN_BUDGET
    try:
        for candidate in sorted(root.rglob("*")):
            if budget <= 0:
                break
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root).as_posix()
            if relative in seen or LINGER_SKIP_DIRS & set(candidate.parts):
                continue
            if candidate.suffix.lower() not in LINGER_TEXT_SUFFIXES:
                continue
            try:
                if candidate.stat().st_size > LINGER_SCAN_MAX_FILE_CHARS:
                    continue
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            budget -= len(text)
            candidates.append((relative, text))
    except OSError:  # pragma: no cover - 枚举失败不该毁掉一次成功的写入
        pass
    return candidates


def _module_path_of(relative: str) -> str:
    """`pipeline/fetch.py` → `pipeline.fetch`；`pipeline/__init__.py` → `pipeline`。"""
    pure = PurePosixPath(relative)
    parts = list(pure.parts)
    if pure.name == "__init__.py":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1] + [pure.stem])


def _absolute_module(module: str | None, level: int, relative: str) -> str:
    """把 `from .fetch import x` 这类相对导入解析成点号绝对名。

    `level` 是点号个数（`ast.ImportFrom.level`）：1 = 当前包（文件所在目录），
    2 = 上一层。解析不出来（点号越过了项目根）返回空串——那种导入本来
    就是错的，但判不出指向谁，宁可不报。
    """
    if level <= 0:
        return module or ""
    base = list(PurePosixPath(relative).parent.parts)
    drop = level - 1
    if drop > len(base):
        return ""
    keep = base[: len(base) - drop] if drop else base
    return ".".join([*keep, *([module] if module else [])])


def _import_statements(text: str, relative: str) -> list[tuple[int, str, tuple[str, ...]]]:
    """这份 `.py` 里所有导入：`(行号, 绝对模块名, 导入的名字)`。

    **用 `ast` 而不是文本匹配**：`.py` 里出现 `pipeline/fetch.py` 这种字符串
    常常正是「断言它不存在」的测试，拿字符串判会把测试报成残留。
    解析不了就返回空表（判不了不报）。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    found: list[tuple[int, str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name, ()))
        elif isinstance(node, ast.ImportFrom):
            absolute = _absolute_module(node.module, int(node.level or 0), relative)
            if not absolute:
                continue
            found.append(
                (node.lineno, absolute, tuple(one.name for one in node.names))
            )
    return found


def _dotted_attributes(text: str) -> list[tuple[int, str]]:
    """`pipeline.fetch.rows` 这类属性访问的点号全名与行号。

    删掉 `pipeline/fetch.py` 之后，`import pipeline` + `pipeline.fetch.rows()`
    不会出现在任何 ImportFrom 里——只有把点号链拼起来才看得见。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    found: list[tuple[int, str]] = []

    def dotted(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            head = dotted(node.value)
            return f"{head}.{node.attr}" if head else ""
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = dotted(node)
            if name:
                found.append((node.lineno, name))
    return found


def _import_targets(module: str, imported: tuple[str, ...], target: str) -> bool:
    """这条导入是不是指向被删掉的那个模块 `target`。

    三种形状都算，其余不算：
    - `import a.b` / `from a.b import x` 里 a.b 就是 target（或它的子模块）；
    - `from a import b` 里 a 是 target 的**父包**、b 是 target 的末段
      （删掉 `pipeline/fetch.py` 之后 `from pipeline import fetch` 就是这种）；
    - `import a.b as c` 同上。
    """
    if not module or not target:
        return False
    if module == target or module.startswith(target + "."):
        return True
    parent, _, last = target.rpartition(".")
    if parent and module == parent and last in imported:
        return True
    return False


def _dedupe_hits(hits: Sequence[MechanicalHit]) -> list[MechanicalHit]:
    """同一 (判据, 文件, 第一行, 名字) 只留一条，顺序即发现顺序。"""
    out: list[MechanicalHit] = []
    seen: set[tuple[str, str, int, str]] = set()
    for hit in hits:
        mark = (hit.kind, hit.path, hit.line, hit.name)
        if mark in seen:
            continue
        seen.add(mark)
        out.append(hit)
    return out


def deleted_reference_hits(root: Path, pending, change: PendingChange) -> list[MechanicalHit]:
    """**删掉的那个文件**还有谁在引用它（本轮补的第二个盲区）。

    两条机械判据：

    1. **按导入/属性引用**（`.py` 走 `ast`）：`import a.b`、`from a.b import x`、
       `from a import b`、`a.b.rows()`——真的指向这个被删的模块才算。
    2. **按名字/路径点名**（非 `.py` 文本）：README / docs / 配置里还写着它的
       顶层符号名、相对路径或点号模块名。

    `.py` 里的纯文本出现**不算**：`assert not Path("pipeline/fetch.py").exists()`
    正是「确认它删掉了」的写法，报它就是把对的判成错的（§4 零误报）。
    """
    names = sorted(set(_TOP_DEF_PATTERN.findall(change.old_text)))
    module = _module_path_of(change.path)
    slash = change.path.replace("/", "\\")
    hits: list[MechanicalHit] = []
    for relative, text in _scan_texts(root, pending, {change.path}):
        if relative.endswith(".py"):
            for line, module_text, imported in _import_statements(text, relative):
                if _import_targets(module_text, imported, module):
                    hits.append(
                        MechanicalHit(
                            kind=DELETED_REFERENCE_KIND,
                            path=relative,
                            line=line,
                            name=module_text,
                            detail=f"这个 import 还指向已删除的 {change.path}",
                        )
                    )
            for line, dotted in _dotted_attributes(text):
                if dotted == module or dotted.startswith(module + "."):
                    hits.append(
                        MechanicalHit(
                            kind=DELETED_REFERENCE_KIND,
                            path=relative,
                            line=line,
                            name=dotted,
                            detail=f"这个属性访问还指向已删除的 {change.path}",
                        )
                    )
            continue
        for name in names:
            lines = _name_lines(text, name)
            if lines:
                hits.append(
                    MechanicalHit(
                        kind=DELETED_REFERENCE_KIND,
                        path=relative,
                        line=lines[0],
                        name=name,
                        detail=f"{change.path} 已被删除，这里还在按名字提到它",
                        lines=tuple(lines),
                    )
                )
        for needle in (change.path, slash, module):
            lines = _literal_lines(text, needle)
            if lines:
                hits.append(
                    MechanicalHit(
                        kind=DELETED_REFERENCE_KIND,
                        path=relative,
                        line=lines[0],
                        name=needle,
                        detail=f"这里点名了已删除的 {change.path}",
                        lines=tuple(lines),
                    )
                )
    return _dedupe_hits(hits)[:LINGER_MAX_FILES]


def lingering_hits(root: Path, pending, change: PendingChange) -> list[MechanicalHit]:
    """这次改动（改名 / 删除）留下的残留引用，逐条带 `文件:行`。

    三个来源都是机械判据（见本节头部那三个盲区）：

    - **本文件内**：定义没了，而同一份新正文里还有它的**读用处**（不是绑定）
      ——悬空引用；
    - **别处**：在新正文里连绑定都没有的名字，在别的文件里还出现
      （README / docs / 配置 / 字符串也算）；
    - **删掉的文件**：见 `deleted_reference_hits`。
    """
    if change.deleted:
        return deleted_reference_hits(root, pending, change)

    names = vanished_definitions(change.old_text, change.new_text)
    if not names:
        return []

    index = _module_index(change.new_text)
    bound = index[0] if index is not None else None
    hits: list[MechanicalHit] = []

    if bound is not None:
        for name in names:
            if name in bound:
                # 新正文里还有它的绑定（兼容别名 / 重新导入）——引用不会坏。
                continue
            lines = sorted(set(index[1].get(name) or []))
            if lines:
                hits.append(
                    MechanicalHit(
                        kind=STALE_REFERENCE_KIND,
                        path=change.path,
                        line=lines[0],
                        name=name,
                        detail="本文件内仍在引用它，而它的定义已经没有了",
                        lines=tuple(lines),
                    )
                )
        elsewhere = [name for name in names if name not in bound]
    else:  # pragma: no cover - 只在新正文语法坏掉时走到
        # 判不了绑定关系时退回旧口径：正文里彻底没有它的名字才当残留候选。
        elsewhere = [name for name in names if not _name_lines(change.new_text, name)]

    if elsewhere:
        for relative, text in _scan_texts(root, pending, {change.path}):
            for name in elsewhere:
                lines = _name_lines(text, name)
                if not lines:
                    continue
                hits.append(
                    MechanicalHit(
                        kind=STALE_REFERENCE_KIND,
                        path=relative,
                        line=lines[0],
                        name=name,
                        detail="这里仍在引用旧名",
                        lines=tuple(lines),
                    )
                )
    return _dedupe_hits(hits)[:LINGER_MAX_FILES]


def lingering_references(
    root: Path, pending, change: PendingChange
) -> list[str]:
    """残留清单（人读的一行一条）。`lingering_hits` 的文本形态。"""
    hits = lingering_hits(root, pending, change)
    out: list[str] = []
    for hit in hits:
        where = "、".join(
            str(item) for item in hit.lines[:LINGER_MAX_LINES_PER_FILE]
        )
        more = (
            f" 等 {len(hit.lines)} 处"
            if len(hit.lines) > LINGER_MAX_LINES_PER_FILE
            else ""
        )
        tail = f"，{hit.detail}" if hit.detail else ""
        out.append(f"{hit.path} 第 {where} 行{more}（{hit.name}{tail}）")
    return out


def _local_top_segments(root: Path, view: WorkspaceView) -> set[str]:
    """工作区（含 `src/`）里**第一方**的顶层包/模块名。

    导入闭合只判这些：第三方与标准库装在哪儿不归工作区管，拿工作区去找
    只会制造误报（§7「不给没有机械判据的东西硬造判据」）。
    """
    found: set[str] = set()
    for base in (root, root / "src"):
        if not base.is_dir():
            continue
        try:
            for item in base.iterdir():
                if item.is_dir() and not (LINGER_SKIP_DIRS & set(item.parts)):
                    found.add(item.name)
                elif item.suffix == ".py" and item.stem != "__init__":
                    found.add(item.stem)
        except OSError:  # pragma: no cover - 枚举失败不算发现
            continue
    return found


def broken_local_imports(
    root: Path, pending, changed: Sequence[str] | None = None
) -> list[MechanicalHit]:
    """改动过的 `.py` 里，**指向本仓却不存在的模块**的 import（引用闭合）。

    只判「第一方 + 模块文件不存在」这一种形状：判据是「这个点号名在工作区里
    没有对应的 `.py` / 包」，机械且可证伪。名字级的闭合（`from a import b`
    里 b 有没有）不在这里判——那种形状由残留检测那条覆盖（被删掉的顶层定义
    名还会在别的文件里以文本形态出现）。

    `changed` 给了就只判这几个路径；不给就判全部待确认改动。
    """
    view = WorkspaceView(root, pending)
    items = pending.items() if pending is not None else []
    wanted = set(changed) if changed else None
    local = _local_top_segments(root, view)
    hits: list[MechanicalHit] = []
    for change in items:
        if change.deleted or Path(change.path).is_absolute():
            continue
        if wanted is not None and change.path not in wanted:
            continue
        if not change.path.endswith(".py"):
            continue
        for line, module_text, _imported in _import_statements(
            change.new_text, change.path
        ):
            if not module_text:
                continue
            head = module_text.split(".")[0]
            if head not in local:
                continue
            if _module_exists(root, view, module_text):
                continue
            hits.append(
                MechanicalHit(
                    kind=BROKEN_IMPORT_KIND,
                    path=change.path,
                    line=line,
                    name=module_text,
                    detail=f"导入的本地模块 {module_text} 在工作区里不存在",
                )
            )
    return _dedupe_hits(hits)[:LINGER_MAX_FILES]


def _module_exists(root: Path, view: WorkspaceView, module_text: str) -> bool:
    """点号模块名在工作区里有没有对应的文件/包（叠加待确认改动之后）。"""
    parts = module_text.split(".")
    for base in (root, root / "src"):
        for suffix in (parts[:-1] + [parts[-1] + ".py"], [*parts, "__init__.py"]):
            target = base.joinpath(*suffix)
            if view.exists(target):
                return True
        # PEP 420 命名空间包：没有 `__init__.py` 的目录也是合法的包。
        if view.is_dir(base.joinpath(*parts)):
            return True
    return False


def lingering_note(root: Path, pending, change: PendingChange) -> str:
    """残留报告（没有残留就返回空串）。

    话术里**必须点名那句不许说的话**：模型不是没看到旧名，它是在结论里
    写下了「整个项目里都统一了」。一句「旧名仍有残留」它可能读成背景信息，
    所以这里直接把「不许宣称已统一」写成要求，并给出下一步。

    **末尾那句反向条款是这段文字的组成部分，不许删**（设计稿 §2.5 的
    「硬规矩（给未来）」+ §8 D12）：这段会进模型可见的上下文，只留
    「去把引用改对」这半段，它会把「清残留」读成「禁止删除 / 禁止精简」，
    于是不敢删死代码、不敢合并重复实现。
    """
    hits = lingering_references(root, pending, change)
    if not hits:
        return ""
    return (
        f"⚠ {LINGERING_MARKER}：这次改名里有名字在本文件里已经没有了，"
        "但下面这些地方还在引用它们——旧名与新名混用的项目是坏的"
        "（导入或调用会直接失败）：\n"
        + "".join(f"  - {item}\n" for item in hits)
        + "这些非代码文本（README / docs）与其它文件都要一起改。"
        f"在它们改完之前，**不要说「整个项目里都统一了」**——"
        "那句话现在与事实不符。改完再给结论，或者如实说明还有哪几处没改。\n"
        "（这一段要的是把引用改对：删除死代码与冗余分支、合并重复实现、"
        "精简文件、删除已被替代的文件，都是允许的正常编辑。）\n"
    )


def is_small_project(root: Path, limit: int = WHOLE_FILE_LINE_LIMIT) -> bool:
    """项目里是否都是小文件。

    只要有一个文件超过阈值，就说明这个项目会需要行号级编辑，
    此时两把工具都要给。
    """
    for path in root.rglob("*.py"):
        if _SKIP_DIRS & set(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.count("\n") + 1 > limit:
            return False
    return True


def _count_lines(text: str) -> int:
    """行数口径与护栏原来的算法一致（不以换行结尾时最后一行也算一行）。"""
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _symbols(text: str) -> list[Disappeared]:
    """旧/新文本里出现的函数、类名、关键字声明、id、事件处理器与 CSS 选择器。"""
    found: list[Disappeared] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, token: str, key: str) -> None:
        token = " ".join(token.split())
        if not token or not key:
            return
        mark = (kind, key)
        if mark in seen:
            return
        seen.add(mark)
        found.append(Disappeared(kind=kind, token=token, key=key))

    for kind, pattern in _DECL_PATTERNS:
        for match in pattern.finditer(text):
            add(kind, match.group(0), match.group(1))
    for kind, pattern, template in _ATTR_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1).lower() if kind == "事件" else match.group(1)
            add(kind, template.format(value), value)
    for pattern in _CSS_PATTERNS:
        for match in pattern.finditer(text):
            add("选择器", match.group(1), match.group(1))
    return found


def disappeared_items(old_text: str, new_text: str) -> list[Disappeared]:
    """算「消失清单」：旧文本里有、新文本里已经没有的那几样东西。

    判定按**标识符**而不是整条目字符串：`const canvas` 被改写成 `let canvas`
    时名字还在，报成消失只会逼模型确认一件没发生的事。
    """
    surviving = {item.key for item in _symbols(new_text)}
    return [item for item in _symbols(old_text) if item.key not in surviving]


def duplicate_span(
    old_text: str, min_lines: int = 6
) -> tuple[int, int, int, int] | None:
    """找出一段重复出现的行区间：返回 (a, b, c, d)，1 起、闭区间。

    合并/搬运类任务的特征就是「同一段内容在文件里出现了两次」。只给一句
    「用 replace_lines」等于没给出路——必须给出能照抄的行号区间。
    """
    lines = old_text.splitlines()
    index: dict[str, list[int]] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if len(stripped) < 3:
            continue
        index.setdefault(stripped, []).append(i)
    best: tuple[int, int, int] | None = None
    for positions in index.values():
        # 太常见的行（`}`、`</div>`）到处都是，连不成「一段重复内容」。
        if len(positions) > 40:
            continue
        for first in range(len(positions)):
            for second in range(first + 1, len(positions)):
                i, j = positions[first], positions[second]
                run = 0
                while (
                    j + run < len(lines)
                    and i + run < j
                    and lines[i + run] == lines[j + run]
                ):
                    run += 1
                if run >= min_lines and (best is None or run > best[0]):
                    best = (run, i, j)
    if best is None:
        return None
    run, i, j = best
    return (i + 1, i + run, j + 1, j + run)


def item_id(index: int) -> str:
    """消失清单里第 index 项的**稳定项 id**（0 起）。

    为什么要有它：确认回合只能发项 id，否则模型就得把「function draw」这类
    条目原文再抄一遍——那就又回到了「重发正文」的老路。id 只由清单内的位置
    决定，同一次暂存里永远指向同一项。
    """
    return f"d{index + 1}"


def _item_aliases(items: Sequence[Disappeared], index: int) -> set[str]:
    """一项可以被认出来的全部写法：项 id、序号、条目原文、标识符。

    宽容是有意的：这些写法**都指向同一项**，认错的可能性为零；而少认一种，
    模型就要白花一轮。它省下的每一轮都是输出预算。
    """
    item = items[index]
    return {
        item_id(index).casefold(),
        str(index + 1),
        " ".join(item.token.split()).casefold(),
        item.key.casefold(),
    }


def _match_items(items: Sequence[Disappeared], raw: Any) -> set[int]:
    """把模型给的项清单（acked / dropped 的左边）换算成清单下标。"""
    if not isinstance(raw, (list, tuple)):
        return set()
    matched: set[int] = set()
    for entry in raw:
        text = " ".join(str(entry).split()).casefold()
        if not text:
            continue
        for index in range(len(items)):
            if text in _item_aliases(items, index):
                matched.add(index)
    return matched


def _render_items(items: Sequence[Disappeared]) -> str:
    """消失清单的渲染。每项都带**稳定项 id**——确认回合只发 id，不发正文。"""
    return "、".join(f"{item_id(i)} {item.kind} {item.token}" for i, item in enumerate(items))


def _ids_of(items: Sequence[Disappeared], subset: Sequence[Disappeared]) -> list[str]:
    """`subset` 里各项在**完整清单**里的项 id（i 是完整清单里的位置）。"""
    found: list[str] = []
    for item in subset:
        try:
            found.append(item_id(list(items).index(item)))
        except ValueError:  # pragma: no cover - subset 必然来自 items
            continue
    return found


def _declaration(args: dict) -> tuple[str, list[str]]:
    """取出模型显式给出的重写声明：(原因, 已确认的消失清单条目)。"""
    reason = args.get(REWRITE_REASON_PARAM)
    reason = reason.strip() if isinstance(reason, str) else ""
    raw = args.get(ACK_PARAM)
    acked = [str(item) for item in raw] if isinstance(raw, list) else []
    return reason, acked


def _declaration_note(
    reason: str,
    acked: Sequence[Disappeared],
    shrink: bool,
    dropped: Sequence[tuple[Disappeared, str]] = (),
) -> str:
    """声明式重写留在待确认清单上的那一行痕迹。

    「确认保留」与「有意丢弃」都要留痕，而且是**分开写**的：报告里得看得出
    哪一项是被有意丢掉的、理由是什么。只写「已确认 N 项」会把两种选择混成
    一件事——而它们对读报告的人意义完全不同。
    """
    if not reason:
        return ""
    parts = [f"声明重写——{reason}"]
    if shrink:
        parts.append(f"行数减少超过一半（{REWRITE_SHRINK_RATIO:.0%} 线），走声明通道")
    if acked:
        parts.append(
            f"消失清单逐项交代：确认保留 {len(acked)} 项："
            + "、".join(item.token for item in acked)
        )
    if dropped:
        parts.append(
            f"消失清单里**有意丢弃** {len(dropped)} 项（逐项写明理由）："
            + "；".join(f"{item.token} ← {why}" for item, why in dropped)
        )
    if not acked and not dropped:
        parts.append("消失清单为空")
    return "；".join(parts)


def _unaccounted(
    items: Sequence[Disappeared],
    acked: Sequence[str],
    dropped: Sequence[str],
) -> list[Disappeared]:
    """清单里既没确认保留、也没声明有意丢弃的项。

    这就是那条**不许松**的硬要求：两者都没给的项只要还在，这次重写就不落盘。
    """
    covered = _match_items(items, acked) | _match_items(items, dropped)
    return [item for index, item in enumerate(items) if index not in covered]


def _split_dropped(raw: Any) -> tuple[list[tuple[str, str]], str]:
    """把 `dropped` 拆成 [(项, 理由)]；形状/理由不合法时返回人话错误。

    两种写法都收：`"d3：这条重复脚本整段删掉"`（提示词里给的形状）与
    `{"id": "d3", "reason": "..."}`。理由缺失一律拒——没有理由的「丢弃」
    与「漏了没交代」是同一件事，放行等于把硬要求掏空。
    """
    if raw in (None, ""):
        return [], ""
    if not isinstance(raw, (list, tuple)):
        return [], (
            f"{DROPPED_PARAM} 必须是数组，每项写成「项id{DROP_REASON_SEPARATOR}为什么可以丢」，"
            '例如 ["d3：这段重复脚本整段删掉"]'
        )
    pairs: list[tuple[str, str]] = []
    for entry in raw:
        if isinstance(entry, dict):
            target = str(entry.get("id") or entry.get("item") or "").strip()
            why = str(entry.get("reason") or entry.get("why") or "").strip()
        else:
            text = str(entry).strip()
            cut = -1
            for mark in (DROP_REASON_SEPARATOR, ":"):
                found = text.find(mark)
                if found >= 0 and (cut < 0 or found < cut):
                    cut = found
            if cut < 0:
                target, why = text, ""
            else:
                target, why = text[:cut].strip(), text[cut + 1 :].strip()
        if not target:
            return [], f"{DROPPED_PARAM} 里有一项没写项 id，例如 d2"
        if not why:
            return [], (
                f"{DROPPED_PARAM} 里的 {target} 没写理由。"
                "「有意丢弃」必须逐项说明为什么可以丢——"
                f'写法是 ["{target}{DROP_REASON_SEPARATOR}为什么这一项可以不要"]；'
                "没有理由就按「还没交代」处理，这次重写不落盘"
            )
        pairs.append((target, why))
    return pairs, ""


def _unknown_entries(items: Sequence[Disappeared], raw: Any) -> list[str]:
    """`raw` 里认不出对应清单项的写法（模型抄错 id 时要说出来）。

    不说出来的后果很具体：模型以为自己交代了 d9，于是对「还有一项没交代」
    百思不得其解，接着重发同一份清单。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    return [
        str(entry)
        for entry in raw
        if str(entry).strip() and not _match_items(items, [entry])
    ]


def _dropped_targets(
    items: Sequence[Disappeared], pairs: Sequence[tuple[str, str]]
) -> tuple[list[tuple[Disappeared, str]], list[str]]:
    """把 [(项写法, 理由)] 换算成 ([(清单项, 理由)], 认不出的写法)。

    认不出的项**不影响判定**：它没有让任何一项被交代掉，所以那些项仍然
    unaccounted、仍然拦着落盘（fail-safe）。但要把它们说出来——「你写的 d9
    清单里没有」比「还有一项没交代」更容易照做。
    """
    matched: list[tuple[Disappeared, str]] = []
    unknown: list[str] = []
    for target, why in pairs:
        found = _match_items(items, [target])
        if not found:
            unknown.append(target)
            continue
        for index in sorted(found):
            matched.append((items[index], why))
    return matched, unknown


def _remedy_lines(
    stored: str,
    old_text: str,
    items: Sequence[Disappeared],
    missing: Sequence[Disappeared],
    alternatives: Sequence[str],
) -> list[str]:
    """照做的下一步：**能照抄的调用**，而不是一句「改用 replace_lines」。

    只点这个工作区真的注册过的工具——点一把没有的工具，模型照着做只会得到
    「未知工具」，比不给下一步更浪费。

    **这里不给 `confirm_rewrite` 的调用形状**：这一条话术是「这次重写被拦下、
    正文没有被暂存」时用的，那时根本不存在令牌。dogfood r3 实测：模型在
    `confirm_rewrite` 出现在工具索引里之后，自己编了一个令牌去确认一份不存在的
    暂存，连撞三次把运行撞停。所以「没有令牌」这件事必须说在明处。
    """
    lines: list[str] = []
    span = duplicate_span(old_text)
    if span is not None and "replace_lines" in alternatives:
        a, b, c, d = span
        removed = "\n".join(old_text.splitlines()[c - 1 : d])
        lines.append(
            f'replace_lines(path="{stored}", start_line={c}, end_line={d}, content="",'
            f' {EXPECT_PARAM}="<第 {c}–{d} 行的原文，逐字抄>")'
            f"  # 删掉第 {c}–{d} 行那份重复内容（它与第 {a}–{b} 行逐行相同）"
        )
        lines.append(
            "# 那一段的原文现在长这样（抄进 expect 即可）：\n"
            f"<expect>\n{removed}\n</expect>"
        )
    ids = _ids_of(items, missing)
    ack = ", ".join(f'"{one}"' for one in ids)
    lines.append(
        f'write_file(path="{stored}", content=<你要交的完整内容>, '
        f'{REWRITE_REASON_PARAM}="为什么这是一次合法大改（合并/去重/搬运）"'
        + (f", {ACK_PARAM}=[{ack}]" if ids else "")
        + ")"
    )
    if ids:
        lines.append(
            "# 带上 rewrite_reason 之后正文会被收下并返回令牌 token；"
            f"清单没凑齐时再用 {CONFIRM_TOOL}(token=..., {ACKED_PARAM}=[...]) 补，"
            "只发项 id、不要重发正文（现在没有令牌，别调它）"
        )
    return lines


def _refusal_text(
    stored: str,
    old_text: str,
    old_lines: int,
    new_lines: int,
    items: Sequence[Disappeared],
    reason: str,
    missing: Sequence[Disappeared],
    *,
    shrink: bool,
    alternatives: Sequence[str] = ("replace_lines",),
) -> str:
    """拒绝话术。三件事必须说清：为什么拦、按这次任务的形状该怎么走、照着做什么。"""
    parts = [f"{REFUSAL_MARKER}：{stored} 从 {old_lines} 行变成 {new_lines} 行"]
    if shrink:
        parts.append(f"（减少 {old_lines - new_lines} 行，超过一半）")
    parts.append("。\n")
    if reason:
        parts.append(
            f"你已经声明了这次重写（{reason}），但消失清单里还有 "
            f"{len(missing)} 项没有交代："
            + "、".join(f"{one} {item.token}" for one, item in zip(_ids_of(items, missing), missing))
            + "。\n"
        )
    else:
        parts.append(
            "这可能是丢内容，也可能是**合法的大改**——合并、去重、搬运这类任务"
            "行数本来就会大减。\n"
        )
    if items:
        parts.append(
            f"消失清单（旧内容里有、新内容里没有，共 {len(items)} 项，"
            "每项前面是项 id）：" + _render_items(items) + "。\n"
        )
    else:
        parts.append(
            "新内容里没有识别出消失的函数／类名、id、事件处理器或 CSS 选择器。\n"
        )
    span = duplicate_span(old_text)
    precise = "replace_lines" in alternatives or "replace_text" in alternatives
    if span is not None:
        a, b, c, d = span
        parts.append(
            f"这份文件里第 {c}–{d} 行与第 {a}–{b} 行逐行相同（{b - a + 1} 行），"
            "像是同一份内容出现了两次——合并类任务就是这种形状。\n"
        )
    if missing:
        ids = "、".join(_ids_of(items, missing))
        if reason:
            parts.append(
                f"清单必须**逐项交代**才放行：给每一项填进 {ACK_PARAM}"
                f"（项 id，如 {ids}），或用 {DROPPED_PARAM} 声明有意丢弃并写明理由；"
                "两者都没给的项，这次重写不会落盘。\n"
            )
        else:
            parts.append(
                f"清单还没有交代：先带上 {REWRITE_REASON_PARAM} 说明这次大改做了什么——"
                "正文会先收下并给你一个令牌，再逐项交代（只发项 id，不用重发正文）；"
                "交代不了就说明这次重写确实丢了内容，不要落盘。\n"
            )
    else:
        if items:
            parts.append(
                f"要合并／去重：带上 {REWRITE_REASON_PARAM} 说明这次大改做了什么，"
                f"再把上面消失的每一样填进 {ACK_PARAM} 或声明丢弃，再重提一次。\n"
            )
        else:
            parts.append(
                f"要合并／去重：带上 {REWRITE_REASON_PARAM} 说明这次大改做了什么，"
                "再重提一次——这次没有识别出消失项，声明本身就是全部举证。\n"
            )
    if not reason:
        # 这次**没有暂存**，所以没有令牌——必须说在明处。
        #
        # dogfood r3 实测：`confirm_rewrite` 出现在工具索引里之后，模型在这一次
        # 拒绝之后自己编了个令牌 `rw-000001` 去确认一份不存在的暂存，连撞三次被
        # 机械层判成打转、整轮收手（7 步 / 134.7s / 文件零改动）。「没有令牌」
        # 不是模型能从上下文推出来的事，得直说。
        parts.append(
            f"这次**没有暂存正文，也没有令牌**：不要调用 {CONFIRM_TOOL}"
            "（它只认上一次写调用返回的令牌）。照做的是下面这一条。\n"
        )
    if precise:
        parts.append(
            f"要保住内容：改用 {'、'.join(alternatives)} 做局部修改——"
            "只改你要改的那一段，其余不动。\n"
        )
        parts.append(
            "若确实要删掉一半以上内容：用 replace_lines 删掉多余的那一段，"
            "剩下的部分再用 replace_text 改。\n"
        )
    else:
        # 纯 HTML/纯文本工作区里只有整份重写这一把写工具（`is_small_project`
        # 只看 *.py，所以大 HTML 项目也走这条）。此时「改用 replace_lines」
        # 是一条做不到的指令——dogfood 复跑就是这么卡的。
        parts.append(
            "这个工作区只有 write_file——replace_lines / replace_text 都没注册，"
            "别去找它们：要删掉一半以上内容，只能先声明（正文会被收下并给令牌）、"
            "再把消失清单逐项交代后落盘。\n"
        )
        parts.append(
            "整份重写时输出一旦被预算截断，写出去的会是半份坏文件——"
            "交代不了清单就别落盘，先 read_file 确认要删的到底是哪一段。\n"
        )
    parts.append(
        "不得原样重提同一个调用：失败过的参数原样重提会被硬拦，"
        "也不会因此多得一步预算。\n"
    )
    for line in _remedy_lines(stored, old_text, items, missing, alternatives):
        parts.append(f"{NEXT_STEP_MARKER}{line}\n")
    return "".join(parts)


def _stage_text(
    record: StagedRewrite,
    missing: Sequence[Disappeared],
    alternatives: Sequence[str] = ("replace_lines",),
) -> str:
    """暂存回执：**正文收下了**，清单还没交代完。

    这一段是这一轮改动对模型的主要出口，所以三件事必须写得能照抄：
    ① 文件尚未落盘 + 令牌是什么；② 清单（每项带稳定项 id）；③ 确认时
    **只发项 id、绝不重发正文**。第三句是整件事的关键——不写清楚，模型
    会照老习惯把 120 行正文再发一遍，输出预算就又爆了。
    """
    ids = _ids_of(record.items, missing)
    token = record.token
    ack_list = ", ".join(f'"{one}"' for one in ids)
    parts = [
        f"{STAGE_MARKER}：{record.path} 的正文已收下（{record.lines} 行），"
        f"但消失清单还有 {len(missing)} 项没交代，所以**文件尚未落盘**。\n",
        f"令牌 {TOKEN_PARAM}={token}（只在本次运行内有效）。\n",
    ]
    if record.replaced:
        parts.append(
            "同一路径上更早的那次暂存已被这次取代（旧令牌 "
            + "、".join(record.replaced)
            + " 不再可用）。\n"
        )
    if record.items:
        parts.append(
            f"消失清单（旧内容里有、新内容里没有，共 {len(record.items)} 项；"
            "前面是项 id）：" + _render_items(record.items) + "。\n"
        )
    else:
        parts.append("这次没有识别出消失项。\n")
    if ids:
        parts.append(
            "清单里每一项二选一，**确认时只发项 id，不要把正文再发一遍**：\n"
            "  · 确认保留（这一项在新内容里确实没有了，而且这样是对的）：\n"
            f'      {CONFIRM_TOOL}({TOKEN_PARAM}="{token}", {ACKED_PARAM}=[{ack_list}])\n'
            "  · 有意丢弃（你主动决定不要它，必须写理由）：\n"
            f'      {CONFIRM_TOOL}({TOKEN_PARAM}="{token}", '
            f'{DROPPED_PARAM}=["{ids[0]}{DROP_REASON_SEPARATOR}为什么这一项可以不要"])\n'
            "两种可以混在同一次调用里。\n"
            "两者都没给的项只要还在，这次重写就不会落盘（硬要求，不会松）。\n"
        )
    if "replace_lines" in alternatives:
        parts.append(
            f"要保住内容也可以改用 {'、'.join(alternatives)} 只动该动的那一段。\n"
        )
    parts.append(
        f"{NEXT_STEP_MARKER}{CONFIRM_TOOL}({TOKEN_PARAM}=\"{token}\", "
        f"{ACKED_PARAM}=[{ack_list}])  # 只发项 id，正文已经收下，不用再发一遍\n"
    )
    return "".join(parts)


def register_edit_tools(registry: Any, root: Path, pending: PendingChanges) -> None:
    """按项目规模决定给哪几把编辑工具。

    集中在一处：主循环和子智能体各注册一遍的话，两边的判定迟早会分叉，
    而那种分叉只会表现为「同一个项目里子智能体做得到、主循环做不到」。

    **注意 replace_text 不在这里注册**，这是量出来的结论，不是漏了。
    实测：给 6 道本来全过的题加上它之后，6 道全挂（0/6，连 token 数都一致），
    而且两次跑完全复现。它确实会被调用——问题出在拼出来的片段本身是坏的
    （比如 `try:` 下面少一个 `except`、缩进也不对）。这个模型擅长的是
    「交出一份完整文件」（单发 50/50 就是那个形态），不擅长拼片段。
    工具实现留着（有单元测试），换模型时可以再量一次。

    注册结果还要**告诉写工具**（`alternatives`）：拒绝话术里点到一把模型没有
    的工具，就等于把出路指到墙外。dogfood 复跑实测——纯 HTML 工作区里
    `replace_lines` 根本没注册，而那正是旧话术让模型去做的事。

    **确认入口跟着 write_file 一起注册**：它管的是「正文已经暂存、清单还没
    交代完」那一回合。少了它，暂存下去的正文就没人能确认——那比不暂存更糟。

    **删除文件与写工具一起给**：合并/去重类任务里「删掉多余那一份」是任务的
    另一半，只有写工具时模型只能让用户自己动手。两者一起注册也保证
    「提示词里提到的工具」与「真的能用」始终一致。
    """
    small = is_small_project(root)
    alternatives = () if small else ("replace_lines",)
    registry.register(write_file_spec(root, pending, alternatives=alternatives))
    registry.register(confirm_rewrite_spec(root, pending))
    registry.register(delete_file_spec(root, pending))
    if not small:
        registry.register(
            replace_lines_spec(root, pending, alternatives=alternatives)
        )


def confirm_rewrite_spec(root: Path, pending: PendingChanges) -> ToolSpec:
    """确认入口：**只吃清单项，不吃正文**。

    这是这一轮改的核心。原先「清单必须一次凑齐」把举证与正文绑在同一回合里，
    而清单是工具算出来的——模型第一次调用必然凑不齐，于是正文被发两遍，
    输出预算在 2457 token 上直接崩（dogfood r1–r3 连 `path` 都没发出来）。

    现在正文在提出时就被收下了（`write_file` 带 rewrite_reason 那次），
    确认回合只给项 id：`acked` 是确认保留，`dropped` 是声明有意丢弃（必须带
    理由）。两者都没给的项还在 → 不落盘并点名。
    """

    def handler(args: dict) -> ToolResult:
        token = str(args.get(TOKEN_PARAM) or "").strip()
        if not token:
            return ToolResult(
                ok=False,
                content=(
                    f"{REFUSAL_MARKER}：{CONFIRM_TOOL} 需要 {TOKEN_PARAM}。"
                    "令牌是上一次带 rewrite_reason 的写调用返回的那一个"
                    "（形如 rw-1a2b3c）；找不到就重新发起一次 write_file 拿新的。"
                ),
            )
        record, why = pending.lookup_staged(token)
        if record is None:
            return ToolResult(
                ok=False, content=_stale_token_text(pending, token, why)
            )

        acked = args.get(ACKED_PARAM)
        acked_list = [str(item) for item in acked] if isinstance(acked, list) else []
        pairs, why_bad = _split_dropped(args.get(DROPPED_PARAM))
        if why_bad:
            return ToolResult(ok=False, content=f"{REFUSAL_MARKER}：{why_bad}。\n")
        dropped, unknown_dropped = _dropped_targets(record.items, pairs)
        unknown_ids = _unknown_entries(record.items, acked_list) + unknown_dropped

        missing = _unaccounted(
            record.items, acked_list, [target for target, _ in pairs]
        )
        if missing:
            # **硬要求**：没交代的项只要还在，就不落盘，而且要点名是哪几项。
            names = "、".join(
                f"{one} {item.token}"
                for one, item in zip(_ids_of(record.items, missing), missing)
            )
            hint = ""
            if unknown_ids:
                hint = (
                    "另外，" + "、".join(unknown_ids) + " 不在这次的清单里"
                    "（清单项 id 见上一次的返回）。\n"
                )
            return ToolResult(
                ok=False,
                content=(
                    f"{REFUSAL_MARKER}：{record.path} 还有 {len(missing)} 项没交代，"
                    f"这次没有落盘：{names}。\n"
                    + hint
                    + "每一项二选一：确认保留就填进 "
                    f"{ACKED_PARAM}（项 id），有意丢弃就填进 {DROPPED_PARAM}"
                    f'（形如 ["d1{DROP_REASON_SEPARATOR}为什么可以丢"]）。\n'
                    f"**不用重发正文**：正文（{record.lines} 行）还在暂存里，"
                    f"令牌 {token} 仍然有效。\n"
                ),
            )

        declared = _declaration_note(
            record.reason,
            _acked_items(record.items, acked_list),
            record.shrink,
            dropped,
        )
        change = pending.take_staged(token, declared)
        if change is None:  # pragma: no cover - 取出与搬入之间没有别处会动它
            return ToolResult(
                ok=False, content=_stale_token_text(pending, token, "unknown")
            )

        parts = [
            f"已按你的交代落成待确认改动：{change.path}"
            f"（{_count_lines(change.new_text)} 行，尚未写入磁盘，等用户确认）。\n",
            f"留档：{declared}\n",
        ]
        if dropped:
            parts.append(
                "有意丢弃（已逐项记进留档，报告里看得到）：\n"
                + "".join(
                    f"  - {item_id(record.items.index(item))} {item.token}"
                    f" ← {why}\n"
                    for item, why in dropped
                )
            )
            parts.append(
                "注意：丢弃是**你的声明**，不是系统替你判断的。"
                "落盘后这一项就没有了。\n"
            )
        if unknown_ids:
            parts.append(
                "（" + "、".join(unknown_ids) + " 不在这次的清单里，已忽略。）\n"
            )
        parts.append(f"\n{change.diff}")
        return ToolResult(ok=True, content="".join(parts))

    return ToolSpec(
        name=CONFIRM_TOOL,
        description=(
            "给已暂存的大重写逐项交代消失清单：acked 是确认保留，"
            "dropped 是声明有意丢弃（每项都要写理由）。"
            "只发项 id 与理由，**不要重发正文**——正文在你带 rewrite_reason "
            "的那次写调用里已经收下了。两者都没给的项只要还在，就不会落盘。"
            "**只在写调用返回过令牌之后用它**：没有令牌（比如那次大重写没带 "
            "rewrite_reason、被拒了）时这里没有暂存可确认，"
            "该做的是带 rewrite_reason 重提一次。"
        ),
        parameters={
            "type": "object",
            "properties": {
                TOKEN_PARAM: {
                    "type": "string",
                    "description": "写调用返回的暂存令牌（形如 rw-1a2b3c）",
                },
                ACKED_PARAM: {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "确认保留的清单项 id（如 [\"d1\",\"d2\"]）",
                },
                DROPPED_PARAM: {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "有意丢弃的项，每项写成「项id：为什么可以丢」，"
                        "例如 [\"d3：这段重复脚本整段删掉\"]；理由不许省"
                    ),
                },
            },
            "required": [TOKEN_PARAM],
            "additionalProperties": False,
        },
        handler=handler,
        brief="逐项交代暂存的重写（只发项 id）",
        group="改",
    )


def _acked_items(
    items: Sequence[Disappeared], acked: Sequence[str]
) -> list[Disappeared]:
    """`acked` 里真正对上清单的那几项（顺序按清单）。"""
    matched = _match_items(items, acked)
    return [item for index, item in enumerate(items) if index in matched]


def _stale_token_text(pending: PendingChanges, token: str, why: str) -> str:
    """令牌不可用时的人话：说清没执行、内容去哪了、怎么重新发起。

    两条硬要求在这里落地：**不许静默当成功**（所以 ok=False 且带拒绝标记，
    循环据此认定这次调用没有执行），**不许静默丢弃暂存内容**（所以要说清
    还有几份暂存、以及重新发起一次写调用就能拿新令牌）。
    """
    if why == "expired":
        head = (
            f"{REFUSAL_MARKER}：令牌 {token} 已过期"
            f"（时效 rewrite_stage_ttl_seconds），这次确认没有执行。"
            "按上界规则，过期的那份暂存正文已被清理——它没有落盘，"
            "也没有被悄悄写进文件。\n"
        )
    else:
        head = (
            f"{REFUSAL_MARKER}：没有这个令牌 {token}"
            "（可能抄错了，也可能来自上一次运行——令牌只在提出它的那次运行里有效）。"
            "这次确认没有执行，也没有改动任何文件。\n"
        )
    live = pending.staged_items()
    if live:
        held = "、".join(
            f"{record.path}（{TOKEN_PARAM}={record.token}）" for record in live
        )
        tail = (
            f"现在还暂存着：{held}。"
            "这些正文还在，用它们各自的令牌就能确认。\n"
        )
    else:
        tail = (
            "现在没有暂存的重写——**不要再拿这个令牌重试**，重试多少次都不会有"
            "东西可确认。\n"
        )
    return (
        head
        + tail
        + "要重新发起：再调用一次 write_file(path=..., content=<完整内容>, "
        f"{REWRITE_REASON_PARAM}=...)，正文会被收下并返回一个新令牌，"
        "然后只需补确认（不用反复交正文）。\n"
    )


def replace_text_spec(root: Path, pending: PendingChanges) -> ToolSpec:
    """按唯一片段替换。改一处最省事的做法。

    另两把工具各有死穴，实测都撞过：replace_lines 要算行号，模型会算错区间、
    留下重复行和悬空语句（5 条失败死在它手上）；write_file 要复现整份，
    风险是丢掉原有内容，而且内容要经过 JSON 转义，出错面更大。

    片段替换两头都躲开了：只给出被替换的那一小段和新内容，既不需要行号，
    也不需要重写整个文件。代价是要求那段在文件里唯一——不唯一就报错，
    让它多带几行上下文，比猜一处改错强。
    """

    def transform(old_text: str, args: dict) -> str:
        old = args["old"]
        new = args["new"]
        if not old:
            raise ValueError("old 不能为空；要新增内容请用 write_file")
        if old not in old_text:
            raise ValueError(
                "文件里找不到这段内容。先 read_file 确认原文——"
                "缩进、空格、引号都要和文件里完全一致"
            )
        count = old_text.count(old)
        if count > 1:
            raise ValueError(
                f"这段内容在文件里出现了 {count} 次，无法确定改哪一处。"
                "请多带几行上下文（比如把上一行也包含进来），"
                "让它在文件里只出现一次"
            )
        return old_text.replace(old, new, 1)

    return ToolSpec(
        name="replace_text",
        description=(
            "把文件里某段唯一的内容替换成新内容；只生成 diff，需用户确认后才写入。"
            "改一处就用它：不必算行号，也不必把整个文件重写一遍"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要改的文件"},
                "old": {
                    "type": "string",
                    "description": "被替换的原内容，必须在文件里唯一出现",
                },
                "new": {
                    "type": "string",
                    "description": "替换成什么；要删掉这段就传空字符串",
                },
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
            handler=lambda args: _propose(
            root, pending, args, lambda old_text: transform(old_text, args)
        ),
        brief="按片段替换",
        group="改",
    )


def make_diff(path: str, old: str, new: str) -> str:
    """生成统一格式的差异文本。"""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


@dataclass
class PendingChange:
    """一处待授权的修改。"""

    path: str
    old_text: str
    new_text: str
    # 这处改动是**声明式重写**时留下的痕迹（为什么大改、确认了哪些消失项）。
    # 沿用既有的待确认清单当留档：不新造一套日志，用户确认时看得到理由。
    declared: str = ""
    # 这是一处**删除**（不是把文件写成空内容）。两者在磁盘上的结果相同，
    # 在台账上完全不同：删除的 `old_text` 是全文，回滚要把它写回去；
    # 而「把文件写成空」回滚之后剩下的也是一个空文件。
    # 靠 `old_text` 反推不出来，所以显式记一位。
    deleted: bool = False

    @property
    def is_new_file(self) -> bool:
        # 删除的 old_text 非空，所以它不会被误判成「新建」。
        return not self.old_text and not self.deleted

    @property
    def diff(self) -> str:
        if self.deleted:
            # 删除的 diff 用「文件 → /dev/null」：用户一眼看得出这是删除，
            # 而不是「把一个文件改空」。
            body = "".join(f"-{line}" for line in self.old_text.splitlines(True))
            return f"--- a/{self.path}\n+++ /dev/null\n{body}"
        if self.is_new_file:
            body = "".join(f"+{line}" for line in self.new_text.splitlines(True))
            return f"--- /dev/null\n+++ b/{self.path}\n{body}"
        return make_diff(self.path, self.old_text, self.new_text)


class StageLimitError(RuntimeError):
    """暂存上界挡下了一次重写。

    话术由调用方给（它知道这次是在哪条路径上被挡的、模型该怎么做），
    这里只负责把「为什么挡」说清楚。
    """


@dataclass
class StagedRewrite:
    """一份已声明、但消失清单还没交代完的重写正文。

    **沿用 `PendingChange`**：同一份 dataclass、同一套留痕字段，只是暂时不放进
    `_changes`——确认之前它既不在待确认清单里，也不会被 `apply()` 写到磁盘。
    """

    token: str
    change: PendingChange
    items: tuple[Disappeared, ...]
    reason: str
    shrink: bool
    created_at: float
    # 同一路径上被这次取代的旧令牌（说出来，不静默丢）
    replaced: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.change.path

    @property
    def lines(self) -> int:
        return _count_lines(self.change.new_text)

    def ids(self) -> tuple[str, ...]:
        return tuple(item_id(index) for index in range(len(self.items)))

    def expired(self, now: float, ttl: float) -> bool:
        """时效过没过。ttl <= 0 表示不设时效（可覆盖旋钮调到 0 的语义）。"""
        return ttl > 0 and now - self.created_at > ttl


def staged_rewrite_key(pending, token: str) -> str:
    """暂存区里「模型点名的这份重写」现在长什么样；**真的取不到**返回空串。

    给重复调用护栏用：`confirm_rewrite` 的参数里**既没有正文也没有路径**
    （只有 token 与逐项交代），所以护栏原先拿不到任何可比对象，只能归成
    「判不出」并放行——真机上于是出现了同一个 stale 令牌连续 18 次
    （令牌早被 `take_staged` 用掉或已被同路径的再次暂存顶掉，每次拿到的
    都是同一份「没有这个令牌」的拒绝，改不了结果）。

    刻意返回**可读的状态串**而不是 sha1：这条链路的证据要能直接贴进拒绝
    话术里给人看，短哈希在这里省不了几个字，却让「为什么判成重复」变回
    一句无法核对的话。

    指纹**不含 created_at**：同一份重写放久了不是「变了」，把它算进去会让
    每一次重复都看起来像「有理由的重取」，等于没拦。
    """
    def _text(value: object) -> str:
        return value if isinstance(value, str) else str(value or "")

    record, _why = pending.lookup_staged(token)
    if record is not None:
        # 查得到：把这份记录的全部可见内容都收进来（正文只取长度与行数，
        # 逐字比由同一个签名里的参数负责——参数一字未改就已经说明了一切）。
        return (
            f"staged token={_text(record.token)} path={_text(record.path)} "
            f"lines={record.lines} items={len(record.items)} "
            f"replaced={len(record.replaced)} chars={len(record.change.new_text)} "
            f"reason={_text(record.reason)} shrink={bool(record.shrink)}"
        )
    # 查不到：令牌作废时，**真正能变的只有清单**（模型可能带了对的意图却拿错
    # 了令牌，重新提一次就变）。清单也一字未动 → 再发一次拿到的还是同一份拒绝。
    staged = [f"{item.token}:{item.path}:{item.lines}" for item in pending.staged_items()]
    changes = [f"{item.path}:{len(item.new_text)}:{item.deleted}" for item in pending.items()]
    if not staged and not changes:
        return ""
    return f"no token={token} staged=[{' '.join(staged)}] changes=[{' '.join(changes)}]"


def _now() -> float:
    """暂存时效用的时钟。

    单独一个函数是为了让测试能**真的把时间推过去**（monkeypatch 它），
    而不是靠 sleep 等 30 分钟。
    """
    return time.monotonic()


def _new_token(path: str, seq: int) -> str:
    """短令牌：`rw-` + 6 位十六进制。

    短是刻意的：它每一轮都要出现在模型的上下文里（以及模型回填的参数里），
    长令牌是每次都付的税。够用就行——它只需要在**本次运行**里唯一。
    """
    digest = hashlib.sha1(f"{path}|{seq}|{_now()}".encode("utf-8")).hexdigest()[:6]
    return f"rw-{digest}"


class PendingChanges:
    """本次运行中提出、尚未落盘的修改集合。"""

    def __init__(
        self,
        root: Path,
        extra_roots: Sequence[Path] = (),
        overrides: dict | None = None,
        run_id: str = "",
    ) -> None:
        self._root = root.resolve()
        self._extra_roots = tuple(Path(item).resolve() for item in extra_roots)
        self._changes: dict[str, PendingChange] = {}
        # ---- 运行记录（`.agent/runs/<id>/`）--------------------------------
        #
        # **运行要可识别**：收尾报告里那句可复制的撤销命令必须点名「退的是哪一轮」，
        # 而用户事后要能拿同一个名字把整轮退回去。id 在待确认清单建出来的时候就定，
        # 因为报告与落盘是两个时刻（报告先说、落盘在收尾），中间没有第二次机会。
        #
        # 记录目录是**落盘那一刻**才建的（`apply`）：只读运行、被拒的运行不该
        # 在用户工作区里留下一堆空目录。
        self.run_id = run_id or runlog.new_run_id()
        self.run_session = ""
        self.run_goal = ""
        self.started_at = runlog.now_iso()
        # 最终验证结论：由循环在收尾那一刻写进来，落盘时随记录一起落。
        # 它是「最终验证：未通过 + 撤销命令放在最前」那句话的出处。
        self.verification: tuple[bool, str] | None = None
        self._landed: list[runlog.RunEntry] = []
        self._refused: list[RefusedChange] = []
        # 已声明、清单还没交代完的重写：token → 暂存记录。
        #
        # 与 `_changes` 分开的理由是**语义**，不是洁癖：`_changes` 里的东西
        # 「确认了就会落盘」，而暂存里的东西「确认清单没交代完，落盘是不允许的」。
        # 混在一起的话，`--policy auto` 会在收尾时把没交代完的重写直接写进
        # 用户的工作区——那正是这道墙要拦的事。
        self._staged: dict[str, StagedRewrite] = {}
        self._stage_seq = 0
        # 上界旋钮走标定表，可按工作区覆盖；不显式传就用工作区保存的那一份。
        self._overrides = (
            dict(overrides) if overrides is not None else limits.load_overrides(self._root)
        )

    def _knob(self, name: str) -> float:
        value, _ = limits.resolve(name, self._overrides)
        return value

    def __len__(self) -> int:
        return len(self._changes)

    def items(self) -> list[PendingChange]:
        return list(self._changes.values())

    def disk_path(self, stored: str) -> Path:
        """待确认记录里的路径 → 磁盘上的绝对路径。

        工作区内记相对路径；关联目录记绝对路径。
        """
        raw = Path(stored)
        if raw.is_absolute():
            return raw
        return self._root / stored

    def is_linked(self, stored: str) -> bool:
        """这条改动是不是关联目录里的（不走自动落盘）。"""
        return Path(stored).is_absolute()

    def path_of(self, candidate: str) -> str:
        """把用户给的路径解析成待确认记录用的键。"""
        target = resolve_readable(self._root, candidate, self._extra_roots)
        try:
            return target.relative_to(self._root).as_posix()
        except ValueError:
            return str(target)

    def _pre_image(self, target: Path) -> str:
        """登记这一刻磁盘上的原文；读不出来就返回空串。

        **不许在这里抛**：读不出来的文件（二进制/坏编码）当然不该被改，但
        「为什么不该改」这句话要说在落盘那一刻、并且带着解决办法（见
        `_baseline_entry`）——在这里抛出去，模型只会看到一句栈里的
        UnicodeDecodeError，然后照原样再试一次。
        """
        if not target.exists():
            return ""
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def propose(self, path: str, new_text: str, declared: str = "") -> PendingChange:
        """登记一处修改。同一路径重复提出时以最后一次为准。

        `declared` 是声明式重写的留痕（为什么大改、确认了哪些消失项），
        跟着这处改动一起走：用户确认的那一刻看得到理由。
        """
        target = self.disk_path(path)
        old_text = self._pre_image(target)
        change = PendingChange(
            path=path, old_text=old_text, new_text=new_text, declared=declared
        )
        self._changes[path] = change
        # 同一路径上有更早的暂存时，这一份更晚的直接提交取代它：`_changes` 的
        # 既有口径就是「同一路径以最后一次为准」，暂存跟着走才不会有两条互相
        # 打架的记录。取代这件事会在下次取旧令牌时如实说出来（不是静默丢）。
        for token, record in list(self._staged.items()):
            if record.path == path:
                self._staged.pop(token, None)
        return change

    def propose_delete(
        self, path: str, old_text: str, declared: str = ""
    ) -> PendingChange:
        """登记一处删除。`old_text` 是删除前的全文——它就是回滚的退路。

        与 `propose` 分开写（而不是加一个布尔参数）是因为两者对 old_text 的
        期待正好相反：`propose` 里 old_text 是**从磁盘读出来的**当前内容，
        删除这里则是**由调用方交进来的**（磁盘上的内容此刻就是它），
        写成两个入口，两个方向都不会被读错。
        """
        change = PendingChange(
            path=path,
            old_text=old_text,
            new_text="",
            declared=declared,
            deleted=True,
        )
        self._changes[path] = change
        for token, record in list(self._staged.items()):
            if record.path == path:
                self._staged.pop(token, None)
        return change

    # ---- 已声明重写的暂存区 -------------------------------------------------

    def staged_count(self) -> int:
        """还有几份暂存的重写没被交代完（收尾报告用它说话）。"""
        return len(self._staged)

    def staged_items(self) -> list[StagedRewrite]:
        return list(self._staged.values())

    def _purge_expired(self) -> list[str]:
        """清掉过期的暂存，返回被清掉的令牌。

        过期**就是**上界的一部分（时效到了不留），但清掉这件事必须能被说出来
        ——所以返回名单，由调用方决定怎么告诉模型。
        """
        ttl = self._knob("rewrite_stage_ttl_seconds")
        now = _now()
        gone = [
            token
            for token, record in self._staged.items()
            if record.expired(now, ttl)
        ]
        for token in gone:
            self._staged.pop(token, None)
        return gone

    def stage(
        self,
        change: PendingChange,
        items: Sequence[Disappeared],
        reason: str,
        shrink: bool,
    ) -> StagedRewrite:
        """暂存一份重写正文，返回带令牌的记录。**上界先判再存**。

        三道闸（条数 / 单份字符 / 合计字符）任一超了就抛 `StageLimitError`，
        并把「现在是多少、上界是多少」写进话术——模型与人都要能回答
        「为什么这次没被收下」。
        """
        self._purge_expired()
        size = len(change.new_text)
        cap = int(self._knob("rewrite_stage_chars"))
        if cap > 0 and size > cap:
            raise StageLimitError(
                f"这份正文 {size} 字符，超过单份暂存上界 {cap} 字符"
                f"（rewrite_stage_chars）。这么大的重写不适合整份交出："
                "拆成几次局部修改（replace_lines / replace_text），"
                "或者先把文件缩小到能一次交完。"
            )
        total_cap = int(self._knob("rewrite_stage_total_chars"))
        held = sum(len(record.change.new_text) for record in self._staged.values())
        limit = int(self._knob("rewrite_stage_limit"))

        # 同一路径上已有的暂存由这一份取代：先摘掉，再判条数——否则
        # 「反复提同一个文件」会把自己的位置占满，而那是假的上界。
        replaced = tuple(
            token for token, record in self._staged.items() if record.path == change.path
        )
        for token in replaced:
            self._staged.pop(token, None)

        if limit > 0 and len(self._staged) >= limit:
            held_paths = "、".join(record.path for record in self._staged.values())
            raise StageLimitError(
                f"同时最多暂存 {limit} 份未交代完的重写（rewrite_stage_limit），"
                f"现在占着的是：{held_paths}。"
                "先把它们交代完（或声明有意丢弃）——那些正文还在，不会因此丢；"
                "实在不要了就等令牌过期，或把这一份并进上一次的重写里。"
            )
        if total_cap > 0 and held + size > total_cap:
            raise StageLimitError(
                f"暂存正文合计会到 {held + size} 字符，超过上界 {total_cap} "
                f"（rewrite_stage_total_chars）。先交代完已有的暂存再提这一份。"
            )

        self._stage_seq += 1
        token = _new_token(change.path, self._stage_seq)
        record = StagedRewrite(
            token=token,
            change=change,
            items=tuple(items),
            reason=reason,
            shrink=shrink,
            created_at=_now(),
            replaced=replaced,
        )
        self._staged[token] = record
        return record

    def lookup_staged(self, token: str) -> tuple[StagedRewrite | None, str]:
        """按令牌取暂存：返回（记录, 失效原因）。

        失效原因分两种，话术不同——「这个令牌我没见过」（多半是抄错了，或
        来自另一次运行）与「这个令牌过期了」（内容已按时效清理）。两种都不许
        静默当成功，也不许静默丢弃：调用方必须把原因说给人听。
        """
        record = self._staged.get(token)
        if record is None:
            expired = self._purge_expired()
            if token in expired:  # pragma: no cover - 取时已清，见下一条分支
                return None, "expired"
            return None, "unknown"
        ttl = self._knob("rewrite_stage_ttl_seconds")
        if record.expired(_now(), ttl):
            # 过期即清理：时效是上界的一部分。清掉之前把「它确实存在过、
            # 里面有多少行」记下来，好让话术说清楚丢的是什么。
            self._staged.pop(token, None)
            return None, "expired"
        return record, ""

    def take_staged(self, token: str, declared: str) -> PendingChange | None:
        """把暂存的那份改动**搬进待确认清单**——这一步之后它才可能落盘。

        `declared` 是确认回合补齐的留痕（确认保留哪些、有意丢弃哪些及理由）：
        留在待确认清单上，用户确认的那一刻看得到。
        """
        record = self._staged.pop(token, None)
        if record is None:
            return None
        change = PendingChange(
            path=record.change.path,
            old_text=record.change.old_text,
            new_text=record.change.new_text,
            declared=declared,
        )
        self._changes[change.path] = change
        return change

    def apply(self) -> list[str]:
        """把全部待授权修改写入磁盘，返回已写入的路径。

        落盘之前先构造基线。没有基线的写操作等于没有退路，
        而「确认错了」是必然会发生的——diff 看得再仔细也挡不住走神。

        删除在这里落地：`unlink` 之后文件真的没了，而基线里留着它的全文，
        所以 `revert` 能把它恢复回来——「可回滚」这条对删除同样成立。

        **给不出基线的改动不许落盘**（不变量）：这一条是唯一允许「扣成果」的
        理由——一个退不回去的改动留在用户工作区里，比少做一处改动坏得多。
        被扣掉的那几处连同原因记在 `refused()` 里，收尾报告会逐条说出来，
        绝不静默跳过。

        落成的每一处都记进运行记录（`.agent/runs/<id>/`）：改了什么、改动前
        是什么、落盘后指纹是什么。整轮回退（`spool revert --run <id>`）读的就是它。
        """
        written: list[str] = []
        entries: list[runlog.RunEntry] = []
        self._landed = []
        self._refused = []
        for change in self._changes.values():
            target = self.disk_path(change.path)
            entry, why = self._baseline_entry(change)
            if entry is None:
                self._refused.append(RefusedChange(path=change.path, reason=why))
                continue
            if change.deleted:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(change.new_text, encoding="utf-8")
            written.append(change.path)
            entries.append(
                runlog.RunEntry(
                    path=change.path,
                    old_text=entry.old_text,
                    existed=entry.existed,
                    deleted=change.deleted,
                    # 落盘后的指纹：「这一处退之前，磁盘上应该是什么」。
                    # 回退靠它认出「落盘之后又被别人改过」。
                    after=runlog.fingerprint(
                        None if change.deleted else change.new_text
                    ),
                    declared=change.declared,
                )
            )
        self._landed = entries
        self._changes.clear()
        if entries:
            runlog.append_landing(
                self._root,
                self.run_id,
                entries,
                session=self.run_session,
                goal=self.run_goal,
                started_at=self.started_at,
                verification=self.verification,
            )
        return written

    def _baseline_entry(self, change: PendingChange) -> tuple[BaselineEntry | None, str]:
        """这一处改动的**改动前状态**；给不出就返回 (None, 为什么)。

        口径是「**落盘这一刻**磁盘上是什么」，不是「登记那一刻台账里记了什么」。
        登记之后、落盘之前文件完全可能又被改过（人手工改的、别的工具改的），
        而回退要回到的是**它被覆盖之前**的样子。

        删除走同一条口径：删除前那一刻的全文，才是「恢复删除」要写回去的东西。
        拿一份过期的 `old_text` 写回去得到的是内容不对的文件；拿空串写回去
        得到的就是「留一个空文件」——那正是这条不变量要拦的形状。
        """
        target = self.disk_path(change.path)
        if not target.exists():
            # 之前不存在就没有「改动前的内容」可回填——这一轮自己造出来的东西
            # 不该在回滚时被当成原来就有的。
            return BaselineEntry(path=change.path, old_text=None, existed=False), ""
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return None, (
                f"改动前它就在磁盘上，但内容读不出来（{type(exc).__name__}）——"
                "没有改动前的原文就没有退路，所以这一处没有落盘"
            )
        return BaselineEntry(path=change.path, old_text=current, existed=True), ""

    def landed(self) -> list[runlog.RunEntry]:
        """上一次 `apply()` 真正落成的那几处（含每处的改动前状态）。"""
        return list(self._landed)

    def note_verification(self, passed: bool, summary: str) -> None:
        """记下收尾那一刻的**最终验证结论**。

        它是运行记录里的一栏，也是收尾报告「最终验证：未通过 + 撤销命令放在
        最前面」那句话的出处。落盘在报告之后，所以这里先把结论存下来。
        """
        self.verification = (bool(passed), str(summary))

    def refused(self) -> list["RefusedChange"]:
        """上一次 `apply()` 因为给不出基线而被扣掉的那几处。"""
        return list(self._refused)

    def unbaselined(self) -> list["RefusedChange"]:
        """**现在**这几处待落盘的改动，哪些给不出基线（`apply` 会扣掉它们）。

        与 `refused()` 的区别是时机：`refused()` 是「上一次 apply **已经**扣掉
        了什么」，这个是「下一次 apply **将要**扣掉什么」。收尾闸门（T11/T13）
        要的是后者——它判在落盘之前，那时 `_refused` 还是空的。
        """
        out: list[RefusedChange] = []
        for change in self._changes.values():
            entry, why = self._baseline_entry(change)
            if entry is None:
                out.append(RefusedChange(path=change.path, reason=why))
        return out

    def baseline(self) -> "Baseline":
        """把当前待写入的修改转成可持久化的基线。

        **「改动之前磁盘上有没有它」是一个事实，不能从 old_text 反推**：
        「把一份刚新建的文件删掉」与「把一份已有文件删掉」在待确认记录里长得
        很像，而回滚的动作正好相反（一个该删掉、一个该把内容写回去）。
        所以这里按 `disk_path().exists()` 判。

        给不出基线的那几处**不进基线**：它们也不会落盘（见 `apply`），
        进来只会让一份「覆盖了它」的记录凭空出现在退路里。
        """
        entries: list[BaselineEntry] = []
        for change in self._changes.values():
            entry, _why = self._baseline_entry(change)
            if entry is not None:
                entries.append(entry)
        return Baseline(entries=tuple(entries))

    def discard(self) -> None:
        self._changes.clear()
        # 暂存也一起清：`discard` 的语义是「这一批不要了」。留着一份没人能
        # 确认的正文只会占着上界，而它的正文本来就在模型那边的上下文里。
        self._staged.clear()

    def drop(self, paths: Sequence[str]) -> list[str]:
        """丢掉指定的几处改动，返回真正被丢掉的路径。

        存在的理由是一个实测事故：自主运行里某一步**越界**写了两处别的题，
        同时还有一处**在范围内、而且是对的**修改。整批一起作废的代价是
        那处对的修改也没了，审查据此判它没做完，整份计划在 43/49 处中止。
        「一条越界、全批作废」在无人值守时不是谨慎，是把对的部分一起扔掉。
        """
        removed = [path for path in paths if path in self._changes]
        for path in removed:
            self._changes.pop(path, None)
        return removed


@dataclass(frozen=True)
class RefusedChange:
    """一处**没有落盘**的改动，以及为什么。

    「给不出 baseline 的改动不许落盘」是那条不变量唯一允许「扣成果」的理由。
    扣掉这件事必须说出来——静默跳过会让人以为改好了，而那正是最坏的一种
    报告：文件没变，结论却说变了。
    """

    path: str
    reason: str


@dataclass(frozen=True)
class BaselineEntry:
    """一个文件的改动前状态。old_text 为 None 表示这个文件是新建的。

    `existed` 显式记「改动之前磁盘上有没有它」。它与 `old_text is None` 在
    绝大多数情况下一致，分成两位是因为删除与「新建后又改」在事后长得一样，
    而回滚的动作相反。缺省值让老基线文件照样读得动（读回来按 old_text 推）。
    """

    path: str
    old_text: str | None
    existed: bool = False


@dataclass(frozen=True)
class Baseline:
    """一次写操作的改动前快照。"""

    entries: tuple[BaselineEntry, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "entries": [
                    {
                        "path": e.path,
                        "old_text": e.old_text,
                        "existed": e.existed,
                    }
                    for e in self.entries
                ]
            },
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Baseline":
        payload = json.loads(text)
        return cls(
            entries=tuple(
                BaselineEntry(
                    path=item["path"],
                    old_text=item.get("old_text"),
                    existed=bool(
                        item.get(
                            "existed", item.get("old_text") is not None
                        )
                    ),
                )
                for item in payload.get("entries", [])
            )
        )


def save_baseline(path: Path, baseline: Baseline) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.to_json(), encoding="utf-8")


def load_baseline(path: Path) -> Baseline | None:
    if not path.exists():
        return None
    return Baseline.from_json(path.read_text(encoding="utf-8"))


def revert(root: Path, baseline: Baseline) -> list[str]:
    """把文件恢复到基线状态，返回被处理过的路径。

    新建的文件在回滚时删除——恢复到「它不存在」才算真的恢复。
    被删除的文件反过来：基线里存着它的全文，回滚要把它写回磁盘。
    """
    touched: list[str] = []
    for entry in baseline.entries:
        target = Path(entry.path) if Path(entry.path).is_absolute() else root / entry.path
        if not entry.existed:
            if target.exists():
                target.unlink()
            touched.append(entry.path)
            continue
        if entry.old_text is None:  # pragma: no cover - 与 existed 同进同出
            touched.append(entry.path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry.old_text, encoding="utf-8")
        touched.append(entry.path)
    return touched


def _normalize_span(text: str) -> str:
    """比对锚点时的口径：只忽略**行尾空白与换行风格**。

    不这么做的代价是真实的：模型从带行号的读取结果里抄原文时，末尾常常多一个
    空格（或反过来少一个），严格逐字比对会把一次**完全正确**的替换拒掉，而它
    除了一直重试没有别的办法。行首缩进不忽略——缩进错了就是抄错了内容。
    """
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))


def _line_edit_receipt(root: Path, pending: PendingChanges, args: dict) -> str:
    """一次按行替换之后的行号回执。

    用户点名的第 5 条发现是「行号漂移把文件改坏」。锚点挡住了**改错**，
    但「模型不知道自己手上的行号已经过期」这件事还在：它下一次接着用旧行号，
    锚点会拒绝，然后它得再读一遍——这一行回执把那次往返省掉。

    给的是三个数，不是整份内容：**新行数、新指纹、这次动了哪一段**。
    """
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return ""
    try:
        stored = pending.path_of(path)
        target = pending.disk_path(stored)
        # 必须走 WorkspaceView（叠加待确认改动之后的视图），不能读磁盘：
        # 这次改动还没落盘，读磁盘拿到的是**替换之前**的内容，回执里
        # 那一行行号就会指错地方——而回执的全部价值就是让人别再算错行号。
        text = WorkspaceView(root, pending).read_text(target)
    except (PathOutsideProjectError, OSError, UnicodeDecodeError, KeyError):
        return ""
    if not text:
        return ""
    lines = text.splitlines()
    start = args.get("start_line")
    end = args.get("end_line")
    count = len(text.splitlines())
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    head = (
        f"\n行号回执：第 {start}–{end} 行已被替换，"
        f"{stored} 现在是 {count} 行，指纹 {digest}。"
        "此前的行号已经失效——要再动别处请重新 read_file 确认行号，"
        f"并把目标区间的原文放进 {EXPECT_PARAM}。"
    )
    if isinstance(start, int) and isinstance(end, int):
        replacement = args.get("content")
        new_len = len(replacement.splitlines()) if isinstance(replacement, str) else 0
        if new_len:
            stop = min(start + new_len - 1, len(lines))
            touched = "\n".join(lines[start - 1 : stop])
            head += f"\n这次写入的是第 {start}–{stop} 行：\n{touched}"
    return head


def _propose(
    root: Path,
    pending: PendingChanges,
    args: dict,
    transform,
    alternatives: Sequence[str] = ("replace_lines",),
) -> ToolResult:
    """共用的提交流程：解析路径、读取原文、变换、登记、返回 diff。

    变换失败用异常表达。曾经让变换函数「成功返回新内容、失败返回错误说明」，
    两者都是字符串，结果成功路径被误判成失败——用异常区分才不会有歧义。

    `alternatives` 是这个工作区里**真的注册过**的精确编辑工具（由
    `register_edit_tools` 传进来）：拒绝话术只能点它们，否则出路指到墙外。
    """
    try:
        stored = pending.path_of(args["path"])
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    target = pending.disk_path(stored)
    try:
        # **必须读叠加视图**，不能读磁盘。用户点名的第 5 条发现正是这条：
        # 连续两次编辑同一个文件时，磁盘还是最老的那一版，于是第二次的
        # 行号与锚点都对着**上一次改动之前**的内容校验——锚点明明对不上
        # 「模型刚读到的内容」却通过了，第二次替换把第一次的结果整个盖掉，
        # 落盘的就是「少掉开始标签 / 多出结束标签」的那种文件。
        # 读视图之后，工具看到的世界与 read_file 给模型的世界是同一个。
        old_text = WorkspaceView(root, pending).read_text(target)
    except (OSError, UnicodeDecodeError):
        old_text = ""
    try:
        new_text = transform(old_text)
    except ValueError as exc:
        return ToolResult(ok=False, content=str(exc))

    if target.exists() and new_text == old_text:
        # 内容一字未变。照样登记的话，用户会被要求确认一处「什么都没改」的
        # 改动——diff 是空的，除了一点点养成「不看就点应用」的习惯，
        # 什么也换不来。同时也要让模型知道：这一写没产生改动，
        # 别以为自己已经改好了。
        return ToolResult(
            ok=True, content=f"{stored} 的内容与现有完全一致，没有产生改动。"
        )

    # 整文件重写已有文件时的两道闸（都在登记之前判，拦下的不进待确认清单）：
    # 行数大幅减少时提醒它检查有没有丢内容；超过一半时拒绝，逼它走
    # 「声明 + 逐项交代消失清单」或者做局部修改。
    reason, acked = _declaration(args)
    dropped_pairs, dropped_error = _split_dropped(args.get(DROPPED_PARAM))
    dropped_raw = [target for target, _ in dropped_pairs]
    old_lines = _count_lines(old_text)
    new_lines = _count_lines(new_text)
    items = disappeared_items(old_text, new_text) if target.exists() else []
    shrink = target.exists() and (
        old_lines > REWRITE_MIN_OLD_LINES
        and new_lines < old_lines * REWRITE_SHRINK_RATIO
    )
    if target.exists():
        if dropped_error and (reason or shrink):
            # 丢弃声明写坏了（多半是漏了理由）：不放行，也不暂存——正文这一轮
            # 已经在上下文里了，先把声明补成合法形状再说。
            return ToolResult(ok=False, content=f"{REFUSAL_MARKER}：{dropped_error}。\n")
        missing = _unaccounted(items, acked, dropped_raw)
        if shrink and not reason:
            # **未声明的大重写照旧硬拦**：这条护栏一个字都没松。
            return ToolResult(
                ok=False,
                content=_refusal_text(
                    stored, old_text, old_lines, new_lines, items, reason, missing,
                    shrink=True, alternatives=alternatives,
                ),
            )
        if reason and missing:
            # 声明了但清单还没交代完：**不再要求一次凑齐**。正文暂存下来、
            # 给一个短令牌，确认回合只发项 id —— 这是这一轮改的东西。
            staged_change = PendingChange(
                path=stored, old_text=old_text, new_text=new_text
            )
            try:
                record = pending.stage(staged_change, items, reason, shrink)
            except StageLimitError as exc:
                return ToolResult(
                    ok=False,
                    content=(
                        f"{REFUSAL_MARKER}：{stored} 的重写正文没有被收下——{exc}\n"
                        "这次没有落盘，正文也没有被暂存（本轮上下文里的正文不受影响）。\n"
                    ),
                )
            return ToolResult(
                ok=True, content=_stage_text(record, missing, alternatives)
            )
    elif dropped_error:
        return ToolResult(ok=False, content=f"{REFUSAL_MARKER}：{dropped_error}。\n")

    declared = _declaration_note(
        reason,
        [item for index, item in enumerate(items) if index in _match_items(items, acked)],
        shrink,
        _dropped_targets(items, dropped_pairs)[0],
    )
    change = pending.propose(stored, new_text, declared=declared)
    verb = "新建" if change.is_new_file else "修改"
    body = f"已生成{verb}预览（尚未写入，需用户确认）："
    if change.declared:
        body += f"\n{change.declared}"
    body += f"\n{change.diff}"

    if not change.is_new_file:
        # 整文件重写已有文件时，如果行数大幅减少，提醒模型检查有没有丢内容。
        # dogfood 暴露：重写 index.html 时 CSS 单位全丢（100vh→100%、400px→400）。
        # 不是 bug——是 27B 整文件重写时漏细节；提醒它再看一眼能拦住一部分。
        if old_lines > 10 and new_lines < old_lines * 0.7:
            body = (
                f"⚠ {stored} 从 {old_lines} 行变成 {new_lines} 行（少了 "
                f"{old_lines - new_lines} 行）。确认没有丢掉原有内容——"
                "整文件重写容易漏细节（比如 CSS 单位、闭合标签）。"
                "如果确实删了那么多，忽略此提示。\n\n" + body
            )
        # 没到硬拦线但已有名字消失时，把「消失清单」摆出来：它是模型自查
        # 有没有漏内容的唯一线索，而算它是免费的。
        if items:
            body = (
                "消失清单（旧内容里有、新内容里没有）："
                + _render_items(items)
                + "。确认这些确实该消失。\n" + body
            )

    # 测试文件是验收标准。改它不会让验证通过（验证按原始内容判定），
    # 但模型不知道这件事——实测它会把测试改成 assert True 然后宣布完成，
    # 白烧四步。与其事后拦，不如在它动手时说清楚。
    if is_test_path(stored):
        body = (
            "注意：这是测试文件。验证按测试的原始内容判定，"
            "改它不会让验证通过——测试失败说明代码不对。\n\n" + body
        )

    # 写完立刻检查结构约束。这是「软要求变硬反馈」的落点：
    # 模型不需要记住规则，只需要对具体违反项作出反应。
    feedback = format_violations(check_source(new_text, stored))
    if feedback:
        body = f"{body}\n\n{feedback}"
    return ToolResult(ok=True, content=body)


def write_file_spec(
    root: Path,
    pending: PendingChanges,
    alternatives: Sequence[str] = ("replace_lines",),
) -> ToolSpec:
    """整文件写入。对已存在的文件是整体替换，改动量大时慎用。

    `alternatives` 由 `register_edit_tools` 按实际注册结果给：纯 HTML/文本
    工作区里没有 replace_lines，拒绝话术就不得再劝它去用（做不到的指令）。
    """

    def handler(args: dict) -> ToolResult:
        return _propose(
            root, pending, args, lambda _old: args["content"],
            alternatives=alternatives,
        )

    return ToolSpec(
        name="write_file",
        description=(
            "新建或整体覆盖一个文件，给出完整内容；只生成 diff，需用户确认后才写入。"
            "新建文件时首选它。改已有文件时优先用 replace_text（改一处）或"
            " replace_lines（改一段）——整文件重写容易丢掉原有内容里的细节"
            "（实测：重写时 CSS 单位 100vh→100%、400px→400 全丢了），"
            "而且内容要经过 JSON 转义，出错面更大。"
            "整份重写让行数减少一半以上时必须说明原因（rewrite_reason），"
            "否则会被拒绝（合并、去重、搬运这类合法大改同样走这条路）。"
            "带上 rewrite_reason 之后正文会被收下并返回一个令牌，"
            "外加一份要逐项交代的「消失清单」：清单还没交代完也不要紧，"
            "用 confirm_rewrite 补项 id 即可，**正文不用再发一遍**"
            "（在这一次调用里就把 acked_disappeared 给全的话，一步落盘）"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                REWRITE_REASON_PARAM: {
                    "type": "string",
                    "description": "这次大重写的原因（合并/去重/搬运等）",
                },
                ACK_PARAM: {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "逐项确认消失清单（可只写项 id，如 [\"d1\",\"d2\"]）；"
                        "没凑齐也不丢正文，改用 confirm_rewrite 补"
                    ),
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=handler,
        brief="整份写文件",
        group="改",
    )


def delete_file_spec(root: Path, pending: PendingChanges) -> ToolSpec:
    """删除工作区里的一个文件，走待确认清单，可 revert。

    存在的理由是一条实测：merge-html 那条探针里模型把内容合并好了，用户那句
    「以后我就看一个文件」却落不了地——**工具集里没有删除文件的工具**，它只能
    建议用户自己去 `rm pricing.html`（两次输出一字不差）。合并、去重、搬运这类
    任务里，「把那份多余的删掉」和「把内容并过去」是同一件事的两半。

    四道守卫，缺一条都会变成一个坏工具：

    - **只允许工作区内的文件**：路径走 `resolve_within(root, ...)` 而不是
      `resolve_readable`——后者包含 `--allow-read` 授权过的外部目录，那些目录
      本来就只读，能删就等于把「读得到」偷换成「删得掉」。
    - **必须给理由**：没有理由的删除在报告里与「误删」长得一模一样。理由进
      `declared`，跟着这处改动走到用户确认的那一刻，和声明式重写同一套留痕。
    - **不许删目录**：删目录是递归操作，一次调用能毁掉整个工作区，而模型要做的
      是「把这一份多余的文件删掉」。目录一律拒绝，并指着它去看有哪些文件。
    - **目标不存在时给人话**：这里有一个必须分开说的情形——**它自己刚删过**。
      报「不存在」会让模型以为自己没删成，于是原样再来一次，白烧一步。
    """

    def handler(args: dict) -> ToolResult:
        reason = args.get(DELETE_REASON_PARAM)
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            return ToolResult(
                ok=False,
                content=(
                    f"{REFUSAL_MARKER}：{DELETE_TOOL} 必须给出 {DELETE_REASON_PARAM}——"
                    "一句话说明为什么这个文件可以被删掉（合并、去重、搬运到别处了）。"
                    "没有理由的删除无法在报告里与误删区分，所以这次没有执行。"
                ),
            )
        try:
            raw = resolve_within(root, str(args.get("path") or ""))
        except PathOutsideProjectError as exc:
            return ToolResult(ok=False, content=str(exc))

        # 视图口径与 read_file / write_file 一致：待确认的删除也算「已经删了」。
        view = WorkspaceView(root, pending)
        if raw.is_dir():
            listing = ""
            try:
                listing = "、".join(sorted(item.name for item in raw.iterdir())[:20])
            except OSError:  # pragma: no cover - 枚举失败不影响拒绝本身
                listing = ""
            return ToolResult(
                ok=False,
                content=(
                    f"{REFUSAL_MARKER}：{DELETE_TOOL} 只删文件，不删目录："
                    f"{args.get('path')} 是一个目录。"
                    "删目录是递归操作，不在这个工具的能力范围内。"
                    + (f"它下面有：{listing}。" if listing else "")
                    + "要清理里面的文件，请逐个点名删除。"
                ),
            )
        if raw.resolve() in view.deleted_paths():
            # **必须先于「不存在」判**：待确认的删除在磁盘上文件还在、在视图里
            # 已经没了。顺序反了的话，模型第一次删除成功、第二次原样重提会收到
            # 「不存在」，于是它以为自己没删成——正是要避免的那一步浪费。
            return ToolResult(
                ok=True,
                content=(
                    f"{args.get('path')} 已经删除（在待确认清单里，尚未落盘）——"
                    "这一步不需要再做一次，接着做别的事。"
                ),
            )
        if not view.exists(raw):
            # 视图口径：这一轮刚新建、还没落盘的文件也「存在」（它在待确认清单
            # 里）。用磁盘判会把「我刚建的那份要撤掉」误判成路径写错了。
            return ToolResult(
                ok=False,
                content=(
                    f"{DELETED_NOT_FOUND}: {args.get('path')} 不存在。"
                    "先 list_dir 看一下有哪些文件，再点名要删哪一个；"
                    "如果它本来就该不存在，那这件事已经做完了，不必再删一次。"
                ),
            )

        stored = pending.path_of(str(args.get("path")))
        # 删除前的全文从**叠加视图**里取：这一轮刚新建（还没落盘）的文件，
        # 磁盘上根本没有它，读磁盘会拿到空串——那样留档是空的、diff 是空的，
        # 而回滚会把一份「这一轮自己造出来的内容」当成不存在。
        old_text = next(
            (
                item.new_text
                for item in pending.items()
                if item.path == stored and not item.deleted
            ),
            None,
        )
        if old_text is None:
            try:
                old_text = WorkspaceView(root, pending).read_text(raw)
            except (OSError, UnicodeDecodeError):
                return ToolResult(
                    ok=False,
                    content=(
                        f"删不了 {args.get('path')}：它不是可读的 UTF-8 文本文件。"
                        "这个工具只处理文本文件——二进制文件请让用户自己决定怎么处理。"
                    ),
                )

        declared = f"删除文件——{reason}"
        # 只登记，不删：落盘与回滚都走既有的待确认清单（`apply` / `revert`）。
        change = pending.propose_delete(stored, old_text, declared=declared)
        lines = _count_lines(old_text)
        body = (
            f"已登记删除预览（尚未落盘，需用户确认）：{stored}（{lines} 行）。\n"
            f"留档：{declared}\n"
            "确认之后它就不再存在；这次运行仍可 revert 回滚——"
            "删除前的全文已经记进基线，回滚会把它原样写回去。\n\n"
            + change.diff
        )
        return ToolResult(ok=True, content=body)

    return ToolSpec(
        name=DELETE_TOOL,
        description=(
            "删除工作区里的一个文件（只删文件，不删目录）；不直接落盘，"
            "先进待确认清单，用户确认后文件才真的消失，本次运行可用 revert 回滚。"
            "必须给出原因（reason）：为什么这个文件可以被删掉。"
            "合并/去重/搬运这类任务里「把多余的那份删掉」就用它——"
            "不要建议用户自己去 rm，也不要靠把内容改空来假装删除。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的文件（工作区内的相对路径）",
                },
                DELETE_REASON_PARAM: {
                    "type": "string",
                    "description": (
                        "为什么这个文件可以删掉（一句话，例如「内容已并入 index.html」）"
                    ),
                },
            },
            "required": ["path", DELETE_REASON_PARAM],
            "additionalProperties": False,
        },
        handler=handler,
        brief="删除文件（需给理由，可回滚）",
        group="改",
    )


def replace_lines_spec(
    root: Path,
    pending: PendingChanges,
    alternatives: Sequence[str] = ("replace_lines",),
) -> ToolSpec:
    """按行区间替换。比整文件写入精确得多，是改动的首选方式。

    **纯行号是不安全的**（用户点名的第 5 条发现）：一次编辑之后行号就漂了，
    模型拿着旧行号再改一次，区间还落在文件里时**不报错**——它安静地替换错
    的那一段，结果正是「多出来的结束标签 / 少掉的开始标签」那类改坏。
    所以这一版要求带 `expect`：被替换区间**原来的内容**，一字不差。

    为什么选内容锚点而不是「编辑后提示新行数」：提示是软要求，模型可以不看；
    锚点是硬校验，错了当场拒绝并把那一区间现在的真实内容端回去。模型因此
    必然要 read_file（锚点只能从读来的内容里抄），行号漂移这件事就被机械地
    挡住，而不是靠它记得。

    编辑成功后的回执里带上**新行数 / 新指纹 / 这次动了哪一段**：行号已经
    失效是模型自己看不见的事，这三个数就是它下一次动行号前该看的依据。
    """

    def transform(old_text: str, args: dict) -> str:
        if not old_text:
            raise ValueError("文件不存在，无法按行替换；请先确认路径")
        lines = old_text.splitlines()
        start = args["start_line"]
        end = args["end_line"]
        expected = args.get(EXPECT_PARAM)
        if not isinstance(expected, str) or not expected.strip():
            # 没带锚点时不放行。理由写在话术里：出路是「先读再抄」，不是猜行号。
            raise ValueError(
                "replace_lines 必须带 expect：被替换区间**原来的内容**"
                "（从 read_file 的结果里逐字抄，含缩进）。"
                "只给行号的话，上一次编辑之后行号就漂了，"
                "替换会落到错的那一段上而不报错。"
                f"当前 {args['path']} 共 {len(lines)} 行，"
                "先 read_file 看一下要改的那一段。"
            )
        if start < 1 or end < start or end > len(lines):
            raise ValueError(
                f"行号区间非法：文件共 {len(lines)} 行"
                f"（行号可能已经漂了：上一次编辑之后行数会变）。"
                "先 read_file 重新确认行号与内容，再带 expect 改。"
            )
        actual = "\n".join(lines[start - 1 : end])
        if _normalize_span(actual) != _normalize_span(expected):
            raise ValueError(
                f"expect 与实际内容不一致，这次替换没有执行——"
                f"{args['path']} 第 {start} 到 {end} 行现在实际是：\n"
                f"{actual}\n"
                f"（你给的 expect 是：{expected}）\n"
                "行号很可能已经漂了。用 read_file 重新读这一段，"
                "把真实内容抄进 expect，或者改用 replace_text（不用行号）。"
            )
        replacement = args["content"].splitlines()
        merged = lines[: start - 1] + replacement + lines[end:]
        return "\n".join(merged) + ("\n" if old_text.endswith("\n") else "")

    def handler(args: dict) -> ToolResult:
        result = _propose(
            root, pending, args, lambda old: transform(old, args),
            alternatives=alternatives,
        )
        if not result.ok:
            return result
        return ToolResult(
            ok=result.ok,
            content=result.content + _line_edit_receipt(root, pending, args),
        )

    return ToolSpec(
        name="replace_lines",
        description=(
            "替换文件的指定行区间；只生成 diff，需用户确认后才写入。"
            "只用于大文件或只动一两行的情况。"
            "**必须带 expect**：被替换区间原来的内容（从 read_file 逐字抄）。"
            "行号在上一次编辑之后会漂，锚点对不上时这次替换会被拒绝。"
            "这次替换让行数减少一半以上时，同样要给出 rewrite_reason，"
            "否则会被拒绝；正文会被收下并返回令牌，清单用 confirm_rewrite 按项 id 补"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "content": {"type": "string"},
                EXPECT_PARAM: {
                    "type": "string",
                    "description": (
                        "被替换区间原来的内容，必须与文件里逐字一致"
                        "（含缩进与换行）；从 read_file 的结果里抄"
                    ),
                },
                REWRITE_REASON_PARAM: {
                    "type": "string",
                    "description": "这次大改的原因（删重复段/合并/搬运等）",
                },
                ACK_PARAM: {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "逐项确认消失清单（可只写项 id）；没凑齐也不丢正文，"
                        "改用 confirm_rewrite 补"
                    ),
                },
            },
            "required": ["path", "start_line", "end_line", "content", EXPECT_PARAM],
            "additionalProperties": False,
        },
        handler=handler,
        brief="按行号替换",
        group="改",
    )
