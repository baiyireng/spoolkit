"""`semantic` 一族：行类型与最简读写（设计稿 §4.3 第 1、2 块）。

P1 把「值得记的语义条目」收进新表 `semantic`（`fact` / `preference` /
`decision` / `lesson` / `skill` 五类），旧表 `memory` **冻结不动**（决议 7：
保留为只读的来源）。本模块是 P1 计划 Task 2 的产物；**T5 起检索口径是**：

- **召回 = 逐字 OR**（`store.normalize_for_fts` + `store._match_query`）。
  T4 一度把它换成双字 shingle，T4 的实测判它是负结果（`清理旧文件` 那种跨词
  边界的查询召回从 5 条掉到 1 条），T5 按裁决**回退**；原因与原始读数写在
  `store.bigram_tokens` 上方的注释里，复核用 `scripts/memory_relevance.py`。
- **排序 = SQLite 内置 `bm25()`**（§14.8 第②步）：`ORDER BY bm25(semantic_fts)`，
  原值**越小越相关**（实测依据见 `store.bm25_score`）。每行把原始值一并带出来
  （`Semantic.bm25_raw`），因为 §6.3 要求 `why` 把**原值与归一化分量两个都打**。
  五维可解释重排是 T6，本模块**不假装**已经排得对——`bm25` 只是"相关"那一维。

三条纪律与 P0 在 `memory_fts` 上完全一致（设计稿 §4.3 原则二的落地口径 +
计划「口径裁决 1」）：

- **`semantic` 上不加触发器**，检索索引由应用层显式写（`insert_semantic`）：
  触发器拿不到「中文逐字拆」这个 Python 函数（`store.normalize_for_fts`），
  而那是本仓中文检索的前提；设计稿 §4.3 第 2 块的 `content='semantic'` 与三个
  触发器是一套的，去掉触发器后 external content 就没了同步机制，所以
  `semantic_fts` 建成**非 external content**（与 `memory_fts` 同形）。
- **分词只动检索侧**：`semantic.text`（用户可见的原文）**一个字节都不改**，
  逐字拆只进 `semantic_fts` 的索引文本与查询串。
- **单字查询有回退**（`归` → `归档旧的任务记录`）：见
  `_semantic_single_char_fallback`。T5 换回逐字之后逐字 OR 自己就能命中 `归`，
  这条回退退化成安全网，但它是被用例钉住的回归锚，**不删**。

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
    _match_query,
    normalize_for_fts,
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
    """一条语义记忆（`semantic` 表的一行）。

    `bm25_raw` **不是表里的列**：它是检索时 `bm25(semantic_fts)` 的原值
    （越小越相关，见 `store.bm25_score`），只在 `search_semantic` 的返回值上
    有值；`get_semantic` / `list_semantic` / 单字回退路径给 `None`。
    """

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
    bm25_raw: float | None = None


def _semantic_fts_values(text: str, trigger: str, evidence: str) -> tuple[str, ...]:
    """索引三列都过一遍**逐字拆**（T5 起召回口径；与 `memory_fts` 同一份）。

    `semantic_fts(text,trigger,evidence)` 三列同一口径：写入与查询都走
    `store.normalize_for_fts` / `store._match_query`，所以不会出现「索引按逐字、
    查询按双字」这种自己跟自己错开的形态。T4 的双字索引口径**已按裁决回退**
    （读数见 `store.bigram_tokens` 上方的注释），双字只留作重排/打分信号。
    """
    return tuple(normalize_for_fts(value) for value in (text, trigger, evidence))


def _to_semantic(row: sqlite3.Row, *, bm25_raw: float | None = None) -> Semantic:
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
        bm25_raw=bm25_raw,
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


# 候选排序的 SQL 片段。**T5 的定案口径 = `bm25()`**（实测原值越小越相关，
# 见 `store.bm25_score`）。
#
# 为什么是一个模块级名字而不是写死的字面量：仪器
# （`scripts/memory_relevance.py`）要复现 T4 冻结的**基线锚**——那条读数是
# 「逐字召回 + 写入顺序」下的 `0.3384615384615385 / 0.5961538461538461 /
# 0.7692307692307693`。把排序口径做成可替换的名字，基线臂与定案臂就跑在
# **同一个函数**里，读数的差只能归因到排序，而不是"两份平行实现"。
_RECALL_ORDER_BY = "bm25(semantic_fts)"


def _semantic_single_char_fallback(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[Semantic]:
    """命中为空时的回退路径：对**原文列**做子串匹配（不下沉到 FTS 索引）。

    为什么留着它（计划 T4 验收 2）：T4 的双字 shingle 只索引相邻双字，长度 < 2
    的查询（`归`）切不出双字 token，走 FTS 必然命中 0 条——而 `归` 正是设计稿
    §14.8 点名的「粒度」案例，它必须查得到 `归档旧的任务记录`。**归零是回归**，
    不是「更精确」。T5 把召回换回逐字之后，逐字 OR 自己命中得了 `归`
    （`_match_query("归") == '"归"'`），所以这条回退退化成**安全网**：FTS 语法
    异常、或分词器切法怪到一个 token 都没命中时，还有一条原文子串的路。它被
    `tests/memory/test_bigram.py` 钉着，**删掉会丢掉那条回归锚**。

    口径：`LIKE '%归%'` 打在 `text` / `trigger` / `evidence` 三列的**原文**上
    （`semantic` 的存储文本一个字节都没被切碎），与 `store.search_episodes`
    用 LIKE 做中文子串匹配同一条理由——小规模、天然子串、不依赖分词器。
    只在 **FTS 命中为空**时兜底（见 `search_semantic`），所以它不会掩盖
    "FTS 确实命中 0 条"的真实结果：`LIKE '%<整条查询>%'` 要求整串子串。
    **这一路没有 `bm25()` 值**（没走 FTS），所以返回的行 `bm25_raw is None`。
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
    """关键词检索：**逐字召回 + `bm25()` 排序**（P1 定案口径）。

    - 召回：**逐字 OR**（`store.normalize_for_fts` + `store._match_query`），
      索引侧（`insert_semantic`）与查询侧同一份口径。T4 的双字召回按裁决回退，
      实测依据（`清理旧文件` 逐字 `[1,3,5,9,13]` vs 双字 `[3]`）见
      `store.bigram_tokens` 上方的注释；双字现在只做重排/打分信号
      （`store.bigram_coverage`），**不在本函数的路径上**；
    - 排序：`ORDER BY bm25(semantic_fts)`（`_RECALL_ORDER_BY`）。SQLite 的
      `bm25()` **原值越小越相关**，所以直接**升序**就是「最相关在前」——
      不取反、不加 `DESC`（方向用例见 `tests/memory/test_semantic_search.py`）。
      每一行把原值带在 `Semantic.bm25_raw` 上：§6.3 要求 `why` 把原始值与
      归一化分量**两个都打**，只打分量分不清是"排序错"还是"归一化错"。
      五维可解释重排是 T6——`bm25` 只是"相关"那一维，本函数不假装排得全对；
    - 命中为空 → `_semantic_single_char_fallback` 的原文子串匹配，
      **不静默返回空**（`归` → `归档旧的任务记录`）；这一路的 `bm25_raw` 是
      `None`（没走 FTS，没有 bm25 值可打）；
    - `limit` 默认取 `limits` 的 `recall_limit`（§6.2 表；可被
      `.agent/limits.json` 覆盖）。截断在 SQL 里做，所以**排序决定留下哪几条**
      ——给足 `limit` 就等于"在全部逐字候选上重排"。
    """
    if not query.strip():
        return []
    if limit is None:
        limit = int(limits.resolve("recall_limit", overrides)[0])
    else:
        limit = int(limit)
    match = _match_query(query)
    rows: list[sqlite3.Row] = []
    if match:
        try:
            rows = conn.execute(
                "SELECT s.*, bm25(semantic_fts) AS bm25_raw"
                " FROM semantic_fts f JOIN semantic s ON s.id = f.rowid"
                f" WHERE semantic_fts MATCH ? ORDER BY {_RECALL_ORDER_BY} LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS5 查询语法错误（比如全是标点）：按「查不到」处理，与
            # `search_memories` 同一口径——检索不该把调用方炸掉。
            rows = []
    if not rows:
        return _semantic_single_char_fallback(conn, query, limit)
    return [_to_semantic(row, bm25_raw=row["bm25_raw"]) for row in rows]
