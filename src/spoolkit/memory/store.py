"""记忆的 SQLite 存储与关键词检索。

检索用 SQLite 自带的 FTS5，不引入向量库：代码与项目语境里的讨论对象
多是独特标识符，关键词命中率本来就高，先把这个便宜的好处拿满。

中文处理的易错点：FTS5 默认分词器会把一整段连续中文当成一个 token，
「自动归档整理」和查询「归档」对不上。因此写入与查询都要先把
中文字符逐字用空格分开，让默认分词器能把它们切成独立的可检索单元。

两条结构性纪律：

- **结构按 `PRAGMA user_version` 步进升级**（`_MIGRATIONS`）：`CREATE TABLE
  IF NOT EXISTS` 给不了既有表新列，所以 `episode` 加列、`memory_fts` 重建
  都必须挂在版本号上，且每一步幂等、失败可诊断（设计稿 §9.3 第 1、5 步）。
- **检索索引由应用层显式写**（`insert_memory`），不用 FTS5 触发器：
  触发器拿不到「中文逐字拆」这个 Python 函数，而那是本仓中文检索的前提。
  `memory_fts` 索引四列 `text/kind/trigger/source`——`kind`、触发词、来源
  都要能查（设计稿 §2.2 与缺陷 4）。**v3 起的 `semantic` 一族沿同一条口径**
  （`SEMANTIC_SCHEMA`：`semantic` 上也没有触发器，同步在
  `semantic.insert_semantic` 一处做）。
"""

