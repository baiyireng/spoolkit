"""相关性排序与自动预取。

模型不擅长决定「下一步该查什么」，所以预取由检索器自动完成：
用任务描述里的关键词匹配符号名与文件路径，把候选文件的符号表直接
放进上下文。模型要做的判断从「该查什么」降级为「这几个里哪个对」。
"""

import re
import sqlite3

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

