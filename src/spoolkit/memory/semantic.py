"""`semantic` 一族：行类型与最简读写（设计稿 §4.3 第 1、2 块）。

P1 把「值得记的语义条目」收进新表 `semantic`（`fact` / `preference` /
`decision` / `lesson` / `skill` 五类），旧表 `memory` **冻结不动**（决议 7：
保留为只读的来源）。本模块是 P1 计划 Task 2 的产物，T4 把检索口径换成**双字
shingle**（§14.8 方案 A）；排序仍是"查得到"的最简形态——`bm25()` 是 T5、
五维重排是 T6，本模块**不假装**已经排得对，`search_semantic` 目前按写入顺序返回。

两条纪律与 P0 在 `memory_fts` 上完全一致（设计稿 §4.3 原则二的落地口径 +
计划「口径裁决 1」）：

- **`semantic` 上不加触发器**，检索索引由应用层显式写（`insert_semantic`）：
  触发器拿不到「把中文切成双字 shingle」这个 Python 函数
  （`store.normalize_for_fts_bigram`），而那是本仓中文检索的前提；设计稿 §4.3
  第 2 块的 `content='semantic'` 与三个触发器是一套的，去掉触发器后 external
  content 就没了同步机制，所以 `semantic_fts` 建成**非 external content**
  （与 `memory_fts` 同形）。
- **分词只动检索侧**：`semantic.text`（用户可见的原文）**一个字节都不改**，
  双字只进 `semantic_fts` 的索引文本与查询串。`memory` / `memory_fts` 侧仍是
  逐字口径（`store.normalize_for_fts`），两套口径并存。
- **单字查询有回退**（`归` → `归档旧的任务记录`）：见
  `_semantic_single_char_fallback`（双字对长度 < 2 的查询切不出 token）。

`SEMANTIC_STATUSES` 比设计稿 §4.3 的枚举多一个取值 `needs_review`：§9.3 第 2
步要求「迁移时 `evidence` 为空 → 标 `needs_review`（**不静默丢弃**）」，而
§4.3 的注释只写了 `active|archived|superseded`。P1 的做法是**加取值而不是加列**
（`status` 是 `TEXT`、没有 `CHECK` 约束），见计划「口径裁决 5」。
"""

import sqlite3
import time
from dataclasses import dataclass

from spoolkit import limits
from spoolkit.memory.gate import content_hash as compute_content_hash
from spoolkit.memory.store import (
    _escape_like,
    normalize_for_fts_bigram,
    semantic_match_query,
)

# `status` 的合法取值。第四个取值是 P1 为迁移补的（见模块 docstring），
# 不是写错的第四个状态：它表示「这条没能给出 evidence，等人复核」。
SEMANTIC_STATUSES: tuple[str, ...] = (
    "active",
    "archived",
    "superseded",
    "needs_review",
)


@dataclass(frozen=True)
class Semantic:
    """一条语义记忆（`semantic` 表的一行）。"""

    id: int
    kind: str
    text: str
    content_hash: str = ""
    scope: str = "workspace"
    scope_id: str = ""
    trigger: str = ""
    steps: str = ""
    source: str = ""
    evidence: str = ""
    confidence: float = 0.5
    importance: float = 0.5
    status: str = "active"
    superseded_by: int | None = None
    use_count: int = 0
    applied_count: int = 0
    success_count: int = 0
    created_at: float = 0.0
    last_used_at: float | None = None


def _semantic_fts_values(text: str, trigger: str, evidence: str) -> tuple[str, ...]:
    """索引三列都过一遍**双字 shingle**（T4 起的口径，§14.8 方案 A）。

    `semantic_fts(text,trigger,evidence)` 三列同一口径：写入与查询都走
    `store.normalize_for_fts_bigram`，所以不会出现「索引按双字、查询按逐字」
    这种自己跟自己错开的形态。
    """
    return tuple(
        normalize_for_fts_bigram(value) for value in (text, trigger, evidence)
    )


