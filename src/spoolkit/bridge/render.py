"""把模型写出来的 Markdown 收成**聊天软件真正渲染得出来的样子**。

实测结论（2026-09-26，用户在手机 QQ 上看我们的机器人回复）：

- QQ 移动端**渲染一部分 Markdown**——标题、列表、代码块、链接都有格式效果；
- 只有 `**加粗**` 这类**强调标记**原样显示成星号；
- 旧版 PC QQ 会提示"不支持该格式的消息"（用户确认这是正常的）。

所以这里有两个层次，**按通道能力选**：

| 模式 | 做什么 | 给谁 |
|---|---|---|
| `fix` | 只去掉客户端**不渲染**的强调标记（`**粗**`、`*斜*`、`~~删~~`），标题/列表/围栏/链接/表格原样留着 | 手机 QQ 这类"渲染一部分"的客户端 |
| `plain` | 折成纯文本：标记全去、表格逐行、代码围栏加边界、长段落折行 | 什么都不渲染的通道（企业微信、不带 parse_mode 的 Telegram） |
| `markdown` | 一个字不动 | 真渲染 Markdown 的通道 |

**教训**：早先按"QQ 完全不支持 Markdown"处理，把标题、列表、链接全折成纯文本——
那是把**已经在生效的排版一起删了**。能力位要按实测填，不能按印象填。

一条贯穿两个模式的硬约束：**只动标记，不改内容**。代码块里的东西、`sum_to` 这类
标识符、`a * b` 这种乘法、`parts[0](args)` 这种代码，一个字都不许被动。
"""

from __future__ import annotations

import re

# 围栏：``` 或 ~~~ 开头的那一行（可能带语言名）。内容一个字不动。
_FENCE = re.compile(r"^\s*(?:```|~~~)")
# 标题：`### 小标题 ##` → `小标题`（只有 plain 模式才用得上）
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
# 引用：`> 一句` → `一句`
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
# 分隔线：整行只有 --- / *** / ___（三个以上）
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
# 表格的分隔行：|---|:--:|
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
# 列表项：`- 一句` / `* 一句` / `+ 一句`（缩进留着）
_BULLET = re.compile(r"^(\s*)[-*+]\s+(?=\S)")
# 反斜杠转义：`\*` 表示"这里真的就是个星号"，把反斜杠脱掉、让后面的规则别碰它。
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~>|])")
# 一句话（用于折行）：尽量在句末标点处断
_SENTENCE = re.compile(r"[^。！？；!?;]*[。！？；!?;]|[^。！？；!?;]+")

# 「链接」的括号里得**像那么回事**：含 `/` `.` `:` `#` 之一。
#
# 为什么要有这个限制：`[文字](链接)` 和**代码**是撞车的——
# `parts[0](args)`（Python 的"取下标再调用"）长得一模一样，真按链接渲染就变成
# `parts0（args）`，把代码改了。`args` 这种没有标点的目标一律不当链接。
# 中文链接也不受"前面是不是词字符"影响（`详见[文档](url)` 里 `见` 是词字符，
# 但那是正常写法），所以判据放在**括号里**而不是左边。
_TARGET = r"[^)\s]*[/:.#][^)\s]*"

# **强调标记**：客户端不渲染的就是这几类，`fix` 模式只删它们。
#
# 刻意**没有** `__粗__`：模型写粗体几乎都用 `**`，而 `__init__` / `__main__`
# 这类 Python dunder 天天出现——两边都猜不准，宁可留个标记也别把标识符改坏。
_EMPHASIS = (
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S), r"\1"),          # **粗**
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S), r"\1"),              # ~~删~~
    # `*斜*`：内容不能以空白开头结尾，前面也不能还是 `*`（那是粗体），
    # 这样 `a * b * c` 这种乘法不会被误伤。
    (re.compile(r"(?<!\*)\*(?=\S)([^*]+?)(?<=\S)\*(?!\*)", re.S), r"\1"),
    # `_斜_`：两侧不能挨着词字符，否则 `a_b_c` 里的 `_b_` 会被当成斜体拆掉。
    (re.compile(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])", re.S), r"\1"),
)

# plain 模式在强调标记之外**还要**处理的东西：行内代码、链接、图片、引文标记。
_PLAIN_EXTRA = (
    (re.compile(r"`([^`\n]+)`"), r"\1"),                               # `行内代码`
    (re.compile(rf"!\[([^\]]*)\]\(({_TARGET})\)"), r"图片：\1（\2）"),   # 图片
    (re.compile(rf"\[([^\]]+)\]\(({_TARGET})\)"), r"\1（\2）"),         # [文字](链接)
    (re.compile(r"\[([^\]]+)\]\[[^\]]*\]"), r"\1"),                    # [文字][引]
)