import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# 库结构的起点：改造前的建表脚本，**`memory` 表那一段逐字冻结、不许改**。
# v1 步直接跑它（新库靠它建表，旧库靠 `IF NOT EXISTS` 变成空操作），
# `episode` 的新列与 `memory_fts` 的改造各自挂在 v1 / v2 步上。
MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    text          TEXT NOT NULL,
    trigger       TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    confidence    REAL NOT NULL DEFAULT 0.5,
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    applied_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS episode (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    task       TEXT NOT NULL,
    summary    TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(text);

CREATE TABLE IF NOT EXISTS session (
    id            TEXT PRIMARY KEY,
    project_root  TEXT NOT NULL,
    model         TEXT NOT NULL DEFAULT '',
    context_limit INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_active   REAL NOT NULL
);

-- 展示用的聊天记录。和上下文是两件事：
-- 上下文不加载历史（状态外置），但要能回头看之前聊过什么。
CREATE TABLE IF NOT EXISTS transcript (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind);
"""

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


@dataclass(frozen=True)
class Memory:
    """一条记忆。"""

    id: int
    kind: str
    text: str
    source: str = ""
    confidence: float = 0.5
    trigger: str = ""
    use_count: int = 0


def normalize_for_fts(text: str) -> str:
    """把中文字符逐字分开，让 FTS5 默认分词器能切出可检索的中文词。"""
    return _CJK.sub(lambda match: f" {match.group(0)} ", text)


def _match_query(query: str) -> str:
    """把查询规范化并转成 FTS5 OR 匹配：任一分词命中即可。"""
    tokens = [t for t in normalize_for_fts(query).split() if len(t) >= 1]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return f'"{tokens[0]}"'
    return " OR ".join(tokens)


_EPISODE_TOKEN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+|[^\s\u3400-\u9fff\uf900-\ufaff]+")


def _episode_query_tokens(query: str) -> list[str]:
    """按空白与非空白 CJK 连续串切分 episode 检索词。"""
    return [t for t in _EPISODE_TOKEN.findall(query.strip()) if len(t) >= 1]


# ---- 结构版本与步进升级（设计稿 §9.3 第 1、3、5 步）--------------------

# 设计稿 §4.3 第 3 块：`episode` **只加列**，不改既有列、不动数据。
# 旧行取默认值 —— 没有「验证」这个概念的既有 episode 一律是「未验证」。
_EPISODE_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("scope", "TEXT NOT NULL DEFAULT 'session'"),
    ("scope_id", "TEXT NOT NULL DEFAULT ''"),
    ("verified", "INTEGER NOT NULL DEFAULT 0"),
    ("verify_detail", "TEXT NOT NULL DEFAULT ''"),
    ("archived", "INTEGER NOT NULL DEFAULT 0"),
)

# P0 不写这些列（没有写入者：`verify_detail` 是 P1 的语义、`archived` 是 P2 的），
# 结构先就位、旧代码照读。
_EPISODE_NEW_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_episode_session ON episode(session_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_episode_created ON episode(created_at DESC)",
)

# `memory_fts` 的四列定义。`memory` 是底账，索引能从它重算，所以重建索引安全
# （回滚 = 用旧定义 `fts5(text)` 重建一次并按 `text` 回填）。
_MEMORY_FTS_COLUMNS: tuple[str, ...] = ("text", "kind", "trigger", "source")


class MemorySchemaError(RuntimeError):
    """记忆库结构升级失败。

    消息是给人看的：第几步、哪一步、什么原因、下一步怎么办。原始异常挂在
    `__cause__` 上供排查——使用者不该看到一个裸 traceback 之后不知道怎么办。
    """


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    """加列前先看表结构：同一步骤重复跑不报 `duplicate column name`。"""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """v0 → v1：建基础表，给 `episode` 加 5 列与 2 个索引。

    这一步里 `memory` 表与 `memory_fts` 的定义一个字节都不动（老规矩：
    「`memory.db` 表结构一个字不改」，决议 7）；`memory_fts` 的重建在 v2。
    """
    conn.executescript(MEMORY_SCHEMA)
    for column, ddl in _EPISODE_NEW_COLUMNS:
        _add_column_if_missing(conn, "episode", column, ddl)
    for statement in _EPISODE_NEW_INDEXES:
        conn.execute(statement)


def _fts_values(text: str, kind: str, trigger: str, source: str) -> tuple[str, ...]:
    """四个字段都过一遍逐字拆：中文才检索得到。"""
    return tuple(normalize_for_fts(value) for value in (text, kind, trigger, source))


def _rebuild_memory_fts(conn: sqlite3.Connection) -> None:
    """按四列重建 `memory_fts`，并从 `memory` 全表回填。

    回填必须在 **Python 侧**做：中文要逐字拆（`normalize_for_fts` 是 Python
    函数），SQL 触发器里没有它（设计稿 §4.3 原则二的 P0 落地口径）。
    形态保持**非 external content**：既有删除路径
    （`DELETE FROM memory_fts WHERE rowid=?`）依赖它，别顺手改。
    """
    conn.execute("DROP TABLE IF EXISTS memory_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        + ", ".join(_MEMORY_FTS_COLUMNS)
        + ")"
    )
    rows = conn.execute(
        "SELECT id, text, kind, trigger, source FROM memory"
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO memory_fts(rowid, text, kind, trigger, source)"
            " VALUES (?,?,?,?,?)",
            (row["id"],)
            + _fts_values(row["text"], row["kind"], row["trigger"], row["source"]),
        )


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2：`memory_fts` 从 1 列扩到 4 列并回填（治缺陷 4 的另一半）。"""
    _rebuild_memory_fts(conn)


# ---- v3：`semantic` 一族（设计稿 §4.3 第 1、2、4、5、6、7 块）------------
#
# DDL **逐字**照设计稿 §4.3，只有两处计划里写明的偏差：
#
# ① **`semantic_fts` 不用触发器、也不是 external content**（计划「口径裁决 1」）。
#    理由与 P0 在 `memory_fts` 上绕过的那条**完全相同**：中文检索依赖写入前把
#    中文字逐字分开（`normalize_for_fts` 是 Python 函数，**SQL 触发器里没有它**），
#    而设计稿 §4.3 第 2 块的 `content='semantic'` 与那三个触发器是**一套的**：
#    去掉触发器之后 external content 就失去了同步机制（写入不会自动进索引），
#    所以按 P0 的同一形态建成非 external content，同步责任收在**一处 Python
#    写入口** `semantic.insert_semantic`（形制照 `insert_memory`）。查询侧若需要
#    触发器，先要有「触发器里能用的中文分词」——那正是本仓还没有的东西。
# ② `memory_decision` **一次建就带 `embed_status` 列**（计划 Task 2 要点 /
#    设计稿 §14.4 规矩 6 的出口①：任何一次「实际没用嵌入」都必须能在
#    `memory_decision` / `--events memory` / `spool memory why` 之一看到）。
#    §14.4 规矩 6 列的取值是 `ok` / `off:not_installed` / `off:no_model` /
#    `off:offline` / `off:timeout` / `off:below_threshold`；P1 的形态是
#    `w_embed=0.0` **明确关闭**，所以默认值用计划「口径裁决 2」点名的
#    `off:disabled`（P1 不引嵌入的定案见 §13.11 / §13.13 第 6 条）。
#
# 去重索引 `idx_semantic_dedupe` **必须是部分唯一索引**（设计稿 §4.3:360-361 的
# `WHERE status = 'active'`）：写成普通唯一索引的话，`superseded` / `archived`
# 的旧条目会**永久挡住**同一 `content_hash` 的新条目——而「条目归档后同一内容
# 可以再次写入」正是 §5.3 ⑤ 冲突链要的反面。
SEMANTIC_SCHEMA = """
-- 1) 语义记忆（新表；承载 fact/preference/decision/lesson/skill）
CREATE TABLE IF NOT EXISTS semantic (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,          -- fact|preference|decision|lesson|skill
    scope         TEXT NOT NULL DEFAULT 'workspace',  -- session|workspace|machine
    scope_id      TEXT NOT NULL DEFAULT '',           -- session 级时为会话 id，否则 ''
    text          TEXT NOT NULL,
    trigger       TEXT NOT NULL DEFAULT '',  -- 逗号分隔关键词（lesson/skill 必填）
    steps         TEXT NOT NULL DEFAULT '',  -- JSON 数组，仅 skill
    source        TEXT NOT NULL DEFAULT '',  -- 出处：ep#12 / user / model / import
    evidence      TEXT NOT NULL DEFAULT '',  -- 可核对的依据：file:line / 命令 / 工具输出摘要
    confidence    REAL NOT NULL DEFAULT 0.5,
    importance    REAL NOT NULL DEFAULT 0.5, -- 写入时判定，参与排序
    content_hash  TEXT NOT NULL,           -- 去重指纹（规范化文本的 sha1 前 16 位）
    status        TEXT NOT NULL DEFAULT 'active',  -- active|archived|superseded|needs_review
    superseded_by INTEGER REFERENCES semantic(id),
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    applied_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_semantic_kind    ON semantic(kind, status);
CREATE INDEX IF NOT EXISTS idx_semantic_scope   ON semantic(scope, scope_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_dedupe
    ON semantic(content_hash) WHERE status = 'active';  -- ② 结构性去重

-- 2) 全文索引：三列 text + trigger + evidence。**非 external content +
--    应用层显式写**（见本常量上方的偏差 ①：触发器拿不到中文分词）。
CREATE VIRTUAL TABLE IF NOT EXISTS semantic_fts USING fts5(text, trigger, evidence);

-- 4) 情节 ↔ 语义 的引用（多对多，两条边各带方向）
CREATE TABLE IF NOT EXISTS memory_link (
    id         INTEGER PRIMARY KEY,
    src_kind   TEXT NOT NULL,   -- episode|semantic
    src_id     INTEGER NOT NULL,
    dst_kind   TEXT NOT NULL,
    dst_id     INTEGER NOT NULL,
    relation   TEXT NOT NULL,   -- derived_from|supersedes|duplicate_of|uses_skill
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_link_src ON memory_link(src_kind, src_id);
CREATE INDEX IF NOT EXISTS idx_link_dst ON memory_link(dst_kind, dst_id);

-- 5) 写入幂等账本
CREATE TABLE IF NOT EXISTS memory_write (
    id         INTEGER PRIMARY KEY,
    round_key  TEXT NOT NULL,   -- sha1(session_id|task_id|goal|final|step)
    source     TEXT NOT NULL,   -- distill|tool:remember|user|consolidate|migrate
    written    INTEGER NOT NULL DEFAULT 0,
    skipped    TEXT NOT NULL DEFAULT '',  -- duplicate|denied|redacted|conflict
    detail     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_write_round ON memory_write(round_key, source);

-- 6) 审计：谁、什么时候、改了什么
CREATE TABLE IF NOT EXISTS memory_audit (
    id         INTEGER PRIMARY KEY,
    at         REAL NOT NULL,
    actor      TEXT NOT NULL,   -- cli|web|agent|distill|consolidate|system
    action     TEXT NOT NULL,   -- add|edit|delete|archive|restore|supersede|demote
    target     TEXT NOT NULL,   -- semantic:12 / episode:8 / hot:fact:3
    before     TEXT NOT NULL DEFAULT '',
    after      TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT ''
);

-- 7) 读取可解释性：这一轮为什么取了这几条
CREATE TABLE IF NOT EXISTS memory_decision (
    id          INTEGER PRIMARY KEY,
    at          REAL NOT NULL,
    session_id  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    channel     TEXT NOT NULL,   -- hot|lesson_push|tool:recall|tool:search_history
    candidates  INTEGER NOT NULL,
    chosen      TEXT NOT NULL,   -- JSON：[{ref,score,parts,reason}]
    dropped     TEXT NOT NULL DEFAULT '',  -- JSON：[{ref,score,reason}]
    embed_status TEXT NOT NULL DEFAULT 'off:disabled'
);
"""


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3：建 `semantic` / `semantic_fts` 与四张账本表。

    `memory` 与 `memory_fts` **一个字节都不动**（决议 7）：这一步只新增表与索引，
    全部 `IF NOT EXISTS`，所以同一步骤重跑是空操作（幂等）。失败了不记版本号
    （`_apply_step`），修掉原因后重跑即可。
    """
    conn.executescript(SEMANTIC_SCHEMA)


# 步骤表：版本号必须严格递增，最后一项就是 `SCHEMA_VERSION`。
_MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "建基础表并给 episode 加列加索引", _migrate_to_v1),
    (2, "重建 memory_fts 为四列并回填", _migrate_to_v2),
    (3, "建 semantic 与四张账本表", _migrate_to_v3),
)

SCHEMA_VERSION = _MIGRATIONS[-1][0]


def _apply_step(
    conn: sqlite3.Connection,
    version: int,
    name: str,
    apply: Callable[[sqlite3.Connection], None],
) -> None:
    """跑一步并把版本号记进 `PRAGMA user_version`。

    失败时抛 `MemorySchemaError`（人话 + 原始异常挂在 `__cause__`），并且
    **不记版本号**——那一步是幂等的，修掉原因后重跑即可。
    """
    try:
        apply(conn)
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise MemorySchemaError(
            f"记忆库结构升级失败：第 {version} 步（{name}）。原始错误：{exc}。"
            f"库里的旧表与旧数据没有被删除，修掉上面的原因后重跑即可"
            f"（这一步是幂等的，不会重复加列）。"
        ) from exc


def init_memory_schema(conn: sqlite3.Connection) -> None:
    """建表并把库结构步进到 `SCHEMA_VERSION`。可重复调用。"""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        # 前向兼容：用户装回过旧版本时，库比代码新。宁可什么都不做，也不能把
        # 版本号写回低值——那会让下一次升级对着已经升级过的库再跑一遍。
        print(
            f"记忆库结构版本（{current}）高于本程序支持的 {SCHEMA_VERSION}，"
            "跳过升级：旧代码不看新列，照常可读。"
        )
        return
    for version, name, apply in _MIGRATIONS:
        if version <= current:
            continue
        _apply_step(conn, version, name, apply)


def open_db_readonly(path: Path) -> sqlite3.Connection | None:
    """**只读**打开记忆库；库不存在返回 `None`（不创建、不建目录）。

    为什么不能直接用 `store/db.py:open_db`：那个函数的第一件事是
    `path.parent.mkdir`，接着 `sqlite3.connect(str(path))` —— **看一眼就把库
    动了一遍**（库不存在时创建空库、库在时按可写打开）。迁移预演的命令必须
    "不写文件、不改库"（设计稿 §9.2 第 1 步），所以走 SQLite 的只读 URI：
    `file:<路径>?mode=ro`。库里若已有 `-wal`，只读打开不会生成 `-shm`
    （读不到就按只读回退）；库不存在时 SQLite 直接报"无法打开"，所以先
    `exists()` 判一次，返回 `None` 让调用方如实写报告。
    """
    if not path.exists():
        return None
    # 用 `as_uri()` 而不是手拼：路径里的空格、`#`、`?` 需要百分号编码，
    # 手拼会在这些目录名上悄悄连到另一个路径（或直接连不上）。
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def insert_memory(
    conn: sqlite3.Connection,
    kind: str,
    text: str,
    source: str = "",
    confidence: float = 0.5,
    trigger: str = "",
) -> Memory:
    """写入一条记忆，并同步维护检索索引——**全仓唯一的记忆写入入口**。

    为什么是应用层显式写索引，而不是 FTS5 触发器（设计稿 §4.3 原则二在
    `memory_fts` 上的 P0 落地口径）：触发器只能拿到 `new.text` 的原样值，
    而本仓检索依赖写入前把中文逐字拆开（`normalize_for_fts`）——那是 Python
    函数，SQL 触发器里没有它。P1 做 `semantic_fts` 时会把分词策略统一收口，
    在那之前先把同步责任收到这一处：`add_memory` 与 `record_lesson` 都走它，
    不再有「某个调用点忘了写索引」——缺陷 4（教训不进 FTS）正是这种
    「调用方约定」的下场。
    """
    cursor = conn.execute(
        "INSERT INTO memory"
        "(kind, text, trigger, source, confidence, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (kind, text, trigger, source, confidence, time.time()),
    )
    memory_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO memory_fts(rowid, text, kind, trigger, source)"
        " VALUES (?,?,?,?,?)",
        (memory_id,) + _fts_values(text, kind, trigger, source),
    )
    conn.commit()
    return Memory(
        id=memory_id,
        kind=kind,
        text=text,
        source=source,
        confidence=confidence,
        trigger=trigger,
    )


def add_memory(
    conn: sqlite3.Connection,
    kind: str,
    text: str,
    source: str = "",
    confidence: float = 0.5,
    trigger: str = "",
) -> Memory:
    """写入一条记忆（`insert_memory` 的既有名字，语义完全相同）。"""
    return insert_memory(
        conn,
        kind=kind,
        text=text,
        source=source,
        confidence=confidence,
        trigger=trigger,
    )


def _to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=row["id"],
        kind=row["kind"],
        text=row["text"],
        source=row["source"],
        confidence=row["confidence"],
        trigger=row["trigger"],
        use_count=row["use_count"],
    )


# 公开别名：别的模块不该为了拿这个转换函数去碰私有名字。
to_memory = _to_memory


def list_memories(conn: sqlite3.Connection, kind: str | None = None) -> list[Memory]:
    """按写入顺序列出记忆，可按类型过滤。"""
    if kind is None:
        rows = conn.execute("SELECT * FROM memory ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memory WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
    return [_to_memory(row) for row in rows]


def search_memories(conn: sqlite3.Connection, query: str) -> list[Memory]:
    """关键词检索，按「用过多少次 + 多久没用」重排。

    FTS5 只给出「匹配上了」，给不出「哪条更有用」。这里叠加两个维度：
    用过次数多的更可能有用；很久没被翻出来的逐渐沉底。

    规则简单，但可解释——排序不合预期时，能直接看出是频率还是
    时间把某条顶上来的。
    """
    if not query.strip():
        return []
    try:
        rows = conn.execute(
            "SELECT m.* FROM memory_fts f JOIN memory m ON m.id = f.rowid"
            " WHERE memory_fts MATCH ?",
            (_match_query(query),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_to_memory(row) for row in _rank(rows)]


FREQUENCY_WEIGHT = 0.6
RECENCY_WEIGHT = 0.4


def _rank(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """按使用频率与新鲜度重排。"""
    now = time.time()

    def score(row: sqlite3.Row) -> float:
        last = row["last_used_at"] or row["created_at"]
        age_days = max(0.0, (now - last) / 86400)
        recency = 1.0 / (1.0 + age_days)
        return row["use_count"] * FREQUENCY_WEIGHT + recency * RECENCY_WEIGHT

    return sorted(rows, key=score, reverse=True)


def touch_memory(conn: sqlite3.Connection, memory_id: int) -> None:
    """记录一次使用，为后续按使用频率排序提供依据。"""
    conn.execute(
        "UPDATE memory SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
        (time.time(), memory_id),
    )
    conn.commit()


def add_episode(
    conn: sqlite3.Connection,
    session_id: str,
    task: str,
    summary: str,
    outcome: str,
) -> int:
    """写入一条历史事件。"""
    cursor = conn.execute(
        "INSERT INTO episode(session_id, task, summary, outcome, created_at)"
        " VALUES (?,?,?,?,?)",
        (session_id, task, summary, outcome, time.time()),
    )
    conn.commit()
    return cursor.lastrowid


def search_episodes(
    conn: sqlite3.Connection, query: str, limit: int = 5
) -> list[sqlite3.Row]:
    """按关键词检索历史事件。

    这里用 LIKE 而不是 FTS5：事件表规模小，而且 LIKE 对中文天然是子串匹配，
    不需要像 FTS5 那样先把中文逐字拆开。简单方案在这个规模上更可靠。
    """
    if not query.strip():
        return []
    tokens = _episode_query_tokens(query)
    if not tokens:
        return []
    clauses: list[str] = []
    params: list[str | int] = []
    for token in tokens:
        needle = f"%{token}%"
        clauses.append("(task LIKE ? OR summary LIKE ?)")
        params.extend([needle, needle])
    params.append(limit)
    where = " OR ".join(clauses)
    return conn.execute(
        "SELECT task, summary, outcome, created_at FROM episode"
        f" WHERE {where}"
        " ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()


def record_session(
    conn: sqlite3.Connection,
    session_id: str,
    project_root: str,
    model: str = "",
    context_limit: int = 0,
) -> None:
    """登记会话，并在首次创建时记下它绑定到哪个工作区。

    绑定要显式记录，不能只靠「数据碰巧放在这个目录下」。一旦以后把记忆
    挪到共享存储，路径约定就失效了，而那时才发现没有任何记录能回答
    某个会话属于哪个工作区。
    """
    now = time.time()
    row = conn.execute(
        "SELECT project_root FROM session WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO session"
            "(id, project_root, model, context_limit, created_at, last_active)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, project_root, model, context_limit, now, now),
        )
    else:
        conn.execute(
            "UPDATE session SET model = ?, context_limit = ?, last_active = ?"
            " WHERE id = ?",
            (model, context_limit, now, session_id),
        )
    conn.commit()


def get_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    """读取会话记录。"""
    return conn.execute(
        "SELECT * FROM session WHERE id = ?", (session_id,)
    ).fetchone()


def list_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """列出全部会话，最近活跃的在前。"""
    return conn.execute(
        "SELECT * FROM session ORDER BY last_active DESC"
    ).fetchall()


def check_binding(
    conn: sqlite3.Connection, session_id: str, project_root: str
) -> str | None:
    """会话绑定的工作区与当前工作区不一致时给出警告说明。

    这不是安全问题（记忆本来就按工作区分开存），而是防止混淆：
    同一个会话名在两个工作区里跑，事件会混进同一条 session 记录，
    「上次做到哪」就不可信了。
    """
    row = get_session(conn, session_id)
    if row is None:
        return None
    if Path(row["project_root"]) == Path(project_root):
        return None
    return (
        f"会话 {session_id} 之前绑定在 {row['project_root']}，"
        f"当前工作区是 {project_root}。建议换个会话名，否则事件会混在一起。"
    )