def _to_semantic(row: sqlite3.Row) -> Semantic:
    return Semantic(
        id=row["id"],
        kind=row["kind"],
        text=row["text"],
        content_hash=row["content_hash"],
        scope=row["scope"],
        scope_id=row["scope_id"],
        trigger=row["trigger"],
        steps=row["steps"],
        source=row["source"],
        evidence=row["evidence"],
        confidence=row["confidence"],
        importance=row["importance"],
        status=row["status"],
        superseded_by=row["superseded_by"],
        use_count=row["use_count"],
        applied_count=row["applied_count"],
        success_count=row["success_count"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


def insert_semantic(
    conn: sqlite3.Connection,
    kind: str,
    text: str,
    *,
    trigger: str = "",
    evidence: str = "",
    source: str = "",
    scope: str = "workspace",
    scope_id: str = "",
    steps: str = "",
    confidence: float = 0.5,
    importance: float = 0.5,
    content_hash: str | None = None,
    status: str = "active",
    overrides: dict | None = None,
) -> Semantic:
    """写入一条语义记忆，并同步维护检索索引——**`semantic` 的唯一写入口**。

    为什么是应用层显式写索引而不是 FTS5 触发器：见模块 docstring（触发器里
    没有中文分词那个 Python 函数）。这条同步责任只在这里有一份，所以不存在
    「某个调用点忘了写索引」——P0 的缺陷 4（教训不进 FTS）正是那种「调用方
    约定」的下场。

    `content_hash` 默认按 `gate.content_hash` 现算（口径与 `memory` 侧同一份：
    去空白 + 去标点 + 全角转半角 + 小写，位数从 `limits` 取）；调用方也可以
    显式传（迁移要按旧行的原文算）。**唯一部分索引** `idx_semantic_dedupe`
    （`WHERE status='active'`）会在同一个 hash 已有 `active` 行时拒绝：
    那是结构性去重，调用方该捕获 `sqlite3.IntegrityError` 并记进
    `memory_write`（T7 的写入口做这件事）。
    """
    digest = content_hash
    if digest is None:
        digest = compute_content_hash(text, overrides)
    cursor = conn.execute(
        "INSERT INTO semantic"
        "(kind, scope, scope_id, text, trigger, steps, source, evidence,"
        " confidence, importance, content_hash, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            kind,
            scope,
            scope_id,
            text,
            trigger,
            steps,
            source,
            evidence,
            confidence,
            importance,
            digest,
            status,
            time.time(),
        ),
    )
    semantic_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO semantic_fts(rowid, text, trigger, evidence) VALUES (?,?,?,?)",
        (semantic_id,) + _semantic_fts_values(text, trigger, evidence),
    )
    conn.commit()
    return Semantic(
        id=semantic_id,
        kind=kind,
        text=text,
        content_hash=digest,
        scope=scope,
        scope_id=scope_id,
        trigger=trigger,
        steps=steps,
        source=source,
        evidence=evidence,
        confidence=confidence,
        importance=importance,
        status=status,
        created_at=time.time(),
    )


def touch_semantic(conn: sqlite3.Connection, semantic_id: int) -> None:
    """记录一次使用（`use_count`/`last_used_at`）：五维打分里的 `use` 维要它。"""
    conn.execute(
        "UPDATE semantic SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
        (time.time(), semantic_id),
    )
    conn.commit()


def get_semantic(conn: sqlite3.Connection, semantic_id: int) -> Semantic | None:
    """按 id 取一条；没有返回 `None`。"""
    row = conn.execute("SELECT * FROM semantic WHERE id = ?", (semantic_id,)).fetchone()
    return None if row is None else _to_semantic(row)


