"""写操作与 diff 预览。

写操作默认**不落盘**，只产生 diff。理由很直接：小模型出错率高，如果错误
直接污染真实代码，用户会丧失信任并最终弃用；而「先看 diff 再应用」几乎
零成本，既能在出错时一眼识破，又不打断思路。

所以全部写操作先进入 PendingChanges，由用户确认后才真正落盘。
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from spoolkit.errors import PathOutsideProjectError
from spoolkit.paths import resolve_readable
from spoolkit.tools.types import ToolResult, ToolSpec
from spoolkit.check.constraints import check_source, format_violations

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

# 拒绝整文件重写的判据（两条硬判据原来的数值没变，只是给了名字）。
REWRITE_MIN_OLD_LINES = 30
REWRITE_SHRINK_RATIO = 0.5

# 拒绝话术的开头。循环靠它认出「这次调用是被护栏拦下的，不是执行后失败」，
# 于是这一步不占步数预算（见 agent/loop.py 的 BLOCKED 判定）。
REFUSAL_MARKER = "⚠ 拒绝整文件重写"

# 出路话术的标记行。循环硬拦「原样重试」时把这一行原样端回给模型——
# 复用既有的工具输出通道（trace / --events 的 tool 事件），不新造一套事件。
NEXT_STEP_MARKER = "照做的下一步："

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


def _declaration(args: dict) -> tuple[str, list[str]]:
    """取出模型显式给出的重写声明：(原因, 已确认的消失清单条目)。"""
    reason = args.get(REWRITE_REASON_PARAM)
    reason = reason.strip() if isinstance(reason, str) else ""
    raw = args.get(ACK_PARAM)
    acked = [str(item) for item in raw] if isinstance(raw, list) else []
    return reason, acked


def _declaration_note(
    reason: str, items: Sequence[Disappeared], shrink: bool
) -> str:
    """声明式重写留在待确认清单上的那一行痕迹。"""
    if not reason:
        return ""
    parts = [f"声明重写——{reason}"]
    if shrink:
        parts.append(f"行数减少超过一半（{REWRITE_SHRINK_RATIO:.0%} 线），走声明通道")
    if items:
        parts.append(
            f"已逐项确认消失清单 {len(items)} 项："
            + "、".join(item.token for item in items)
        )
    else:
        parts.append("消失清单为空")
    return "；".join(parts)


def _ack_missing(
    items: Sequence[Disappeared], acked: Sequence[str]
) -> list[Disappeared]:
    """清单里还没被确认的条目。确认口径：条目本身，或它的标识符。"""
    confirmed = {" ".join(entry.split()).casefold() for entry in acked}
    return [
        item
        for item in items
        if item.token.casefold() not in confirmed and item.key.casefold() not in confirmed
    ]


def _remedy_lines(
    stored: str,
    old_text: str,
    missing: Sequence[Disappeared],
    alternatives: Sequence[str],
) -> list[str]:
    """照做的下一步：**能照抄的调用**，而不是一句「改用 replace_lines」。

    只点这个工作区真的注册过的工具——点一把没有的工具，模型照着做只会得到
    「未知工具」，比不给下一步更浪费。
    """
    lines: list[str] = []
    span = duplicate_span(old_text)
    if span is not None and "replace_lines" in alternatives:
        a, b, c, d = span
        lines.append(
            f'replace_lines(path="{stored}", start_line={c}, end_line={d}, content="")'
            f"  # 删掉第 {c}–{d} 行那份重复内容（它与第 {a}–{b} 行逐行相同）"
        )
    ack = ", ".join(f'"{item.token}"' for item in missing)
    lines.append(
        f'write_file(path="{stored}", content=<你要交的完整内容>, '
        f'{REWRITE_REASON_PARAM}="为什么这是一次合法大改（合并/去重/搬运）"'
        + (f", {ACK_PARAM}=[{ack}]" if missing else "")
        + ")"
    )
    return lines


def _rewrite_refusal(
    stored: str,
    old_text: str,
    old_lines: int,
    new_lines: int,
    items: Sequence[Disappeared],
    reason: str,
    acked: Sequence[str],
    *,
    shrink: bool,
    alternatives: Sequence[str] = ("replace_lines",),
) -> str | None:
    """该不该拦这次重写：拦就返回话术，放行返回 None。

    两条规则，顺序不能反：
    - **未声明的大重写**照旧硬拦（护栏不松）；
    - **声明了但清单还有未确认项**也不放行（逐项确认是对「我没丢内容」的举证）。
    """
    missing = _ack_missing(items, acked)
    if not shrink:
        # 行数没砍掉一半以上：只有「声明了却漏确认」这一种情况要拦。
        if reason and missing:
            return _refusal_text(
                stored, old_text, old_lines, new_lines, items, reason, missing,
                shrink=False, alternatives=alternatives,
            )
        return None
    if not reason or missing:
        return _refusal_text(
            stored, old_text, old_lines, new_lines, items, reason, missing,
            shrink=True, alternatives=alternatives,
        )
    return None


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
            f"{len(missing)} 项没有确认："
            + "、".join(item.token for item in missing)
            + "。\n"
        )
    else:
        parts.append(
            "这可能是丢内容，也可能是**合法的大改**——合并、去重、搬运这类任务"
            "行数本来就会大减。\n"
        )
    if items:
        parts.append(
            f"消失清单（旧内容里有、新内容里没有，共 {len(items)} 项）："
            + "、".join(item.token for item in items)
            + "。\n"
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
        keys = "、".join(item.key for item in missing)
        if reason:
            parts.append(
                f"清单必须**逐项确认**才放行：把每一项填进 {ACK_PARAM}"
                f"（逐字照抄，或只写标识符，如 {keys}）；"
                "确认不了就说明这次重写确实丢了内容，不要落盘。\n"
            )
        else:
            parts.append(
                f"清单还没有确认：先带上 {REWRITE_REASON_PARAM} 说明这次大改做了什么，"
                f"再把每一项填进 {ACK_PARAM}（逐字照抄，或只写标识符，如 {keys}）；"
                "确认不了就说明这次重写确实丢了内容，不要落盘。\n"
            )
    else:
        if items:
            parts.append(
                f"要合并／去重：带上 {REWRITE_REASON_PARAM} 说明这次大改做了什么，"
                f"并把上面消失的每一样填进 {ACK_PARAM}，再重提一次。\n"
            )
        else:
            parts.append(
                f"要合并／去重：带上 {REWRITE_REASON_PARAM} 说明这次大改做了什么，"
                "再重提一次——这次没有识别出消失项，声明本身就是全部举证。\n"
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
            "别去找它们：要删掉一半以上内容，只能先声明、再把消失清单逐项确认后"
            "整份交出。\n"
        )
        parts.append(
            "整份重写时输出一旦被预算截断，写出去的会是半份坏文件——"
            "确认不了清单就别落盘，先 read_file 确认要删的到底是哪一段。\n"
        )
    parts.append(
        "不得原样重提同一个调用：失败过的参数原样重提会被硬拦，"
        "也不会因此多得一步预算。\n"
    )
    for line in _remedy_lines(stored, old_text, missing, alternatives):
        parts.append(f"{NEXT_STEP_MARKER}{line}\n")
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
    """
    small = is_small_project(root)
    alternatives = () if small else ("replace_lines",)
    registry.register(write_file_spec(root, pending, alternatives=alternatives))
    if not small:
        registry.register(
            replace_lines_spec(root, pending, alternatives=alternatives)
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

    @property
    def is_new_file(self) -> bool:
        return not self.old_text

    @property
    def diff(self) -> str:
        if self.is_new_file:
            body = "".join(f"+{line}" for line in self.new_text.splitlines(True))
            return f"--- /dev/null\n+++ b/{self.path}\n{body}"
        return make_diff(self.path, self.old_text, self.new_text)


class PendingChanges:
    """本次运行中提出、尚未落盘的修改集合。"""

    def __init__(self, root: Path, extra_roots: Sequence[Path] = ()) -> None:
        self._root = root.resolve()
        self._extra_roots = tuple(Path(item).resolve() for item in extra_roots)
        self._changes: dict[str, PendingChange] = {}

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

    def propose(self, path: str, new_text: str, declared: str = "") -> PendingChange:
        """登记一处修改。同一路径重复提出时以最后一次为准。

        `declared` 是声明式重写的留痕（为什么大改、确认了哪些消失项），
        跟着这处改动一起走：用户确认的那一刻看得到理由。
        """
        target = self.disk_path(path)
        old_text = target.read_text(encoding="utf-8") if target.exists() else ""
        change = PendingChange(
            path=path, old_text=old_text, new_text=new_text, declared=declared
        )
        self._changes[path] = change
        return change

    def apply(self) -> list[str]:
        """把全部待授权修改写入磁盘，返回已写入的路径。

        落盘之前先构造基线。没有基线的写操作等于没有退路，
        而「确认错了」是必然会发生的——diff 看得再仔细也挡不住走神。
        """
        written: list[str] = []
        for change in self._changes.values():
            target = self.disk_path(change.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.new_text, encoding="utf-8")
            written.append(change.path)
        self._changes.clear()
        return written

    def baseline(self) -> "Baseline":
        """把当前待写入的修改转成可持久化的基线。"""
        return Baseline(
            entries=tuple(
                BaselineEntry(
                    path=change.path,
                    old_text=None if change.is_new_file else change.old_text,
                )
                for change in self._changes.values()
            )
        )

    def discard(self) -> None:
        self._changes.clear()

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
class BaselineEntry:
    """一个文件的改动前状态。old_text 为 None 表示这个文件是新建的。"""

    path: str
    old_text: str | None


@dataclass(frozen=True)
class Baseline:
    """一次写操作的改动前快照。"""

    entries: tuple[BaselineEntry, ...]

    def to_json(self) -> str:
        return json.dumps(
            {"entries": [{"path": e.path, "old_text": e.old_text} for e in self.entries]},
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Baseline":
        payload = json.loads(text)
        return cls(
            entries=tuple(
                BaselineEntry(path=item["path"], old_text=item.get("old_text"))
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
    """
    touched: list[str] = []
    for entry in baseline.entries:
        target = Path(entry.path) if Path(entry.path).is_absolute() else root / entry.path
        if entry.old_text is None:
            if target.exists():
                target.unlink()
            touched.append(entry.path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry.old_text, encoding="utf-8")
        touched.append(entry.path)
    return touched


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
    old_text = target.read_text(encoding="utf-8") if target.exists() else ""
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
    # 「声明 + 逐项确认消失清单」或者做局部修改。
    reason, acked = _declaration(args)
    old_lines = _count_lines(old_text)
    new_lines = _count_lines(new_text)
    items = disappeared_items(old_text, new_text) if target.exists() else []
    shrink = target.exists() and (
        old_lines > REWRITE_MIN_OLD_LINES
        and new_lines < old_lines * REWRITE_SHRINK_RATIO
    )
    if target.exists():
        refusal = _rewrite_refusal(
            stored, old_text, old_lines, new_lines, items, reason, acked,
            shrink=shrink, alternatives=alternatives,
        )
        if refusal is not None:
            return ToolResult(ok=False, content=refusal)

    change = pending.propose(stored, new_text, declared=_declaration_note(
        reason, items, shrink
    ))
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
                + "、".join(item.token for item in items)
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
            "并把工具列出的消失清单逐项填进 acked_disappeared，"
            "否则这次写入会被拒绝（合并、去重、搬运这类合法大改同样走这条路）"
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
                    "description": "逐项确认消失清单：把清单条目照抄进来（可只写标识符）",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=handler,
        brief="整份写文件",
        group="改",
    )


def replace_lines_spec(
    root: Path,
    pending: PendingChanges,
    alternatives: Sequence[str] = ("replace_lines",),
) -> ToolSpec:
    """按行区间替换。比整文件写入精确得多，是改动的首选方式。"""

    def transform(old_text: str, args: dict) -> str:
        if not old_text:
            raise ValueError("文件不存在，无法按行替换；请先确认路径")
        lines = old_text.splitlines()
        start = args["start_line"]
        end = args["end_line"]
        if start < 1 or end < start or end > len(lines):
            raise ValueError(f"行号区间非法：文件共 {len(lines)} 行")
        replacement = args["content"].splitlines()
        merged = lines[: start - 1] + replacement + lines[end:]
        return "\n".join(merged) + ("\n" if old_text.endswith("\n") else "")

    def handler(args: dict) -> ToolResult:
        return _propose(
            root, pending, args, lambda old: transform(old, args),
            alternatives=alternatives,
        )

    return ToolSpec(
        name="replace_lines",
        description=(
            "替换文件的指定行区间；只生成 diff，需用户确认后才写入。"
            "只用于大文件或只动一两行的情况。"
            "这次替换让行数减少一半以上时，同样要给出 rewrite_reason "
            "并逐项确认消失清单（acked_disappeared），否则会被拒绝"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "content": {"type": "string"},
                REWRITE_REASON_PARAM: {
                    "type": "string",
                    "description": "这次大改的原因（删重复段/合并/搬运等）",
                },
                ACK_PARAM: {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "逐项确认消失清单：把清单条目照抄进来（可只写标识符）",
                },
            },
            "required": ["path", "start_line", "end_line", "content"],
            "additionalProperties": False,
        },
        handler=handler,
        brief="按行号替换",
        group="改",
    )
