"""merge-html 的验收判据。

合并后 `index.html` 必须**同时**含有首页与价格页的内容，而且仍然是一份文档
——一个 `<!DOCTYPE>`、一个 `<html>`、一个 `<nav>`、一个 `<footer>`，不是把
两份页面首尾粘起来。

另外两条是「真的能用」而不是「看起来像」：

- `index.html` 里引用的本地文件必须真的存在（这正是 `verify_file` 对 HTML 的
  既有判据：页脚/样式路径写坏了算没做完）；
- 如果 `pricing.html` 还留着，它不能还是原来那份带完整导航和页脚的页面——
  留着就意味着「改个链接还是要改两遍」，那正是这次任务要消掉的东西。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checker, parse_args  # noqa: E402

# 首页原本有的内容（合并后不能丢）
HOME_MARKERS = ["把碎片笔记连成一张网", "双向链接与关系图", "全文检索，离线可用"]
# 价格页原本有的内容（合并后必须出现）
PRICING_MARKERS = ["免费版", "团队版", "¥29", "能离线用吗？", "可以退款吗？"]

TAG_PATTERNS = {
    "<html": r"<html\b",
    "<head": r"<head\b",
    "<body": r"<body\b",
    "<nav": r"<nav\b",
    "<footer": r"<footer\b",
}

REMOTE_PREFIXES = ("http://", "https://", "//", "mailto:", "data:", "tel:", "#")


def count_tag(text, pattern):
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


def local_refs(text):
    """HTML 里引用到的本地路径（远程 URL、锚点、绝对站点路径不算）。"""
    refs = []
    for match in re.finditer(r'(?:href|src)\s*=\s*"([^"]*)"', text, flags=re.IGNORECASE):
        raw = match.group(1).strip()
        if not raw or raw.startswith(REMOTE_PREFIXES):
            continue
        if raw.startswith("/"):
            # 站点根路径在本地文件系统里对不上，和 verify_file 的处理一致：跳过。
            continue
        refs.append(raw.split("#", 1)[0].split("?", 1)[0])
    return [item for item in refs if item]


def main():
    args = parse_args(__doc__)
    root = Path(args.root).resolve()
    checker = Checker("merge-html")

    index = root / "index.html"
    if not checker.require(index.is_file(), "index.html 存在"):
        return checker.finish()

    text = index.read_text(encoding="utf-8", errors="replace")
    pricing = root / "pricing.html"

    for marker in PRICING_MARKERS:
        checker.require(marker in text, f"index.html 里含价格页的内容：{marker!r}")
    for marker in HOME_MARKERS:
        checker.require(marker in text, f"index.html 里首页的内容没丢：{marker!r}")

    checker.require(
        count_tag(text, r"<!doctype") == 1,
        "只有一个 <!DOCTYPE>（不是把两份文档粘起来）",
    )
    for label, pattern in TAG_PATTERNS.items():
        count = count_tag(text, pattern)
        checker.require(count == 1, f"{label}> 恰好一个（实际 {count}）")

    missing = []
    for ref in local_refs(text):
        target = (index.parent / ref).resolve()
        if not target.exists():
            missing.append(ref)
    checker.require(
        not missing,
        f"index.html 引用的本地文件都存在（缺：{missing}）",
    )

    if pricing.is_file():
        stub = pricing.read_text(encoding="utf-8", errors="replace")
        # 留着可以，但不能再是那份带完整导航/页脚的重复页面。
        checker.require(
            count_tag(stub, TAG_PATTERNS["<nav"]) == 0,
            "pricing.html 若保留，就不能还有一整份导航（否则改个链接照样要改两遍）",
        )
        checker.note(f"pricing.html 仍存在，{len(stub)} 字符")
    else:
        checker.note("pricing.html 已删除（只剩一个页面，符合用户的原话）")

    return checker.finish()


if __name__ == "__main__":
    raise SystemExit(main())
