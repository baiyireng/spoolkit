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

**T7 起 `write_memory` 是五道门禁的唯一写入口**（设计稿 §5.3）：
脱敏 → J1–J5（J2「可核对」由 T7 在 `gate.py` 落地）→ 幂等（`memory_write` 的
`idx_write_round`）→ 去重（`content_hash` + `idx_semantic_dedupe`）→ 冲突
（旧行 `superseded`、新行 `active`、旧行不删）。顺序与理由写在
`write_memory` 上方的注释里；写入路径**单写 `semantic`**，不碰旧 `memory` 表。
"""

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from spoolkit import limits
from spoolkit.memory.gate import (
    content_hash as compute_content_hash,
    judge,
    normalize_key,
    sanitize,
)
from spoolkit.memory.store import (
    _escape_like,
    _match_query,
    bigram_tokens,
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


# ---- T7：五道门禁的唯一写入口（设计稿 §5.3 的流程图） -------------------------
#
# 顺序**写死在这一个函数里**：自动蒸馏、`remember` 工具、CLI、迁移、回放都走它
# ——§5.3 的流程图只有在"只有一处"时才成立（每多一个写入口，就要重新记得一遍
# 五道门禁，`record_lesson` 漏写 FTS 就是那种"调用方约定"的下场）：
#
#   ① 脱敏 `gate.sanitize`：本模块唯一一处调用（漏斗是 `_sanitize_fields`）；
#   ② 判据 J1–J5：`gate.judge`，`evidence` 走 T7 新落地的 J2「可核对」；
#   ③ 幂等：`memory_write(round_key, source)` 的唯一索引 `idx_write_round`；
#   ④ 去重：归一化 `content_hash` + `idx_semantic_dedupe`（部分唯一索引）；
#   ⑤ 冲突：同 kind + 主题键交集非空 → 旧行 `superseded`、新行 `active`、旧行不删。
#
# **账本行先抢位（③ 在 ①② 之前落 INSERT）**，这是有意的：被拒、被脱敏的尝试
# 同样要烧掉这一轮，否则「第一次被拒、同一轮重跑又被收下」这条形态就漏过去了
# ——那正是幂等要挡的东西。门禁的**判定顺序**仍是 ①②③④⑤（账本行先占位，
# 最后连同结论一起落账）。抢位就是 INSERT：撞唯一索引即"这一轮已处理过"。
# **不许先查后写**——先查后写有竞态，而且"漏查"这类 bug 不报错（§5.3 ③ 原话）。
#
# **单写语义**（§9.4）：写 `semantic` 的路径一个字节都不碰旧 `memory` 表
# （决议 7：旧表保留只读）。这条由用例 `test_单写semantic` 钉住。

# `semantic.kind` 的合法取值（设计稿 §4.3 第 1 块：fact|preference|decision|
# lesson|skill）。`distill.KINDS` 只有前四类（蒸馏不产 skill），两者**有意不同**：
# 这里是表的枚举，那里是某一条产出路径的枚举。表没有 `CHECK` 约束（§4.3 的
# DDL 逐字如此），所以这道校验只能在写入口做。
SEMANTIC_KINDS: tuple[str, ...] = (
    "fact",
    "preference",
    "decision",
    "lesson",
    "skill",
)

# 冲突判定里"主题键"取 `text` 规范化后的前多少字（设计稿 §5.3 ⑤ 的「前 N 个
# 字符」）。**本仓的 limits 登记表由 T1 冻结、不在 T7 的改动面上**，所以这个 N
# 暂时是模块常量 + `overrides` 的裸键 `conflict_theme_chars`（按工作区仍可覆盖）
# ——正确形态是把它登记进 `limits.KNOBS`，这一条写进 T7 的报告里等回填。
CONFLICT_THEME_CHARS = 8
# 冲突判定的三种结论（§5.3 ⑤：矛盾/补充/无关）与"取代链"的关系键。
CONFLICT_CONTRADICTION = "矛盾"
CONFLICT_VERDICTS: tuple[str, ...] = (CONFLICT_CONTRADICTION, "补充", "无关")
SUPERSEDES_RELATION = "supersedes"
# 判定失败的标记：`status` 是**单列**，没法同时是 `active` 又是 `needs_review`，
# 所以"两条都 active + 标 needs_review"这条纪律落在 `memory_link.relation` 上
# （一对行之间的待复核标记），`status` 两个都保持 `active`。
NEEDS_REVIEW_RELATION = "needs_review"


@dataclass(frozen=True)
class WriteRequest:
    """一条写入请求（五道门禁的输入）。

    `evidence` 的默认值是 `""` = **没给出处**：写入路径上它必过 J2，
    所以"忘了给"与"给不出来"是同一个结果（拒），而不是悄悄放行。
    """

    kind: str
    text: str
    evidence: str = ""
    trigger: str = ""
    scope: str = "workspace"
    scope_id: str = ""
    steps: str = ""
    # 出处：distill|tool:remember|user|consolidate|migrate（设计稿 §4.3 第 5 块）。
    source: str = ""
    confidence: float = 0.5
    importance: float = 0.5
    # 同一轮的幂等键：sha1(session_id|task_id|goal|final|step)（§5.3 ③）。
    # 空 = 调用方没有轮次上下文（人手敲的一条）→ 用一次性键占账本行。
    round_key: str = ""


@dataclass(frozen=True)
class WriteResult:
    """一次写入的结论（**给人看**：`reason` 是可以直接打印的一句中文）。

    `skipped` 的取值与设计稿 §4.3 的账本注释同一套：`""`（正常写入）、
    `duplicate`（判重命中 / 同一轮重跑）、`denied`（判据不过）、`redacted`
    （写入前脱敏命中）、`conflict`（写过冲突判定）。**`skipped` 非空不一定
    表示没写**：`written=True` + `skipped='conflict'` 的意思是"写下了，但这一条
    与既有行之间发生过冲突判定"，详情看 `reason`。
    """

    written: bool
    ref: str = ""
    skipped: str = ""
    reason: str = ""
    # 脱敏命中的**形状名**（设计稿 §5.3 ①）：值本身一个字符都不留。
    shapes: tuple[str, ...] = ()
    semantic_id: int | None = None
    # 判重命中 / 同一轮重跑时指向既有的那一行。
    duplicate_of: int | None = None
    # 被这一条取代（`status='superseded'`）的旧行 id；旧行仍在库里、仍查得到。
    superseded_id: int | None = None


def _ref(item: Semantic) -> str:
    """稳定的引用名 `kind:id`（设计稿 §5.4：引用不受排序、分页、重排影响）。"""
    return f"{item.kind}:{item.id}"


def _actor_for(source: str) -> str:
    """写入来源 → `memory_audit.actor` 的取值（§4.3 第 6 块的枚举）。"""
    if source.startswith("tool:"):
        return "agent"
    if source in ("distill", "consolidate"):
        return source
    if source in ("user", "cli"):
        return "cli"
    return "system"


def _adhoc_round_key() -> str:
    """没有轮次上下文的写入用一次性键占账本行。

    为什么不拿内容当键：那会把"同一条事实隔天再确认一次"误判成"同一轮重跑"。
    幂等（③）管的是**轮次**，判重（④）管的是**内容**，两者不是一回事。
    """
    return f"adhoc:{uuid.uuid4().hex}"


def _sanitize_fields(*values: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把要落库/进索引的每一段文本过一遍 `sanitize`——**全模块唯一的调用点**。

    §5.3 ① 要的是"写入侧只有一处"，不是"每个字段各抄一遍正则"：所以这里是一个
    漏斗，`write_memory` 之外没有任何地方再调它。返回（脱敏后的各字段,
    命中的形状名）——形状名可以写进理由，值本身不回显。
    """
    cleaned: list[str] = []
    shapes: list[str] = []
    for value in values:
        text, hits = sanitize(value)
        cleaned.append(text)
        shapes.extend(hits)
    return tuple(cleaned), tuple(dict.fromkeys(shapes))


