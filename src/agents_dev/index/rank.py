"""相关性排序与自动预取。

模型不擅长决定「下一步该查什么」，所以预取由检索器自动完成：
用任务描述里的关键词匹配符号名与文件路径，把候选文件的符号表直接
放进上下文。模型要做的判断从「该查什么」降级为「这几个里哪个对」。
"""

import re
import sqlite3
from pathlib import Path

from agents_dev.index.repo_map import render_file_symbols
from agents_dev.llm.tokenizer import TokenCounter

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
) -> str:
    """把最相关文件的**当前内容**也放进去。

    只给符号表的话，模型知道「有什么」，却看不到「怎么写的」。实测本地
    7B 在这套提示词下会一直查符号、整条轨迹里一次 read_file 都不调用，
    12 步烧完也没提出过一次改动；同样的模型、同样的任务，把文件内容直接
    给它，一轮就能改对（回归集 2/10 与 10/10 的差别就在这里）。

    取用范围是有限的：只取排在最前面的少数几个文件，单个文件超过
    CONTENT_FILE_CAP 就跳过——那种文件本来就该用 read_file 按行取。
    """
    files = rank_files(conn, extract_keywords(task_text), limit=max_files)
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
