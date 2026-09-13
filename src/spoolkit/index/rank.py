"""相关性排序与自动预取。

模型不擅长决定「下一步该查什么」，所以预取由检索器自动完成：
用任务描述里的关键词匹配符号名与文件路径，把候选文件的符号表直接
放进上下文。模型要做的判断从「该查什么」降级为「这几个里哪个对」。
"""

import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from spoolkit.index.indexer import SKIP_DIRS
from spoolkit.index.repo_map import render_file_symbols
from spoolkit.llm.tokenizer import TokenCounter

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")

_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "have",
        "please", "use", "not", "are", "was", "you", "can", "how",
    }
)

EXACT_HIT = 10
PARTIAL_HIT = 4
PATH_HIT = 3
DEFAULT_LIMIT = 5

# 内容预取的额度。给得比符号表大方，但仍然是「取最相关的少数几个」，
# 不是「把项目塞进来」：预算用完就停，大文件依旧留给 read_file 按行取。
CONTENT_BUDGET = 1000
CONTENT_MAX_FILES = 2
CONTENT_FILE_CAP = 600

# 列目录的上限。一个目录一行、一行几十个 token，所以给得松；
# 文件正文沿用内容预取的额度（CONTENT_BUDGET / CONTENT_FILE_CAP），
# 别让一次锚定把上下文吃光。
LISTING_MAX_ITEMS = 30


def extract_keywords(text: str) -> list[str]:
    """提取英文标识符风格的关键词，去停用词、去重、保序。"""
    found: list[str] = []
    for match in _WORD.findall(text):
        lowered = match.lower()
        if lowered in _STOPWORDS or lowered in found:
            continue
        found.append(lowered)
    return found


def rank_files(
    conn: sqlite3.Connection, keywords: list[str], limit: int = DEFAULT_LIMIT
) -> list[str]:
    """按关键词与索引的匹配程度给文件打分排序。"""
    if not keywords:
        return []

    rows = conn.execute(
        "SELECT f.path AS path, s.name AS name"
        " FROM symbol s JOIN file f ON f.id = s.file_id"
    ).fetchall()
    paths = [r["path"] for r in conn.execute("SELECT path FROM file").fetchall()]

    scores: dict[str, int] = {}
    for keyword in keywords:
        for row in rows:
            name = row["name"].lower()
            if name == keyword:
                scores[row["path"]] = scores.get(row["path"], 0) + EXACT_HIT
            elif keyword in name:
                scores[row["path"]] = scores.get(row["path"], 0) + PARTIAL_HIT
        for path in paths:
            if keyword in path.lower():
                scores[path] = scores.get(path, 0) + PATH_HIT

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [path for path, _ in ordered[:limit]]


def prefetch(
    conn: sqlite3.Connection,
    task_text: str,
    counter: TokenCounter,
    max_tokens: int,
) -> str:
    """自动预取：把最相关文件的符号表拼成一段可直接注入的内容。"""
    files = rank_files(conn, extract_keywords(task_text))
    if not files:
        return ""

    blocks: list[str] = []
    remaining = max_tokens
    for path in files:
        block = render_file_symbols(conn, path, counter, remaining)
        if not block:
            continue
        cost = counter.count(block)
        if cost > remaining:
            continue
        blocks.append(block)
        remaining -= cost
    return "\n".join(blocks)


def prefetch_contents(
    conn: sqlite3.Connection,
    root: Path,
    task_text: str,
    counter: TokenCounter,
    max_tokens: int = CONTENT_BUDGET,
    max_files: int = CONTENT_MAX_FILES,
    skip: frozenset[str] | set[str] = frozenset(),
) -> str:
    """把最相关文件的**当前内容**也放进去。

    只给符号表的话，模型知道「有什么」，却看不到「怎么写的」。实测本地
    7B 在这套提示词下会一直查符号、整条轨迹里一次 read_file 都不调用，
    12 步烧完也没提出过一次改动；同样的模型、同样的任务，把文件内容直接
    给它，一轮就能改对（回归集 2/10 与 10/10 的差别就在这里）。

    取用范围是有限的：只取排在最前面的少数几个文件，单个文件超过
    CONTENT_FILE_CAP 就跳过——那种文件本来就该用 read_file 按行取。
    """
    # 已经被锚定预取放进上下文的文件不再重复一遍——同一段正文出现两次
    # 不只是浪费，还会让模型以为看到了两个版本。
    files = [
        path
        for path in rank_files(
            conn, extract_keywords(task_text), limit=max_files + len(skip)
        )
        if path not in skip
    ][:max_files]
    blocks: list[str] = []
    remaining = max_tokens
    for path in files:
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        body = text.rstrip("\n")
        if not body:
            continue
        block = f"{path}\n{body}"
        cost = counter.count(block)
        if cost > min(remaining, CONTENT_FILE_CAP):
            continue
        blocks.append(block)
        remaining -= cost
        if remaining <= 0:
            break
    if not blocks:
        return ""
    return "以下是相关文件当前的内容：\n\n" + "\n\n".join(blocks)