def list_semantic(
    conn: sqlite3.Connection,
    kind: str | None = None,
    status: str | None = None,
) -> list[Semantic]:
    """按写入顺序列出（可按 kind / status 过滤）。

    注意默认**不过滤** `status`：使用者问「这条去哪了」时要能看见 `archived` /
    `superseded` / `needs_review` 的行（§15.4「旧行不删、两种问法都要能答」的
    同一纪律）。
    """
    clauses: list[str] = []
    params: list[str] = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM semantic{where} ORDER BY id", params
    ).fetchall()
    return [_to_semantic(row) for row in rows]


def _semantic_single_char_fallback(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[Semantic]:
    """单字查询的回退路径：对**原文列**做子串匹配（不下沉到 FTS 索引）。

    为什么需要它（计划 T4 验收 2）：双字 shingle 只索引相邻双字，长度 < 2 的
    查询（`归`）切不出任何双字 token，走 FTS 必然命中 0 条——而 `归` 正是设计稿
    §14.8 点名的「粒度」案例，它必须查得到 `归档旧的任务记录`。**归零是回归**，
    不是「更精确」。

    口径：`LIKE '%归%'` 打在 `text` / `trigger` / `evidence` 三列的**原文**上
    （`semantic` 的存储文本一个字节都没被切碎），与 `store.search_episodes`
    用 LIKE 做中文子串匹配同一条理由——小规模、天然子串、不依赖分词器。
    只在 **FTS 命中为空**时兜底（见 `search_semantic`），所以它不会掩盖
    `清理旧文件` 那种"跨词边界、双字确实命中 0 条"的真实结果：`LIKE '%清理旧文件%'`
    要求整串子串，那种查询的答案仍然是"查不到"。
    """
    needle = f"%{_escape_like(query.strip())}%"
    rows = conn.execute(
        "SELECT * FROM semantic"
        " WHERE text LIKE ? ESCAPE '\\'"
        " OR trigger LIKE ? ESCAPE '\\'"
        " OR evidence LIKE ? ESCAPE '\\'"
        " ORDER BY id LIMIT ?",
        (needle, needle, needle, limit),
    ).fetchall()
    return [_to_semantic(row) for row in rows]


def search_semantic(
    conn: sqlite3.Connection,
    query: str,
    limit: int | None = None,
    overrides: dict | None = None,
) -> list[Semantic]:
    """关键词检索（**双字 shingle 口径**，排序仍是最简形态）。

    - 分词口径：**双字 shingle**（T4 落地，§14.8 方案 A）。索引侧
      （`insert_semantic`）与查询侧都走 `store.bigram_tokens`，同一份口径；
    - 单字查询（长度 < 2，切不出双字）→ **FTS 命中为空时**转
      `_semantic_single_char_fallback` 的原文子串匹配，**不静默返回空**（`归` →
      `归档旧的任务记录`）。多字查询即使双字命中 0 条也**不**走回退：`LIKE '%<整条查询>%'`
      本来也要求整串子串，所以真实结果仍是"查不到"，不会被偷偷改写成逐字 OR；
    - 排序：暂时按写入顺序（`ORDER BY s.id`）。`bm25()` 是 T5、五维重排是 T6，
      本任务**不假装**排得对（§13.11 的顺序不许颠倒：先分词口径、再 bm25）；
    - `limit` 默认取 `limits` 的 `recall_limit`（§6.2 表；可被
      `.agent/limits.json` 覆盖）。
    """
    if not query.strip():
        return []
    if limit is None:
        limit = int(limits.resolve("recall_limit", overrides)[0])
    else:
        limit = int(limit)
    match = semantic_match_query(query)
    rows: list[sqlite3.Row] = []
    if match:
        try:
            rows = conn.execute(
                "SELECT s.* FROM semantic_fts f JOIN semantic s ON s.id = f.rowid"
                " WHERE semantic_fts MATCH ? ORDER BY s.id LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS5 查询语法错误（比如全是标点）：按「查不到」处理，与
            # `search_memories` 同一口径——检索不该把调用方炸掉。
            rows = []
    if not rows:
        return _semantic_single_char_fallback(conn, query, limit)
    return [_to_semantic(row) for row in rows]
