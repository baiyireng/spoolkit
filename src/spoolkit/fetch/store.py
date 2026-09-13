"""抓到的网页存在哪、怎么切块。

目录：`.agent/web/<key>/`，`key = sha1(url)[:16]`——**由 URL 直接算出来**，
所以不需要一张 url→目录 的索引表，任何会话都能拿着 URL 或 key 找到同一份。

    meta.json    标题、URL、抓取时间、总字节/token、每块的标题与大小
    text.txt     抽出来的正文原文（按需重新切块时用）
    000.txt…     切好的块——工具只把**被要的那一块**交出去

切块按字符预算走，但**优先在标题处断开**：标题是天然的语义边界，
按它切出来的块才有资格进目录（"第 3 块：安装"比"第 3 块"有用得多）。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

WEB_DIR = "web"
DEFAULT_CHUNK_CHARS = 2400      # ≈ 600 token：留出余量，别让一块就吃掉预算
CATALOG_LIMIT = 40              # 目录最多列多少块（再多也没人看）


def key_for(url: str) -> str:
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16]


def dir_for(root: Path | str, url_or_key: str) -> Path:
    """URL 或 key 都能定位到同一个目录。"""
    text = url_or_key.strip()
    name = text if _looks_like_key(text) else key_for(text)
    return Path(root) / ".agent" / WEB_DIR / name


def _looks_like_key(text: str) -> bool:
    return len(text) == 16 and all(char in "0123456789abcdef" for char in text)


@dataclass(frozen=True)
class Chunk:
    index: int
    heading: str
    chars: int
    tokens: int


@dataclass
class Entry:
    url: str
    title: str
    key: str
    fetched_at: float
    chars: int
    tokens: int
    chunks: list[Chunk] = field(default_factory=list)
    cached: bool = False

    @property
    def count(self) -> int:
        return len(self.chunks)


def split(text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> list[tuple[str, str]]:
    """切成（标题, 内容）。优先在 `#` 标题处开新块。返回的标题用于目录。"""
    from spoolkit.llm.tokenizer import OfflineTokenCounter

    counter = OfflineTokenCounter()
    chunks: list[tuple[str, str]] = []
    heading = ""
    buffer: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        body = "\n".join(buffer).strip()
        if body:
            chunks.append((heading, body))
        buffer, size = [], 0

    for line in text.splitlines():
        is_heading = line.startswith("#")
        if is_heading:
            if size and size > chunk_chars * 0.4:
                flush()          # 已经攒了不小一块，正好借标题断开
            heading = line.lstrip("# ").strip() or heading
        if size + len(line) + 1 > chunk_chars and buffer:
            flush()
        buffer.append(line)
        size += len(line) + 1
    flush()
    if not chunks:
        chunks = [("", text.strip())]
    del counter  # 切块按字符算；token 由调用方一处统一估算
    return chunks


def save(root: Path | str, url: str, title: str, text: str,
         chunk_chars: int = DEFAULT_CHUNK_CHARS) -> Entry:
    """落盘并返回条目。同一个 URL 重复保存会覆盖（内容变了就该覆盖）。"""
    from spoolkit.llm.tokenizer import OfflineTokenCounter

    counter = OfflineTokenCounter()
    key = key_for(url)
    target = dir_for(root, key)
    target.mkdir(parents=True, exist_ok=True)
    pieces = split(text, chunk_chars)
    chunks: list[Chunk] = []
    for index, (heading, body) in enumerate(pieces):
        (target / f"{index:03d}.txt").write_text(body, encoding="utf-8")
        chunks.append(
            Chunk(
                index=index,
                heading=heading,
                chars=len(body),
                tokens=counter.count(body),
            )
        )
    (target / "text.txt").write_text(text, encoding="utf-8")
    entry = Entry(
        url=url,
        title=title,
        key=key,
        fetched_at=time.time(),
        chars=len(text),
        tokens=counter.count(text),
        chunks=chunks,
    )
    meta = {
        "url": url,
        "title": title,
        "key": key,
        "fetched_at": entry.fetched_at,
        "chars": entry.chars,
        "tokens": entry.tokens,
        "chunks": [chunk.__dict__ for chunk in chunks],
    }
    (target / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return entry


def load(root: Path | str, url_or_key: str) -> Entry | None:
    path = dir_for(root, url_or_key) / "meta.json"
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return Entry(
        url=str(meta.get("url") or ""),
        title=str(meta.get("title") or ""),
        key=str(meta.get("key") or ""),
        fetched_at=float(meta.get("fetched_at") or 0.0),
        chars=int(meta.get("chars") or 0),
        tokens=int(meta.get("tokens") or 0),
        chunks=[Chunk(**item) for item in meta.get("chunks") or []],
        cached=True,
    )


def read_chunk(root: Path | str, url_or_key: str, index: int) -> str | None:
    path = dir_for(root, url_or_key) / f"{index:03d}.txt"
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def catalog(entry: Entry, limit: int = CATALOG_LIMIT) -> str:
    """目录：每块一行（标题 + 估算 token）——这是"按需取用"的入口。"""
    lines = [
        f"标题：{entry.title or '（没有标题）'}",
        f"来源：{entry.url}",
        f"共 {entry.count} 块 / 约 {entry.tokens} token"
        + ("（来自缓存）" if entry.cached else ""),
        "要哪块用 `web_read` 取（一次一块，别整份读）：",
    ]
    for chunk in entry.chunks[:limit]:
        label = chunk.heading or "（无小标题）"
        lines.append(f"  [{chunk.index}] {label} · 约 {chunk.tokens} token")
    if entry.count > limit:
        lines.append(f"  …还有 {entry.count - limit} 块，用 web_read 按序号取")
    return "\n".join(lines)
