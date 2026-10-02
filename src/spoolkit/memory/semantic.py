"""`semantic` 一族：行类型与最简读写（设计稿 §4.3 第 1、2 块）。

P1 把「值得记的语义条目」收进新表 `semantic`（`fact` / `preference` /
`decision` / `lesson` / `skill` 五类），旧表 `memory` **冻结不动**（决议 7：
保留为只读的来源）。本模块是 P1 计划 Task 2 的产物，只给「**查得到**」的
最简形态：双字 shingle 分词口径是 T4、`bm25()` 排序是 T5、五维重排是 T6
——本模块**不假装**已经排得对，`search_semantic` 目前按写入顺序返回。

两条纪律与 P0 在 `memory_fts` 上完全一致（设计稿 §4.3 原则二的落地口径 +
计划「口径裁决 1」）：

- **`semantic` 上不加触发器**，检索索引由应用层显式写（`insert_semantic`）：
  触发器拿不到「把中文逐字分开」这个 Python 函数（`store.normalize_for_fts`），
  而那是本仓中文检索的前提；设计稿 §4.3 第 2 块的 `content='semantic'` 与三个
  触发器是一套的，去掉触发器后 external content 就没了同步机制，所以
  `semantic_fts` 建成**非 external content**（与 `memory_fts` 同形）。
- **本模块不换分词口径**：仍然是逐字拆。bigram 是 T4 的事，顺序（先分词口径、
  再 bm25、最后才谈嵌入）本身是 §13.11 的定案。

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
from spoolkit.memory.store import normalize_for_fts, _match_query

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
    """索引三列都过一遍逐字拆：中文才检索得到（`semantic_fts(text,trigger,evidence)`）。"""
    return tuple(normalize_for_fts(value) for value in (text, trigger, evidence))


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


def search_semantic(
    conn: sqlite3.Connection,
    query: str,
    limit: int | None = None,
    overrides: dict | None = None,
) -> list[Semantic]:
    """关键词检索（**最简版：查得到**，排序留给 T4/T5/T6）。

    - 分词口径：仍是逐字拆（`store._match_query`）——bigram 是 T4 的事；
    - 排序：暂时按写入顺序（`ORDER BY id`）。`bm25()` 是 T5、五维重排是 T6，
      本任务**不假装**排得对（§13.11 的顺序不许颠倒：先分词口径、再 bm25）；
    - `limit` 默认取 `limits` 的 `recall_limit`（§6.2 表；可被
      `.agent/limits.json` 覆盖）——这也是本任务对那个旋钮的接线处。
    """
    if not query.strip():
        return []
    if limit is None:
        limit = int(limits.resolve("recall_limit", overrides)[0])
    try:
        rows = conn.execute(
            "SELECT s.* FROM semantic_fts f JOIN semantic s ON s.id = f.rowid"
            " WHERE semantic_fts MATCH ? ORDER BY s.id LIMIT ?",
            (_match_query(query), limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS5 查询语法错误（比如全是标点）：按「查不到」处理，与
        # `search_memories` 同一口径——检索不该把调用方炸掉。
        return []
    return [_to_semantic(row) for row in rows]
