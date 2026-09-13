"""HTML → 正文文本。标准库实现，不引依赖。

刻意**不做**完整 readability：那类库（trafilatura / readability-lxml）要拖进
一组解析依赖，而这个项目的原则是"能不加依赖就不加"。这里只做三件确定性的事：

1. 丢掉脚本/样式/框架里那些**永远不会是正文**的东西；
2. 保留标题层级与代码块——它们是"按需取用"的锚点（目录按标题切）；
3. 其余按块级标签断行、压掉多余空行。

它的短板要写清楚：**靠 JS 渲染的页面抽不出正文**（拿到的 HTML 里本来就没有），
遇到那种页面只能拿到标题和空壳——工具会把这件事如实报出来，而不是假装有内容。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# 这些标签里的文字不是正文（脚本、样式、模板、导航性质的重复内容也常在里面）
_SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe"}
_BLOCK = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "ul", "ol", "table", "tr", "blockquote", "figure", "form", "nav", "hr",
}
_HEADING = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False
        self._heading = ""
        self._pre = 0

    # --- 工具 ---

    def _newline(self, count: int = 1) -> None:
        text = "".join(self.parts[-3:])
        if not text.endswith("\n" * count):
            self.parts.append("\n" * count)

    # --- HTMLParser 回调 ---

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag in _HEADING:
            self._newline(2)
            self._heading = _HEADING[tag]
            self.parts.append(f"{self._heading} ")
        elif tag == "pre":
            self._pre += 1
            self._newline(2)
            self.parts.append("```\n")
        elif tag == "li":
            self._newline()
            self.parts.append("- ")
        elif tag == "br":
            self._newline()
        elif tag in _BLOCK:
            self._newline(2)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._newline()
            self.parts.append("```\n")
        elif tag in _HEADING or tag in _BLOCK:
            self._newline(2)

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            if self._pre and self._skip_depth == 0:
                self.parts.append(data)
            return
        if self._in_title and not self.title:
            self.title = data.strip()
            return
        if self._pre:
            # 代码块里保留原样的空白（缩进是代码的一部分）
            self.parts.append(data.rstrip("\n") + "\n")
            return
        self.parts.append(data)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract(html: str, fallback_title: str = "") -> tuple[str, str]:
    """返回（标题, 正文）。标题拿不到时用 `fallback_title`。"""
    reader = _Reader()
    try:
        reader.feed(html)
        reader.close()
    except Exception:  # noqa: BLE001 - 畸形 HTML 不该让抓取失败
        pass
    text = _tidy("".join(reader.parts))
    title = reader.title.strip() or fallback_title
    return title, text
