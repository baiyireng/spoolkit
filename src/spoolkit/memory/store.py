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
  `semantic.insert_semantic` 一处做）。**v4 起的时序六表（`TIMELINE_SCHEMA`：
  `memory_event` / `memory_state` / 两张别名表 / 注册表 / 事件归档表）同样
  不建触发器**，而且金额与数量列一律用 `TEXT`（整数最小单位或 `Decimal`
  序列化串）——浮点累加会让账不平，而账不平不报错、只是数不对
  （设计稿 §15.3 第 4 条）。
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
    """把中文字符逐字分开，让 FTS5 默认分词器能切出可检索的中文词。

    **这是 `memory` / `memory_fts` 侧的逐字口径，也是 T5 起 `semantic_fts`
    召回侧的口径**：T4 一度把 `semantic` 的召回换成双字 shingle
    （`normalize_for_fts_bigram`），T5 的实测把那一步**回退**掉了——双字召回
    抓不到跨词边界的查询，P@5/R@5 都低于逐字（原始读数见 `bigram_tokens`
    上方的实测记录）。本函数一个字不改（决议 7 的只读精神 + 「不许扩大改动面」）。
    """
    return _CJK.sub(lambda match: f" {match.group(0)} ", text)


def _match_query(query: str) -> str:
    """把查询规范化并转成 FTS5 OR 匹配：任一分词命中即可。

    逐字口径，**两处召回共用**：`memory_fts`（P0 起）与 `semantic_fts`
    （T5 起——T4 的双字召回已按裁决回退，见 `bigram_tokens` 上方的实测记录）。
    """
    tokens = [t for t in normalize_for_fts(query).split() if len(t) >= 1]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return f'"{tokens[0]}"'
    return " OR ".join(tokens)