def _audit(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    target: str,
    before: str = "",
    after: str = "",
    reason: str = "",
) -> None:
    """记一行审计（§4.3 第 6 块）。`reason` 与 `before`/`after` 里**不许有密钥值**：
    调用方传进来的都是脱敏后的文本或形状名。"""
    conn.execute(
        "INSERT INTO memory_audit(at, actor, action, target, before, after, reason)"
        " VALUES (?,?,?,?,?,?,?)",
        (time.time(), actor, action, target, before, after, reason),
    )


def _reserve_round(
    conn: sqlite3.Connection, ledger_key: str, source: str
) -> int | None:
    """抢一个轮次：INSERT 一行 `memory_write`；撞唯一索引返回 `None`。

    **这就是"用唯一索引挡"的全部实现**：没有 SELECT 预查。撞索引的语义是
    "这个 (round_key, source) 已经处理过了"——不管上次的结论是写入、拒收还是
    脱敏（三种都占掉了这一轮）。
    """
    try:
        cursor = conn.execute(
            "INSERT INTO memory_write"
            "(round_key, source, written, skipped, detail, created_at)"
            " VALUES (?,?,0,'','',?)",
            (ledger_key, source, time.time()),
        )
    except sqlite3.IntegrityError:
        return None
    return cursor.lastrowid