def prefetch_scope(
    root: Path,
    scope: Sequence[str],
    counter: TokenCounter,
    max_tokens: int = CONTENT_BUDGET,
    max_files: int = CONTENT_MAX_FILES,
) -> tuple[str, int, set[str]]:
    """按**步骤自己声明的范围**预取：先列同目录的条目，再给这些文件的正文。

    为什么要有它：关键词预取是在**整段步骤提示词**上做的，而那段文字噪音
    很多——「已完成」列着前几步的目标、「验收标准」写着 pytest、契约里写着
    `不得改动 test_xxx.py`。实测第 4 步（修一个 import）里，两个内容名额
    被两个**测试文件**占满，真正要改的 `main.py` 和它旁边的 `helper.py`
    一个都没进去，于是执行者只能自己 read_file + list_dir 一路找下去，
    一步 6 轮 12802 token（同一批 5 题里 37% 的开销）。

    范围是这一步最可靠的信号：它是计划写下的、也是写入闸门用的那一条。
    所以锚定部分优先占额度，剩下的才轮到关键词排序。

    目录范围只给条目列表、不给正文——列目录就是「我还不确定改哪个文件」
    时最缺的那条信息，正文留给 read_file 按需取。
    """
    blocks: list[str] = []
    covered: set[str] = set()
    remaining = max_tokens
    for raw in scope:
        rel = str(raw).strip().replace("\\", "/").lstrip("./")
        if not rel or any(char in rel for char in "*?["):
            # 通配范围（`**`）宽到没有信息量，交给关键词排序。
            continue
        target = root / rel
        listing = ""
        if target.is_dir():
            listing = _render_listing(root, target)
        elif target.is_file():
            listing = _render_listing(root, target.parent)
        if listing:
            cost = counter.count(listing)
            if cost <= remaining:
                blocks.append(listing)
                remaining -= cost
        if not target.is_file() or rel in covered:
            continue
        try:
            body = target.read_text(encoding="utf-8").rstrip("\n")
        except (OSError, UnicodeDecodeError):
            continue
        if not body:
            continue
        block = f"{rel}\n{body}"
        cost = counter.count(block)
        if len(covered) >= max_files:
            # 名额用完了。说清楚是「没预取」，不是「没有这个文件」。
            blocks.append(f"（{rel} 也在这一步的范围里，没预取，要看就用 read_file）")
            continue
        if cost > min(remaining, CONTENT_FILE_CAP):
            # 放不下就说清楚，别让它以为「上面没有」等于「文件是空的」。
            blocks.append(f"（{rel} 太大，没整份预取，用 read_file 按行看）")
            remaining = max(0, remaining - counter.count(blocks[-1]))
            continue
        blocks.append(block)
        covered.add(rel)
        remaining -= cost
    if not blocks:
        return "", 0, covered
    text = "本次要动的地方：\n\n" + "\n\n".join(blocks)
    return text, max_tokens - remaining, covered


def _render_listing(root: Path, directory: Path) -> str:
    """一行列出一个目录里有什么。找相邻模块（改 import 这类）只靠它。"""
    try:
        entries = sorted(
            item.name + ("/" if item.is_dir() else "")
            for item in directory.iterdir()
            if item.name not in SKIP_DIRS
        )
    except OSError:
        return ""
    if not entries:
        return ""
    shown = entries[:LISTING_MAX_ITEMS]
    more = "" if len(entries) <= LISTING_MAX_ITEMS else f"…（还有 {len(entries) - LISTING_MAX_ITEMS} 项）"
    rel = directory.relative_to(root).as_posix()
    where = "工作区根目录" if rel == "." else f"{rel}/"
    return f"{where} 下的条目：" + "、".join(shown) + more