# ---- T4 的双字 shingle 口径：T5 起**只做重排/打分信号**，不在召回路径上 -------
#
# 先把历史写清楚，免得后人再走一遍：
#
# · T4（设计稿 §14.8 方案 A）把 `semantic` 侧的**召回口径**从逐字换成相邻双字
#   shingle，假设「双字比单字更有信息量」。T4 的实测判定它是**负结果**：16 条
#   语料 / 13 条查询上，逐字臂 `P@5=0.3384615384615385`、`R@5=0.5961538461538461`
#   （top1 `0.7692307692307693`），双字臂 `P@5=0.23076923076923078`、
#   `R@5=0.46153846153846156`（top1 反而更好：`1.0`）。原因很直白：双字**抓不到
#   跨词边界的查询**——查 `清理旧文件` 时逐字召回 `[1,3,5,9,13]`（`旧` 命中了
#   `归档旧的任务记录`），双字只召回 `[3]`（`旧文` 这个双字在语料里不存在）。
#   两者互补，但召回的损失比 top1 的收益大。
# · T5 的裁决：**召回阶段回到逐字**（`normalize_for_fts` + `_match_query`），
#   双字只保留下来做**重排/打分信号**（`bigram_coverage`），并在仪器里留一条
#   `bigram` 对照臂（`normalize_for_fts_bigram` + `semantic_match_query`）。
#   所以本节四个函数**不是死代码，但也都不在召回路径上**；谁把它们接回召回，
#   谁就要先拿出「P@5/R@5 不低于逐字」的读数（T4 的仪器在
#   `scripts/memory_relevance.py` 里，跑 `--arm bigram` 就能复核）。
#
# 口径（索引与查询同一份，都走 `normalize_for_fts_bigram`）：
#
# 1. 先把文本切成**连续同类段**：CJK 段（`[\u3400-\u9fff\uf900-\ufaff]+`）与
#    字母数字段（`[A-Za-z0-9]+`）。段与段之间的标点、空白、`/`、`-` 等
#    一律**丢弃**——双字**绝不跨标点/跨空白造**（"档，"、"的任"这类假双字是
#    §14.8 取舍列点名的噪声源，本实现直接不生成）。
# 2. 每段取**相邻双字**：`归档旧的任务记录` → `归档 档旧 旧的 的任 任务 务记 记录`
#    （7 个 token），与设计稿 §14.8 / 计划 T4 验收 1 的字面量逐字一致。
# 3. **单字段保留为单字 token**（`归档，删除` 里的独立段、`版本 1` 里的 `1`）：
#    段长 1 时没有相邻对可造，丢掉它等于让单字条目检索不到。
# 4. 字母数字段按**整段**处理：`uv` / `requirements` / `FTS5` 原样成 token
#    （不按字符切，否则 `uv` 会碎成 `u`、`v` 两个毫无信息的 token）。
#
# **单字查询与双字召回**（计划 T4 验收 2 / §14.8 点名的「粒度」案例）：长度 < 2 的
# 查询（如 `归`）切不出任何双字，`semantic_match_query` 给出的 `"归"` 对应不上
# 任何双字索引行。**这条回退留在 `semantic.search_semantic` 里**：FTS 命中为空时
# 对 `semantic` 的**原文列**做子串匹配（LIKE），不静默返回空。T5 把召回换回逐字
# 之后，逐字 OR 自己就能命中 `归`（`_match_query("归") == '"归"'`），回退从
# 「双字的兜底」退化成一条安全网——但它在单字查询上仍是被钉住的回归锚
# （`tests/memory/test_bigram.py`），**删掉它反而会丢掉那条锚**。
#
# `_BIGRAM_RUN` 的分支顺序是**必须**的：`[^\W_]+` 在 Unicode 默认模式下**也吃掉
# CJK**（"归档" 是 `\w`），所以若用它当"字母数字段"，`用uv管理依赖` 会被整段吞成
# 一个 token、`归档旧的任务记录` 会被当成一个"词"。第一版写成 `(CJK+|[^\W_]+)`
# 就踩了这个坑（混合段被吞掉、纯中文段整段成 token）；写成 `(CJK+|[A-Za-z0-9]+)`
# 并把 CJK 分支放前面，切法才唯一。
_BIGRAM_CJK_CLASS = "[\u3400-\u9fff\uf900-\ufaff]"
_BIGRAM_RUN = re.compile(f"({_BIGRAM_CJK_CLASS}+|[A-Za-z0-9]+)")


def _bigram_runs(text: str) -> list[str]:
    """切出连续同类段：CJK 段 / 字母数字段。标点、空白、`/`、`-` 都是段边界。"""
    return [run for match in _BIGRAM_RUN.finditer(text) for run in match.groups()]


def bigram_tokens(text: str) -> list[str]:
    """把文本切成相邻双字 token（顺序即出现顺序，重复保留）。

    **T5 起它的用途是「重排/打分信号」，不是召回口径**（见本节开头的实测记录）：
    消费它的有 `bigram_coverage`（打分信号）与仪器里的 `bigram` 对照臂
    （`normalize_for_fts_bigram` + `semantic_match_query`）。信号本身**也没有**
    进生产排序——T5 实测它与 `bm25()` 的读数逐位相同（见 `bigram_coverage`）。
    空文本与纯标点文本返回 `[]`。
    """
    tokens: list[str] = []
    for run in _bigram_runs(text):
        if run[0].isascii():
            tokens.append(run)  # 字母数字段整段成 token（口径 4）
            continue
        if len(run) == 1:
            tokens.append(run)  # 单字段保留（见上方口径 3）
            continue
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def normalize_for_fts_bigram(text: str) -> str:
    """双字 shingle 规范化：token 之间用一个空格分开（FTS5 默认分词器照用）。

    它把双字**当索引文本**用的时候，只属于仪器里的 `bigram` 对照臂
    （`run_arm("bigram")`）；生产召回走 `normalize_for_fts`（逐字）。
    """
    return " ".join(bigram_tokens(text))