def _finish(
    conn: sqlite3.Connection, ledger_id: int, result: WriteResult, detail: str
) -> WriteResult:
    """把结论落回账本行并提交（账本行与 semantic 的写入在同一个事务里）。"""
    conn.execute(
        "UPDATE memory_write SET written = ?, skipped = ?, detail = ? WHERE id = ?",
        (1 if result.written else 0, result.skipped, detail, ledger_id),
    )
    conn.commit()
    return result


def _active_by_hash(conn: sqlite3.Connection, digest: str) -> Semantic | None:
    """按指纹找既有的 `active` 行（④ 的"命中既有条目"那半边）。

    这一查**只是为了把结果说清楚**（要 `touch` 哪一行）；真正的守门人是
    `idx_semantic_dedupe` 那个部分唯一索引——绕过应用层的直接 INSERT 一样被它拒。
    """
    row = conn.execute(
        "SELECT * FROM semantic WHERE content_hash = ? AND status = 'active' LIMIT 1",
        (digest,),
    ).fetchone()
    return None if row is None else _to_semantic(row)


def _theme_chars(overrides: dict | None) -> int:
    """主题键取前几个字：模块常量 + `overrides` 的裸键（见 `CONFLICT_THEME_CHARS`）。"""
    raw = (overrides or {}).get("conflict_theme_chars", CONFLICT_THEME_CHARS)
    return max(1, int(raw))


def _theme_keys(text: str, trigger: str, overrides: dict | None) -> set[str]:
    """一条记忆的"主题键"（§5.3 ⑤）：`text` 前 N 字规范化后的双字 shingle
    ∪ `trigger` 的词。

    为什么是双字 shingle 而不是"前 N 字逐字相等"：中文事实换个说法时开头
    常常只差一两个字（`测试命令是 pytest -q` vs `测试命令是 uv run pytest`），
    逐字相等会让"同主题"永远判不出来。双字 shingle 复用 `store.bigram_tokens`
    （T4 的双字口径，T5 起就是"重排/打分信号"），**不引新依赖、不引嵌入**。
    """
    prefix = normalize_key(text, overrides)[: _theme_chars(overrides)]
    keys = set(bigram_tokens(prefix))
    for token in re.split(r"[,，、;；/\s]+", trigger):
        normalized = normalize_key(token, overrides)
        if normalized:
            keys.add(normalized)
    return keys


def _find_conflict(
    conn: sqlite3.Connection, item: Semantic, overrides: dict | None
) -> Semantic | None:
    """找一条"可能和 `item` 矛盾"的既有行（§5.3 ⑤ 的机械部分）。

    判据四条：同 `kind`、既行 `active`、文本规范化后**不同**（相同是判重的事）、
    主题键交集非空。返回**最近的一条**候选（`id` 最大）——取代链一次走一步，
    新条不需要把历史上所有同主题的行一次全取代。
    """
    keys = _theme_keys(item.text, item.trigger, overrides)
    if not keys:
        return None
    item_key = normalize_key(item.text, overrides)
    rows = conn.execute(
        "SELECT * FROM semantic WHERE kind = ? AND status = 'active' AND id <> ?"
        " ORDER BY id DESC",
        (item.kind, item.id),
    ).fetchall()
    for row in rows:
        old = _to_semantic(row)
        if normalize_key(old.text, overrides) == item_key:
            continue
        if _theme_keys(old.text, old.trigger, overrides) & keys:
            return old
    return None


