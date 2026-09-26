"""把模型写出来的 Markdown 折成**聊天软件能看的纯文本**。

为什么要有这一层：模型天然爱写 Markdown（`**重点**`、``` 代码块 ```、表格、`[链接](url)`），
而这几条通道**都是纯文本发送**——QQ 机器人走 `msg_type: 0`、Telegram 不带
`parse_mode`、企业微信也是 text——于是那些标记会被当成字面量原样显示，
在手机上就是一堆莫名其妙的星号和反引号。

三条设计约束：

1. **不碰模型**：这是**发送侧**的转换，提示词与模型的输出习惯一个字都不改
   （让模型"别写 Markdown"既不可靠、又白占上下文）。
2. **只删标记，不改内容**：代码块里的内容、标识符里的下划线（`sum_to`）、
   乘法里的星号（`a * b`）都要原样留着——见下面每条正则的边界处理。
3. **按通道能力开关**：默认按纯文本渲染；哪天某个通道真支持 Markdown，
   在它的类上写 `markdown = True` 就能关掉（见 `Bridge._render`）。
"""

from __future__ import annotations

import re

# 围栏：``` 或 ~~~ 开头的那一行（可能带语言名）。内容一个字不动。
_FENCE = re.compile(r"^\s*(?:```|~~~)")
# 标题：`### 小标题 ##` → `小标题`（QQ 里没有字号，留着 # 只是噪音；尾部的 `##` 也去掉）
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
# 引用：`> 一句` → `一句`
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
# 分隔线：整行只有 --- / *** / ___（三个以上）
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
# 表格的分隔行：|---|:--:|
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
# 反斜杠转义：`\*` 表示"这里真的就是个星号"，把反斜杠脱掉、让后面的规则别碰它。
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~>|])")

# 「链接」的括号里得**像那么回事**：含 `/` `.` `:` `#` 之一。
#
# 为什么要有这个限制：`[文字](链接)` 和**代码**是撞车的——
# `parts[0](args)`（Python 的"取下标再调用"）长得一模一样，真按链接渲染就变成
# `parts0（args）`，把代码改了。`args` 这种没有标点的目标一律不当链接。
# 中文链接也不受"前面是不是词字符"影响（`详见[文档](url)` 里 `见` 是词字符，
# 但那是正常写法），所以判据放在**括号里**而不是左边。
_TARGET = r"[^)\s]*[/:.#][^)\s]*"

# 行内标记。**顺序重要**：先长后短，避免 `**x**` 被 `*x*` 先吃掉一半。
#
# 刻意**没有** `__粗__` 这条：模型写粗体几乎都用 `**`，而 `__init__` / `__main__`
# 这类 Python dunder 在这个项目的输出里天天出现——`__` 包起来的短词到底是粗体还是
# 标识符，两边都猜不准，那就只认 `**`（宁可留个没删掉的标记，也别把标识符改坏）。
_INLINE = (
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S), r"\1"),          # **粗**
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S), r"\1"),              # ~~删~~
    # `*斜*`：内容不能以空白开头结尾，前面也不能还是 `*`（那是粗体），
    # 这样 `a * b * c` 这种乘法不会被误伤。
    (re.compile(r"(?<!\*)\*(?=\S)([^*]+?)(?<=\S)\*(?!\*)", re.S), r"\1"),
    # `_斜_`：两侧不能挨着词字符，否则 `a_b_c` 里的 `_b_` 会被当成斜体拆掉。
    (re.compile(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])", re.S), r"\1"),
    (re.compile(r"`([^`\n]+)`"), r"\1"),                               # `行内代码`
    (re.compile(rf"!\[([^\]]*)\]\(({_TARGET})\)"), r"图片：\1（\2）"),   # 图片
    (re.compile(rf"\[([^\]]+)\]\(({_TARGET})\)"), r"\1（\2）"),         # [文字](链接)
    (re.compile(r"\[([^\]]+)\]\[[^\]]*\]"), r"\1"),                    # [文字][引]
)


def to_plain(text: str) -> str:
    """Markdown → 纯文本。**只删标记**，不改写内容、不重排段落。"""
    if not text:
        return text
    out: list[str] = []
    in_fence = False
    for line in str(text).splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue  # 围栏那一行本身丢掉，里面的内容留着
        if in_fence:
            out.append(line)  # 代码块内部一个字不动
            continue
        if _RULE.match(line):
            continue  # 分隔线：手机上留个 `---` 反而像没写完
        if _TABLE_SEP.match(line) and "|" in line:
            continue  # 表格的 |---|---| 那行直接去掉
        line = _HEADING.sub(r"\1", line)
        line = _QUOTE.sub(r"\1", line)
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            # 表格行：去掉两侧的竖线，中间的留一个带空格的竖线，读起来还是表格
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            line = "  |  ".join(cells)
        line = _ESCAPE.sub(r"\1", line)  # `\*` → `*`（先脱转义，后面的规则才认得出）
        for pattern, repl in _INLINE:
            line = pattern.sub(repl, line)
        out.append(line)
    return "\n".join(out)