def semantic_match_query(query: str) -> str:
    """把查询转成 FTS5 MATCH 串（双字口径，OR 连接）。

    与 `_match_query`（逐字，生产口径）成对：本函数是**双字对照臂**的查询侧，
    索引侧是 `normalize_for_fts_bigram`，两者同一份口径，不会「索引按双字、
    查询按逐字」地自己跟自己错开。

    单字查询切不出双字，但**切得出一个单字 token**（口径 3 的查询侧对应物），
    所以这里照常给出 `"归"` 这样的 MATCH 串——它对应不上任何双字索引行，
    于是命中 0 条。**回退不在这里做**：`semantic.search_semantic` 在 FTS 命中
    为空时转 `_semantic_single_char_fallback` 的原文子串匹配，这样"命中 0 条"
    这件事本身（`清理旧文件` 那种跨词边界的查询）**不会被回退掩盖**。
    """
    tokens = bigram_tokens(query)
    if not tokens:
        return ""
    return " OR ".join(f'"{token}"' for token in tokens)


def bigram_coverage(query: str, text: str) -> float:
    """双字覆盖率：查询切出的双字里有多少**不重复地**出现在 `text` 里。

    这是 T5 保留 `bigram_tokens` 的**唯一用途**——重排/打分信号，不是召回：
    召回已经由逐字 OR 做完（逐字负责"抓得到"），双字负责在候选里分辨"更像"
    （`归档旧的任务记录` 对 `归档` 覆盖率 1.0，对 `清理旧文件` 覆盖率 0.0）。
    取**集合**交并而不是多重集：重复出现的同一个双字不该把分数抬起来。

    分母是**查询的双字数**（"查询覆盖了多少"），不是 Jaccard：条目越长越容易
    碰到查询里的某个双字，除以并集会让长条目吃亏（那是 T6 五维打分要权衡的
    事，不在本函数里）。查询切不出双字时返回 `0.0`。

    实测（16 条语料 / 13 条查询）：把它当次序号加在 `bm25()` 后面，三项读数与
    「只用 `bm25()`」**逐位相同**——所以它**没有**进生产排序（见
    `scripts/memory_relevance.py` 的 `char+bm25+bigram` 臂）。
    """
    query_tokens = set(bigram_tokens(query))
    if not query_tokens:
        return 0.0
    return len(query_tokens & set(bigram_tokens(text))) / len(query_tokens)


# ---- T5：`bm25()` 的原值与方向（先实测钉住符号，再拿它排序）------------------
#
# **不假设符号**（计划 T5 风险 ①：猜错会让整个排序反向，而用例可能看不出来）。
# 实测（SQLite 自带 FTS5，逐字索引，`SELECT bm25(semantic_fts)`）：
#
#   · 查 `依赖管理`（MATCH `依 OR 赖 OR 管 OR 理`）：
#     `本项目统一用 uv 管理依赖`（4 个逐字命中）raw = -6.8386；
#     `清理历史数据并备份`（只有 `理` 命中）raw = -1.7271。
#   · 查 `归档` 只剩 `归档旧的任务记录` / `把过期条目存档` 时：
#     raw 分别是 -4.2183 / -1.9047。
#
# 结论：**原值越小（越负）越相关**，`ORDER BY bm25(semantic_fts)`（升序）就是
# 「最相关在前」。方向用例见 `tests/memory/test_semantic_search.py` 的第一条。
def bm25_score(raw: float) -> float:
    """把 `bm25()` 原值翻成「**越大越相关**」的分数（= `-raw`）。

    为什么需要这一步：设计稿 §6.1 写的 `rel = 1 - exp(-bm25)` 里那个 `bm25`
    必须是"越大越相关"的量，而 SQLite 给的原值恰好相反（越负越相关）。所以
    §6.1 的公式配的是**本函数的返回值**，不是原值；T6 的 `bm25_norm` 以本函数
    为输入，T13 的 `why` 则要把**原值与本函数的返回值两个都打**（§6.3），
    否则"排序错"与"归一化错"分不清。

    实测到的两个边界（T5 记录，免得后人把它当 bug 追）：

    - FTS5 的词权重是 `log((N-n+0.5)/(n+0.5))`（`N` = 条目数，`n` = 含该词的条目
      数）：一个词出现在**一半**条目附近时它过零（`N=2, n=1` 时恰好是 0），出现在
      **多数**条目里时为负。所以"每个查询词几乎所有条目都含"时原值会被压到
      `1e-06` 量级（8 条都含查询里四个字时实测 `-4.08e-06`），排序几乎只剩
      **长度归一化**——**短的在前**。负 IDF 与长度惩罚都是 bm25 的定义，不是实现
      错误；
    - 因此"相关"与"更长/更完整"不总是同向：`归档策略：迁移前先备份` 在
      「8 条里 7 条都含 `归`/`档`」的语料上会排在 `策略条目甲` 后面。五维重排
      （T6）要给 `importance` / `use` 这些维度留出纠偏的位置，正是这个原因。
    """
    return -raw