def _conflict_verdict(
    decide: Callable[[Semantic, WriteRequest], str] | None,
    old: Semantic,
    request: WriteRequest,
) -> str | None:
    """冲突判定（§5.3 ⑤）：拿到可解析的结论词就返回它，判定失败返回 `None`。

    这一次判定按设计稿要由**独立上下文的调用**给出（模型/人），所以它在这里是
    一个**可注入的钩子**：本模块不引模型、不引依赖，也不假装判过。
    - `decide=None`（没有判定调用）→ `None` = 判定失败；
    - 判定调用抛异常 → `None`（判定是可失败点，失败不许把写入炸掉）；
    - 返回的既不是"矛盾/补充/无关" → `None`（"模型没给出可解析结果"同一形态）。
    """
    if decide is None:
        return None
    try:
        raw = decide(old, request)
    except Exception:  # noqa: BLE001 - 判定失败是一种**预期**结果，不是崩溃
        return None
    verdict = str(raw or "").strip()
    return verdict if verdict in CONFLICT_VERDICTS else None


def _link(
    conn: sqlite3.Connection,
    *,
    src_kind: str,
    src_id: int,
    dst_kind: str,
    dst_id: int,
    relation: str,
) -> None:
    """写一条引用边（§4.3 第 4 块）。方向 = **新 → 旧**（`supersedes` 读作
    "新行取代了旧行"），`duplicate_of`/`needs_review` 同理。"""
    conn.execute(
        "INSERT INTO memory_link"
        "(src_kind, src_id, dst_kind, dst_id, relation, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (src_kind, src_id, dst_kind, dst_id, relation, time.time()),
    )


