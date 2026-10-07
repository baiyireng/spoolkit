"""观察：文件的判断活在对话外面。

设计稿 §2.1 的落地：短上下文靠「历史不再必要」，不靠把历史压短。
一次读取产生的结构结论（指纹、检查器指出的问题、名字和行号）记在这里。
压缩可以丢掉对话里的工具输出，丢不掉这份观察。主循环每步只注入这些事实。

整份超过内联行数的可检查文件，对话里只留事实卡。全文留在检查器读过的那一次里，
不进主循环，也不进素材卡。不超过上限的行段，正文只出现在当轮工具结果里；
素材卡只留一行指针，避免压缩把正文改写失真。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from spoolkit.index.symbols import extractor_for

#: 这些扩展名已经有按类型分派的检查器（`tools/verify.analyze_text`）。
#: 观察只承接检查器能给出的事实，不另造一套规则。
CHECKABLE_SUFFIXES = frozenset({".html", ".htm", ".js", ".css", ".json", ".toml", ".py"})

#: 明确取过的行段，每段最多留这么多行。再长就是把整份又塞回对话。
SPAN_KEEP_LINES = 40


@dataclass
class Observation:
    """一个对象上已经成立的事实。"""

    path: str
    fingerprint: str
    lines: int
    problems: tuple[str, ...]
    #: 整份读进来时因为太长，正文没有进入对话。
    withheld: bool = False
    spans: list[str] = field(default_factory=list)
    #: 已经用事实卡回答过的超宽行段，`start-end`。窄行段不记在这里：
    #: 正文不在观察里，压缩之后同一段要重新取。
    span_keys: list[str] = field(default_factory=list)
    #: 名字和行号。正文不在对话里时，靠它对比两份文档，而不是再把全文读回来。
    brief: str = ""

    def render(self) -> str:
        head = f"- {self.path}（指纹 {self.fingerprint}，{self.lines} 行）"
        if self.problems:
            body = "\n".join(f"  {line}" for line in self.problems)
        elif self.withheld:
            body = "  文档检查没有结构问题。全文不在对话里；要看某一段用 start_line/end_line。"
        else:
            body = "  文档检查没有结构问题。"
        parts = [f"{head}\n{body}"]
        if self.brief:
            parts.append(self.brief)
        # 行段正文不注入主循环。要看某几行，当轮用不超过上限的区间再取。
        return "\n".join(parts)


class ObservationBook:
    """本次运行的观察。按路径留最新一份。"""

    def __init__(self) -> None:
        self._items: dict[str, Observation] = {}

    def clear(self) -> None:
        self._items.clear()

    def get(self, path: str) -> Observation | None:
        return self._items.get(path)

    def holds_withheld(self, path: str, fingerprint: str) -> bool:
        """整份被留在观察里、指纹仍对得上：再读整份不会带来新事实。"""
        item = self._items.get(path)
        return bool(
            item is not None and item.withheld and item.fingerprint == fingerprint
        )

    def note(
        self,
        *,
        path: str,
        fingerprint: str,
        lines: int,
        problems: tuple[str, ...],
        withheld: bool,
        span: str = "",
        span_key: str = "",
        brief: str = "",
    ) -> Observation:
        previous = self._items.get(path)
        same = previous is not None and previous.fingerprint == fingerprint
        spans = list(previous.spans) if same else []
        keys = list(previous.span_keys) if same else []
        if span:
            spans.append(span)
            spans = spans[-3:]
        if span_key and span_key not in keys:
            keys.append(span_key)
            keys = keys[-3:]
        item = Observation(
            path=path,
            fingerprint=fingerprint,
            lines=lines,
            problems=problems,
            withheld=(previous.withheld or withheld) if same else withheld,
            spans=spans,
            span_keys=keys,
            brief=brief,
        )
        self._items[path] = item
        return item

    def render(self) -> str:
        visible = [
            item
            for item in self._items.values()
            if item.problems or item.withheld or item.spans or item.brief
        ]
        if not visible:
            return ""
        blocks = [item.render() for item in visible]
        return (
            "【观察】判断记在这里，压缩对话不会把它丢掉。"
            "改动要让对应的事实消失；事实还在，就还没修好。\n"
            + "\n".join(blocks)
        )


def suffix_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    if dot < 0:
        return ""
    return name[dot:].lower()


def pointer(
    path: str,
    fingerprint: str,
    lines: int,
    problem: str = "",
    *,
    asked: int | None = None,
    limit: int | None = None,
) -> str:
    """整份不进对话时，工具结果里留下的那一句。首条结构问题写在这里，轨迹看得到。"""
    head = (
        f"已记入观察：{path}（指纹 {fingerprint}，{lines} 行）。"
        "全文不放入对话。"
    )
    if asked is not None and limit is not None:
        head += f"这次要了 {asked} 行，超过内联上限 {limit}。"
    tail = (
        "声明对照在观察里。要看某一段，用 read_file 的 start_line/end_line，"
        "区间不要超过内联行数。"
    )
    if problem:
        return head + "\n" + problem + "\n" + tail
    return head + tail


def clip_span(numbered: str, *, keep: int = SPAN_KEEP_LINES) -> str:
    if keep <= 0:
        return ""
    rows = [line for line in numbered.splitlines() if line.strip()]
    if len(rows) <= keep:
        shown = rows
        tail = ""
    else:
        shown = rows[:keep]
        tail = f"\n  （这一段还有 {len(rows) - keep} 行没有留在观察里）"
    body = "\n".join(f"  {line}" for line in shown)
    return f"  已取行段：\n{body}{tail}"


_SECOND_DOC = re.compile(r":(\d+)：又一个 <html>")
_NAME_CAP = 24


def feature_brief(path: str, text: str, problems: tuple[str, ...]) -> str:
    """按文档切开后的顶层声明：只有名字和行号。

    两份文件拼在一起时，主循环拿不到两份正文。对照表回答「谁多了什么」，
    不用把全文再读回来。单份长文件同样给一份名单，方便按行号去取那一段。
    """
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return ""
    cuts = sorted(
        {
            int(match.group(1))
            for problem in problems
            for match in _SECOND_DOC.finditer(problem)
            if 1 < int(match.group(1)) <= total
        }
    )
    bounds = [1, *cuts, total + 1]
    segments: list[tuple[int, int, list[tuple[str, int]], list[tuple[str, int]]]] = []
    for start, end_exclusive in zip(bounds, bounds[1:]):
        if start >= end_exclusive:
            continue
        end = end_exclusive - 1
        chunk = "\n".join(lines[start - 1 : end])
        functions = _functions(path, chunk, start)
        bindings = _bindings(chunk, start, taken={name for name, _line in functions})
        segments.append((start, end, functions, bindings))
    if not segments:
        return ""
    rows = ["  声明对照（只有名字和行号，正文不在对话里）："]
    for start, end, functions, bindings in segments:
        rows.append(
            f"  · 第 {start}–{end} 行：函数 {_fmt(functions)}；绑定 {_fmt(bindings)}"
        )
    if len(segments) >= 2:
        rows.extend(_contrast(segments))
    text_out = "\n".join(rows)
    if len(text_out) > 1200:
        return text_out[:1200] + "\n  …（声明对照还有更多）"
    return text_out


def facts_for_span(
    path: str,
    text: str,
    problems: tuple[str, ...],
    start: int,
    end: int,
) -> str:
    """某一行段里的名字和问题。正文不在这里。"""
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return ""
    start = max(1, min(start, total))
    end = max(start, min(end, total))
    chunk = "\n".join(lines[start - 1 : end])
    functions = _functions(path, chunk, start)
    bindings = _bindings(chunk, start, taken={name for name, _line in functions})
    hit = [problem for problem in problems if _problem_in_span(problem, start, end)]
    rows = [
        f"  第 {start}–{end} 行的事实（正文不在对话里）：",
        f"  · 函数 {_fmt(functions)}；绑定 {_fmt(bindings)}",
    ]
    if hit:
        rows.extend(f"  · {problem}" for problem in hit[:8])
    else:
        rows.append("  · 这一段没有单独的结构问题。")
    rows.append("  要看某一处正文，对准上面的行号，区间不超过内联行数。")
    return "\n".join(rows)


_PROBLEM_LINE = re.compile(r":(\d+)：|第 (\d+) 行")


def _problem_in_span(problem: str, start: int, end: int) -> bool:
    match = _PROBLEM_LINE.search(problem)
    if match is None:
        return False
    number = int(match.group(1) or match.group(2))
    return start <= number <= end


def _functions(path: str, chunk: str, start: int) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    seen: set[str] = set()
    for symbol in extractor_for(path).extract(chunk, path):
        if symbol.kind != "function" or symbol.name in seen:
            continue
        seen.add(symbol.name)
        found.append((symbol.name, start + symbol.start_line - 1))
    return found


def _bindings(
    chunk: str, start: int, *, taken: set[str]
) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    seen: set[str] = set()
    for offset, line in enumerate(chunk.splitlines(), start=start):
        for name in _binding_names(line):
            if name in taken or name in seen:
                continue
            seen.add(name)
            found.append((name, offset))
    return found


def _binding_names(line: str) -> list[str]:
    """只认行首的 const/let/var。缩进的是函数内部，不算这份文件的声明。"""
    if not re.match(r"(?:const|let|var)\b", line):
        return []
    body = re.sub(r"^(?:const|let|var)\s+", "", line)
    body = body.split("//", 1)[0]
    names: list[str] = []
    for part in body.split(","):
        name = part.strip().split("=")[0].strip().rstrip(";").strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            names.append(name)
    return names


def _fmt(pairs: list[tuple[str, int]]) -> str:
    if not pairs:
        return "（无）"
    shown = " ".join(f"{name}@{line}" for name, line in pairs[:_NAME_CAP])
    extra = len(pairs) - _NAME_CAP
    if extra > 0:
        shown += f" …还有 {extra} 个"
    return shown


def _contrast(
    segments: list[tuple[int, int, list[tuple[str, int]], list[tuple[str, int]]]],
) -> list[str]:
    """两份（或更多）之间：只在某一段、以及每段都有的名字。"""
    sets = [
        {name for name, _line in functions + bindings}
        for _start, _end, functions, bindings in segments
    ]
    if all(not one for one in sets):
        return ["  · 各段都没有顶层函数或绑定"]
    shared = set.intersection(*sets) if sets else set()
    rows: list[str] = []
    for (start, end, _functions, _bindings), names in zip(segments, sets):
        only = sorted(names - shared)
        if only:
            shown = " ".join(only[:_NAME_CAP])
            extra = len(only) - _NAME_CAP
            if extra > 0:
                shown += f" …还有 {extra} 个"
            rows.append(f"  · 只在第 {start}–{end} 行：{shown}")
    if shared:
        label = "两边都有" if len(segments) == 2 else "各段都有"
        rows.append(f"  · {label}：" + " ".join(sorted(shared)[:_NAME_CAP]))
    elif len(segments) == 2:
        rows.append("  · 两边没有同名声明")
    return rows