def _escape_like(needle: str) -> str:
    """转义 `LIKE` 的通配符，让查询里的 `%` / `_` 只当普通字符。"""
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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


# ---- v4：状态与时序六表（设计稿 §15.1 / §15.2 / §15.4 / §15.5 / §15.7 + 计划 T3）----
#
# DDL 的字段名**逐条**照 §15.1 的字段表（`memory_event` 14 项 + `status`、
# `memory_state` 14 项 + `id`）、§15.2 的两张别名表（`alias` / `canonical` / `lang` /
# `scope` / `created_at` / `source`），并补上 §15.5 硬约束 3 要求的
# `tz_offset_min`（必填）与 `tz_name`（可空）；归档表与 `memory_event` 同形
# （§13.13 第 5 条：「超限把最旧者搬进归档表」，`id` 保持原值，`derived_from`
# 里的 event id 搬过去仍解析得到）。**本任务不写归档逻辑**（那是 T9）。
#
# 三处按计划 T3 的硬约束写死：
#
# ① **`memory_state` 是双时态行 + 当前行唯一**（原设计写的是
#    `(entity, attribute)` 联合主键，**已就地改掉**，理由见下）。
# ② **`is_derived` 的 `DEFAULT 1`**（§15.1 原话「DDL 默认 1，恒为真」）：它让
#    「这是推导值」这句话在数据里可查，不是装饰。
# ③ **金额/数量列不许 `REAL`**（§15.3 第 4 条）：`memory_event.value` 与
#    `memory_state.value` 都是 `TEXT`（整数分或 `Decimal` 串），比较用精确相等、
#    输出带 `unit`。
#
# ①的历史（写在这里，免得后人再走一遍）：v4 第一版把 `(entity, attribute)` 做成
# 联合主键，**那与 §15.4 直接冲突**——§15.4 要求同键新旧两行并存（旧行
# `valid_to` 非空 + `superseded_by` 指向新行 + 旧行仍可查），而联合主键规定同键
# 只能有一行：§15.4 的「我当时说的」这种问法就只剩 replay 事件链一条路。同时
# §15.1 没给 `id`，`superseded_by` 无从 `REFERENCES`。裁决是**双时态行 + 当前行
# 唯一**，一次把两个问题都消掉：
#
#   · `id INTEGER PRIMARY KEY` —— 给 `superseded_by` 一个可引用的目标；
#   · `UNIQUE(entity, attribute, valid_from)` —— 同键**同生效时刻**只有一行；
#   · 部分唯一索引 `idx_state_current ... WHERE valid_to IS NULL` —— 每个键
#     **至多一行当前值**，同时**允许历史行共存**（与 `semantic` 的
#     `idx_semantic_dedupe` 是同一套路：写成普通唯一索引就会把历史行挡死）；
#   · `superseded_by INTEGER REFERENCES memory_state(id)` —— **真的**自引用外键。
#
# 改 v4 而不是加 v5：v4 **从未发布**（还没提交；真实工作区 `user_version=0`，
# 只有测试副本跑过它），所以原地改对、保持版本阶梯 0→1→2→3→4 干净——
# 「迁移是发布契约」，发布出去的版本才需要补丁版本。
#
# **不建触发器**：理由与 P0 在 `memory_fts`、P1 在 `semantic_fts` 上完全相同——
# 中文检索依赖写入前把中文字逐字分开（`normalize_for_fts` 是 Python 函数，
# **SQL 触发器里没有它**）；而这里的结构性约束（同键同刻唯一、每个键至多一行
# 当前值、别名唯一）由唯一索引在数据库层挡住，本来也不需要触发器。同步责任归
# 写入侧一处（T9 的归约器）。
TIMELINE_SCHEMA = """
-- 1) 事件：原始观察，append-only（§15.1）。「在某个时刻，我观察到某人说了/发生了某事」。
--    · `value` 用 TEXT：金额存整数最小单位、或 Decimal 序列化串，**不建 REAL 列**
--      （§15.3 第 4 条：0.1+0.2 那类误差会变成账不平）。
--    · `at_abs` 可空：相对时间解析失败时落 NULL（§15.5 第 4 行：不猜，
--      原始观察照样入库）；`at_source`/`time_precision` 相应为 unknown。
--    · `tz_offset_min` 必填且**不给默认值**：默认 0 等于悄悄按 UTC 算日界线
--      （§15.5 硬约束 3：日界线按 tz_offset_min 算，不许按 UTC 算）。
--    · `status` 只有 active / retracted：纠错走软撤销，**本表没有 DELETE 路径**（§15.4）。
CREATE TABLE IF NOT EXISTS memory_event (
    id             INTEGER PRIMARY KEY,        -- 引用名 event:<id>
    entity         TEXT NOT NULL,             -- 归一化实体名（§15.2），如 wallet
    attribute      TEXT NOT NULL,             -- 归一化属性名，如 balance
    op             TEXT NOT NULL,             -- delta|set|observe
    value          TEXT NOT NULL,             -- 带符号增量或新值：整数最小单位/Decimal 串
    unit           TEXT NOT NULL DEFAULT '',  -- CNY|次|分钟|''；不同 unit 不许相加
    at_abs         REAL,                      -- 绝对时刻（epoch 秒）；解析失败为 NULL
    tz_offset_min  INTEGER NOT NULL,          -- 必填：日界线按它算
    tz_name        TEXT,                      -- 可空
    at_source      TEXT NOT NULL,             -- explicit|relative|turn|unknown
    time_precision TEXT NOT NULL,             -- exact|minute|day|turn|unknown
    source_ref     TEXT NOT NULL,             -- session:<id>#turn:<n> / transcript:<id> / tool:<name>
    raw_text       TEXT NOT NULL,             -- 原始那句，不许改写
    speaker        TEXT NOT NULL DEFAULT '',  -- user|agent|tool（来源优先级的一半）
    confidence     REAL NOT NULL DEFAULT 0.5, -- 写入时判定；时间 unknown 的默认低
    content_hash   TEXT NOT NULL,             -- 去重指纹（与 §4.3 同一算法）
    status         TEXT NOT NULL DEFAULT 'active'  -- active|retracted
);
CREATE INDEX IF NOT EXISTS idx_event_key_at     ON memory_event(entity, attribute, at_abs);
CREATE INDEX IF NOT EXISTS idx_event_key_status ON memory_event(entity, attribute, status);
CREATE INDEX IF NOT EXISTS idx_event_hash       ON memory_event(content_hash);
CREATE INDEX IF NOT EXISTS idx_event_at         ON memory_event(at_abs);

-- 2) 推导出的当前值：由确定性归约器写，模型不许直接写（§15.1）。
--    **双时态行 + 当前行唯一**（本常量上方的 ①）：新值到来时插一行新行、把旧行
--    的 `valid_to` 置为生效时刻、`superseded_by` 指向新行 id —— **旧行不删不改值**，
--    所以「现在是多少」与「我当时说的」两种问法各自都答得出来（§15.4）。
--    · `valid_from` / `valid_to` 是半开区间 [from, to)，`valid_to IS NULL` = 至今有效。
--    · `UNIQUE(entity, attribute, valid_from)`：同键同生效时刻只许一行。
--    · `idx_state_current` **必须是部分唯一索引**：只约束当前行，历史行照旧共存。
--    · `value` 可空：缺基准时**留空，不给一个猜的数**（§15.6 第 1 条）。
CREATE TABLE IF NOT EXISTS memory_state (
    id            INTEGER PRIMARY KEY,        -- 自增主键：superseded_by 的引用目标（§15.4）
    entity        TEXT NOT NULL,             -- 归一化实体名
    attribute     TEXT NOT NULL,             -- 归一化属性名
    value         TEXT,                      -- 推导值：整数最小单位/Decimal 串；缺基准时空
    unit          TEXT NOT NULL DEFAULT '',  -- 全程只有一个 unit 才允许相加（§15.6 第 2 条）
    derived_from  TEXT NOT NULL DEFAULT '',  -- 证据链：有序 event id 列表（JSON 数组）
    reducer       TEXT NOT NULL DEFAULT 'none',  -- sum_delta|latest|set|max|min|none
    valid_from    REAL NOT NULL,             -- 半开区间起点（NOT NULL：没有「不知道从哪生效」的行）
    valid_to      REAL,                      -- NULL = 至今有效
    superseded_by INTEGER REFERENCES memory_state(id),  -- 取代链：旧 → 新（§15.4）
    confidence    REAL NOT NULL DEFAULT 0.5,
    status        TEXT NOT NULL DEFAULT 'active',  -- active|conflict|needs_review
    reason        TEXT NOT NULL DEFAULT '',  -- missing_base|unit_mismatch|same_instant|no_reducer|time_unknown
    computed_at   REAL NOT NULL,             -- 最近一次重算的时刻
    is_derived    INTEGER NOT NULL DEFAULT 1,  -- DDL 默认 1，恒为真（§15.1）
    UNIQUE (entity, attribute, valid_from)   -- 同键同生效时刻只有一行
);
-- 每个键**至多一行当前值**，历史行共存（部分唯一索引；写成普通唯一索引会把
-- 历史行挡死，取代链就没法走了）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_state_current
    ON memory_state(entity, attribute) WHERE valid_to IS NULL;

-- 3) 键注册表（§15.2 / §15.6 第 5 条）：键怎么定与用哪个算子都落在这里。
--    `type` 可空：没有 type 与没有这一行等价——都走 needs_review + no_reducer，
--    **不许默认套 sum_delta**。`reducer` 可空：不给就按 type 推（§15.3 的对照表）。
CREATE TABLE IF NOT EXISTS entity_attribute (
    entity    TEXT NOT NULL,   -- 归一化实体名（单数名词）
    attribute TEXT NOT NULL,   -- 归一化属性名（名词）
    type      TEXT,            -- monetary|count|duration|text|enum|status|tags|files|best_score
    reducer   TEXT,            -- sum_delta|latest|set|max|min|none
    PRIMARY KEY (entity, attribute)
);

-- 4) 实体别名映射（§15.2）：用户说「余额」「钱」「现金」，系统都指 wallet.balance。
--    `alias` 是**唯一主键** = 「一个 alias 只能指一个 canonical」的机制；
--    想让它指另一个 canonical 只能走 `alias move --force` 且写 audit（T9/T14）。
--    来源只有两条：用户显式、人确认过的候选（P1 不收自动生成的）。
CREATE TABLE IF NOT EXISTS entity_alias (
    alias      TEXT NOT NULL PRIMARY KEY,    -- 别名（唯一；NULL 别名在这里没有意义）
    canonical  TEXT NOT NULL,                -- 归一化实体名
    lang       TEXT NOT NULL DEFAULT '',
    scope      TEXT NOT NULL DEFAULT 'workspace',
    created_at REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT ''      -- user|confirmed
);

-- 5) 属性别名映射（§15.2）：与 entity_alias 同形，canonical 是属性段。
CREATE TABLE IF NOT EXISTS attribute_alias (
    alias      TEXT NOT NULL PRIMARY KEY,
    canonical  TEXT NOT NULL,                -- 归一化属性名
    lang       TEXT NOT NULL DEFAULT '',
    scope      TEXT NOT NULL DEFAULT 'workspace',
    created_at REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT ''
);

-- 6) 事件归档表（§13.13 第 5 条 / §15.7 生命周期）：与 memory_event 同形，
--    `id` 保持原值——`derived_from` 里引用的 event id 搬过来仍解析得到。
--    本任务只建表；「超限把最旧者搬进来、检索默认不含已归档」是 T9。
CREATE TABLE IF NOT EXISTS memory_event_archive (
    id             INTEGER PRIMARY KEY,
    entity         TEXT NOT NULL,
    attribute      TEXT NOT NULL,
    op             TEXT NOT NULL,
    value          TEXT NOT NULL,
    unit           TEXT NOT NULL DEFAULT '',
    at_abs         REAL,
    tz_offset_min  INTEGER NOT NULL,
    tz_name        TEXT,
    at_source      TEXT NOT NULL,
    time_precision TEXT NOT NULL,
    source_ref     TEXT NOT NULL,
    raw_text       TEXT NOT NULL,
    speaker        TEXT NOT NULL DEFAULT '',
    confidence     REAL NOT NULL DEFAULT 0.5,
    content_hash   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
);
"""


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4：建状态与时序六表（`user_version` 3 → 4）。

    六张表：`memory_event`（原始观察，append-only）、`memory_state`（推导值，
    **双时态行 + 当前行唯一**）、`entity_attribute`（键注册表）、
    `entity_alias` / `attribute_alias`（别名映射，`alias` 唯一主键）、
    `memory_event_archive`（事件归档表，本步不写归档逻辑）。

    **`memory_state` 的一处就地修订（v4 从未发布，所以改 v4、不加 v5）**：
    本步的第一版按 §15.1 的字面写 `(entity, attribute)` 联合主键，那与 §15.4
    「新值到来 → 旧行 `valid_to` 非空 + `superseded_by` 指向新行、**旧行仍可查**」
    不能同时成立（同键只允许一行，「我当时说的」那种问法就答不了），而 §15.1
    又没给 `id`，`superseded_by` 无从 `REFERENCES`。裁决后的形态是：
    `id INTEGER PRIMARY KEY` + `UNIQUE(entity, attribute, valid_from)` +
    部分唯一索引 `idx_state_current ... WHERE valid_to IS NULL`（每个键至多一行
    当前值、历史行共存）+ `superseded_by INTEGER REFERENCES memory_state(id)`
    （真的自引用外键）+ `valid_from NOT NULL` / `valid_to` 可空。理由与完整历史
    写在 `TIMELINE_SCHEMA` 上方的注释里。

    与前两步同一条纪律：**只新增**表与索引，全部 `IF NOT EXISTS`，所以同一步骤
    重跑是空操作；`memory` / `memory_fts` / `semantic` / `semantic_fts` / `episode`
    与四张账本表**一个字节都不动**（决议 7）。**不建触发器**（理由见
    `TIMELINE_SCHEMA` 上方的注释）。失败不记版本号（`_apply_step`）。
    """
    conn.executescript(TIMELINE_SCHEMA)


# 步骤表：版本号必须严格递增，最后一项就是 `SCHEMA_VERSION`。
_MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "建基础表并给 episode 加列加索引", _migrate_to_v1),
    (2, "重建 memory_fts 为四列并回填", _migrate_to_v2),
    (3, "建 semantic 与四张账本表", _migrate_to_v3),
    (4, "建状态与时序六表", _migrate_to_v4),
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