def write_memory(
    conn: sqlite3.Connection,
    request: WriteRequest,
    *,
    decide: Callable[[Semantic, WriteRequest], str] | None = None,
    overrides: dict | None = None,
) -> WriteResult:
    """五道门禁的唯一写入口（设计稿 §5.3）：一次写入 = 一次判定 + 一行账本。

    顺序与理由见本节开头的注释。三条使用纪律：

    - **`write_memory` 拥有自己的事务**（成功与拒绝都会 `commit`，撞索引会
      `rollback`）：调用方不要在同一个连接上夹带未提交的写入；
    - **拒收也是结果，不是异常**：`evidence` 缺失（J2）、寒暄（J3）、交付正文
      （J4）都返回 `WriteResult(written=False, skipped='denied', reason=…)`，
      理由可以直接打印给模型/人看（§5.2：让它知道"为什么没记住"）；
    - **近似改写不合并是已知代价**（§5.3 ④ 第 2 级不在本轮的落地范围）：
      `用 uv 管理依赖` 与 `本仓统一用 uv` 指纹不同 → 两条都收。这一条如实写在
      用例（`test_同义改写不合并_已知代价`）里，不假装是语义去重。
    """
    kind = (request.kind or "").strip()
    text = (request.text or "").strip()
    evidence = (request.evidence or "").strip()
    trigger = (request.trigger or "").strip()
    steps = (request.steps or "").strip()
    source = (request.source or "").strip()
    scope = (request.scope or "").strip() or "workspace"
    scope_id = (request.scope_id or "").strip()
    actor = _actor_for(source)

    # ③ 账本先抢位（理由见上）。空 round_key → 一次性键，见 `_adhoc_round_key`。
    round_key = (request.round_key or "").strip()
    ledger_key = round_key or _adhoc_round_key()
    ledger_id = _reserve_round(conn, ledger_key, source)
    if ledger_id is None:
        conn.rollback()
        return WriteResult(
            written=False,
            skipped="duplicate",
            reason=(
                f"同一轮不重复写：round_key={ledger_key!r} + source={source!r} "
                "已经有账本行（memory_write 的 idx_write_round 唯一索引挡住了），"
                "本次没有落库"
            ),
        )

    # ① 脱敏：本模块唯一一处调用。命中就不落库——密钥一旦进去就是长期慢性泄漏
    # （设计稿 §5.3 ①），而且 `semantic_fts` 也会把它索引一遍。
    cleaned, shapes = _sanitize_fields(text, trigger, steps, evidence)
    text, trigger, steps, evidence = cleaned
    if shapes:
        shape_names = "、".join(shapes)
        detail = f"命中密钥形状：{shape_names}"
        _audit(
            conn,
            actor=actor,
            action="add",
            target=f"{kind or '条目'}:rejected",
            reason=f"写入前脱敏命中：{shape_names}（值不落库、不进索引，只留形状名）",
        )
        return _finish(
            conn,
            ledger_id,
            WriteResult(
                written=False,
                skipped="redacted",
                reason=(
                    f"命中密钥形状：{shape_names}：值不落库、不进索引，"
                    "只留形状名（整改后重写）"
                ),
                shapes=shapes,
            ),
            detail,
        )

    # ② 判据 J1–J5（J2 = 可核对）。`kind` 先校验：表里没有 `CHECK` 约束，
    # 非法 kind 落库就是脏数据（设计稿 §4.3 的枚举）。
    if kind not in SEMANTIC_KINDS:
        reason = f"kind 非法：{kind!r}，合法取值 {'/'.join(SEMANTIC_KINDS)}"
        _audit(conn, actor=actor, action="add", target="条目:rejected", reason=reason)
        return _finish(
            conn,
            ledger_id,
            WriteResult(written=False, skipped="denied", reason=reason),
            reason,
        )
    verdict = judge(kind, text, trigger, evidence=evidence, overrides=overrides)
    if not verdict.ok:
        # 判据不过 = 降级为 episode 而非丢弃（§5.3 ②）。**降级出口（写
        # `memory.kind='episode'`）在 `archive.py`，不在本轮的改动面上**：
        # 这里只把"这条被拒了、为什么"落进账本与 audit，不静默丢。
        _audit(
            conn,
            actor=actor,
            action="add",
            target=f"{kind}:rejected",
            after=text,
            reason=verdict.reason,
        )
        return _finish(
            conn,
            ledger_id,
            WriteResult(written=False, skipped="denied", reason=verdict.reason),
            verdict.reason,
        )

    # ④ 去重：指纹为空 = 归一化后没有任何可比内容。这种条目**不能落库**：
    # `idx_semantic_dedupe` 是按 `content_hash` 唯一的部分索引，空指纹会让
    # 第二条空指纹条目撞上第一条、被误报成"同一条"。
    digest = compute_content_hash(text, overrides)
    if not digest:
        reason = "归一化后没有可比内容（content_hash 为空）：它不是一条可检索的事实"
        _audit(conn, actor=actor, action="add", target=f"{kind}:rejected", reason=reason)
        return _finish(
            conn,
            ledger_id,
            WriteResult(written=False, skipped="denied", reason=reason),
            reason,
        )
    existing = _active_by_hash(conn, digest)
    if existing is not None:
        touch_semantic(conn, existing.id)
        _audit(
            conn,
            actor=actor,
            action="edit",
            target=_ref(existing),
            reason=f"duplicate：同一 content_hash（{digest}）已有 active 行，只记一次使用",
        )
        return _finish(
            conn,
            ledger_id,
            WriteResult(
                written=False,
                skipped="duplicate",
                reason=(
                    f"同一条已经落过：content_hash 命中 {_ref(existing)}，"
                    "只记一次使用（use_count +1）、不新增行"
                ),
                semantic_id=existing.id,
                duplicate_of=existing.id,
            ),
            f"duplicate {_ref(existing)}",
        )

    try:
        item = insert_semantic(
            conn,
            kind,
            text,
            trigger=trigger,
            evidence=evidence,
            source=source,
            scope=scope,
            scope_id=scope_id,
            steps=steps,
            confidence=float(request.confidence),
            importance=float(request.importance),
            content_hash=digest,
            overrides=overrides,
        )
    except sqlite3.IntegrityError:
        # 竞态兜底：**唯一部分索引才是守门人**，应用层那一查只是为了结果可读。
        # 走到这里说明"查之后、写之前"有另一条 active 同 hash 行落下。回滚
        # （连带撤掉刚抢的账本行）→ 重新抢位 → 按判重给可读结论。
        conn.rollback()
        ledger_id = _reserve_round(conn, ledger_key, source)
        if ledger_id is None:  # pragma: no cover - 回滚之后不该再撞
            return WriteResult(
                written=False,
                skipped="duplicate",
                reason="同一轮在重试期间被别人写掉了：本次没有落库",
            )
        existing = _active_by_hash(conn, digest)
        if existing is not None:
            touch_semantic(conn, existing.id)
        return _finish(
            conn,
            ledger_id,
            WriteResult(
                written=False,
                skipped="duplicate",
                reason=(
                    "同一条已经被落过了（写入瞬间撞上 idx_semantic_dedupe 的"
                    f"唯一索引）：content_hash={digest}，不新增行"
                ),
                semantic_id=None if existing is None else existing.id,
                duplicate_of=None if existing is None else existing.id,
            ),
            "duplicate（并发/竞态：索引拒了第二次插入）",
        )

    # ⑤ 冲突：机械判据只给"候选"，结论由**独立上下文的判定调用**给（可注入）。
    old = _find_conflict(conn, item, overrides)
    if old is None:
        _audit(
            conn,
            actor=actor,
            action="add",
            target=_ref(item),
            after=item.text,
            reason=f"收下（source={source or 'unknown'}，evidence={item.evidence}）",
        )
        return _finish(
            conn,
            ledger_id,
            WriteResult(
                written=True,
                ref=_ref(item),
                semantic_id=item.id,
                reason=f"写入 {_ref(item)}",
            ),
            "",
        )

    verdict_text = _conflict_verdict(decide, old, request)
    if verdict_text == CONFLICT_CONTRADICTION:
        # 新行 active、旧行 superseded + superseded_by，**旧行不删**（§5.3 ⑤）：
        # `show <旧 id>` 仍能看到"我们曾经这么认为"。
        conn.execute(
            "UPDATE semantic SET status = 'superseded', superseded_by = ? WHERE id = ?",
            (item.id, old.id),
        )
        _link(
            conn,
            src_kind="semantic",
            src_id=item.id,
            dst_kind="semantic",
            dst_id=old.id,
            relation=SUPERSEDES_RELATION,
        )
        _audit(
            conn,
            actor=actor,
            action="supersede",
            target=_ref(old),
            before=old.text,
            after=item.text,
            reason=f"冲突判定：{verdict_text}；旧行保留（status=superseded），新行 active",
        )
        conn.commit()
        return _finish(
            conn,
            ledger_id,
            WriteResult(
                written=True,
                ref=_ref(item),
                skipped="conflict",
                semantic_id=item.id,
                superseded_id=old.id,
                reason=(
                    f"写入 {_ref(item)}，并把 {_ref(old)} 标为 superseded"
                    "（旧行仍可查：两条都查得到，active 的是新的那条）"
                ),
            ),
            f"supersede {_ref(old)} → {_ref(item)}",
        )

    if verdict_text is None:
        # 判定失败（没给判定调用 / 判定不可解析）：**不许静默选一个**，默认保留
        # 两条 active 并标 needs_review（§5.3 ⑤ 原话："静默丢一条比留着两条更危险"）。
        _link(
            conn,
            src_kind="semantic",
            src_id=item.id,
            dst_kind="semantic",
            dst_id=old.id,
            relation=NEEDS_REVIEW_RELATION,
        )
        _audit(
            conn,
            actor=actor,
            action="add",
            target=_ref(item),
            after=item.text,
            reason=(
                f"与 {_ref(old)} 主题键重叠但没有可解析的冲突判定："
                "两条都保持 active，标 needs_review 待复核"
            ),
        )
        conn.commit()
        return _finish(
            conn,
            ledger_id,
            WriteResult(
                written=True,
                ref=_ref(item),
                skipped="conflict",
                semantic_id=item.id,
                reason=(
                    f"写入 {_ref(item)}；与 {_ref(old)} 可能冲突但没判定出结论："
                    "两条都 active、标 needs_review 待复核（没有静默选一个）"
                ),
            ),
            f"needs_review {_ref(old)} ↔ {_ref(item)}",
        )

    # 判定成功且不是"矛盾"（"补充"/"无关"）：机械的主题键重叠只是候选，
    # 判定说不矛盾就不动旧行，也不打 needs_review（那会让标记失去意义）。
    _audit(
        conn,
        actor=actor,
        action="add",
        target=_ref(item),
        after=item.text,
        reason=f"与 {_ref(old)} 主题键重叠，判定为「{verdict_text}」：不动旧行",
    )
    return _finish(
        conn,
        ledger_id,
        WriteResult(
            written=True,
            ref=_ref(item),
            skipped="conflict",
            semantic_id=item.id,
            reason=f"写入 {_ref(item)}；与 {_ref(old)} 的判定结论是「{verdict_text}」，不动旧行",
        ),
        f"conflict-candidate {_ref(old)}：{verdict_text}",
    )


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