_INLINE = _EMPHASIS + _PLAIN_EXTRA


def fix_markers(text: str) -> str:
    """**只去掉客户端不渲染的强调标记**，其它一律原样留着（QQ 手机端走这条）。

    标题、列表、围栏、链接、表格都不动——它们在手机 QQ 上本来就有格式效果，
    折掉等于把排版删了。代码块内部一个字不动。
    """
    if not text:
        return text
    out: list[str] = []
    in_fence = False
    for line in str(text).splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)  # 围栏本身留着：客户端认得它
            continue
        if in_fence:
            out.append(line)
            continue
        line = _ESCAPE.sub(r"\1", line)
        for pattern, repl in _EMPHASIS:
            line = pattern.sub(repl, line)
        out.append(line)
    return "\n".join(out)


def to_plain(text: str, *, wrap: int = 0) -> str:
    """Markdown → 纯文本，并**按手机窄屏排版**（什么都不渲染的通道走这条）。

    `wrap` > 0 时按句末标点软折行（不改字，只在标点后加换行）。
    """
    if not text:
        return text
    lines = str(text).splitlines()
    out: list[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if _FENCE.match(line):
            in_fence = not in_fence
            # 围栏行换成看得见的边界：手机上等宽对齐没了，得让人知道代码从哪到哪
            out.append("┌─ " + (line.strip().lstrip("`~").strip() or "代码") if in_fence else "└─")
            index += 1
            continue
        if in_fence:
            out.append(line)  # 代码块内部一个字不动
            index += 1
            continue
        if _RULE.match(line):
            index += 1
            continue  # 分隔线：手机上留个 `---` 反而像没写完
        if _TABLE_SEP.match(line) and "|" in line:
            index += 1
            continue  # 表格的 |---|---| 那行直接去掉
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            # 表格 → **逐行**：竖屏下 `a | b` 会折断，而"键：值"一行一条更好读。
            # 连续几行一起处理；第一行当表头（只用来决定有没有表头）。
            block: list[list[str]] = []
            cursor = index
            while cursor < len(lines) and lines[cursor].lstrip().startswith("|"):
                row = lines[cursor].strip().strip("|")
                if _TABLE_SEP.match(lines[cursor]) and "|" in lines[cursor]:
                    cursor += 1
                    continue
                block.append([cell.strip() for cell in row.split("|")])
                cursor += 1
            body = block[1:] if len(block) > 1 else block
            for row in body:
                if not any(row):
                    continue
                head = row[0]
                tail = "、".join(cell for cell in row[1:] if cell)
                item = f"· {head}：{tail}" if tail else f"· {head}"
                out.extend(_wrap(item, wrap))
            index = cursor
            continue
        line = _HEADING.sub(r"\1", line)
        line = _QUOTE.sub(r"\1", line)
        line = _BULLET.sub(r"\1· ", line)  # `- ` → `· `：手机上不像减号
        line = _ESCAPE.sub(r"\1", line)
        for pattern, repl in _INLINE:
            line = pattern.sub(repl, line)
        out.extend(_wrap(line, wrap) if line.strip() else [""])
        index += 1
    return _collapse_blank("\n".join(out))


def _wrap(line: str, limit: int) -> list[str]:
    """按句末标点软折行。`limit <= 0` 表示不折。"""
    if limit <= 0 or len(line) <= limit:
        return [line]
    pieces = _SENTENCE.findall(line) or [line]
    out: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > limit:
            out.append(current)
            current = piece
        else:
            current += piece
        while len(current) > limit:
            cut = _soft_cut(current, limit)
            out.append(current[:cut].rstrip())
            current = current[cut:].lstrip()
    if current:
        out.append(current)
    return out


def _soft_cut(text: str, limit: int) -> int:
    """在标点或空格处断，尽量别把词切开；实在没有就在 limit 处硬断。"""
    window = text[:limit]
    for mark in ("，", ",", "、", "：", ":", " "):
        spot = window.rfind(mark)
        if spot >= limit // 2:
            return spot + 1
    return limit


def _collapse_blank(text: str) -> str:
    """连续空行收敛成一个：手机上三个空行就是半屏。"""
    return re.sub(r"\n{3,}", "\n\n", text)
