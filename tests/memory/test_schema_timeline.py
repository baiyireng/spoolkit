"""schema v4：状态与时序六表（`user_version` 3→4）。

设计稿 §15.1（`memory_event` / `memory_state` 的字段与 `is_derived`）/ §15.2（键与
别名映射）/ §15.3 第 4 条（金额不许浮点）/ §15.4（事件永不删除，只有软撤销）/
§15.5 硬约束 3（`tz_offset_min` 必填）/ §15.7（表与迁移）+ 计划 Task 3。

这里钉七件事：

1. **六张表与索引就位**、`user_version=4`，且列清单与 `timeline/models.py` 的
   `FIELD_SPECS` **逐列一致**（DDL 与常量是两份独立的表示，任一处改坏都会红）；
2. **`memory_state` 的双时态形态 + `is_derived` DDL 默认 1**（用例名
   `test_state_columns` 与计划「怎么证伪」表里点的名字一致）：`id` 自增主键、
   `UNIQUE(entity, attribute, valid_from)`、**部分唯一索引** `idx_state_current`
   （`WHERE valid_to IS NULL`，每个键至多一行当前值）、
   **自引用外键** `superseded_by INTEGER REFERENCES memory_state(id)`（原设计的
   `(entity, attribute)` 联合主键与 §15.4 冲突，已就地改成这一形态）；
3. **别名唯一**（`test_alias_unique`，同上）：同一 `alias` 第二次写入被数据库挡住；
4. **金额/数量列不是 `REAL`**（§15.3 第 4 条）：`value` 一律 `TEXT`；
5. **既有表逐字/逐行不变**：P0 的四种钉法——① 冻结建表文本（`memory` 从 P0 的
   用例导入，`episode` 与两张 FTS 表是这里的字面量；再加整库 DDL 指纹的
   SHA-256 与两个 schema 常量的整段哈希，任何一个字节变了都红）、
   ② 列清单元组、③ `MEMORY_SCHEMA` / `SEMANTIC_SCHEMA` 片段、
   ④ 升级前后的 DDL 指纹；**真库副本**（`D:/workSpace/agentWorkSpace/.agent/`
   `memory.db`，`user_version=0`）上也跑同一份比对（`test_真库副本...`，库不在
   就 skip；**只读复制到 tmp，原库一个字节都不写**）；
6. **幂等**：连跑两次结构与行数不变、版本号被退回时不重复建表；
7. **不建触发器**、前向兼容、失败给人话且不记版本号。

**后续版本**：T11 补缺轮往 `_MIGRATIONS` **追加**了 v5（`memory_event_fts`，见
`tests/memory/test_schema_event_fts.py`）。本文件因此分成两种断言：**v4 那一步**
的行为（用 `store._MIGRATIONS[:4]` 单独跑，逐字钉住"v4 恰好新增六表五索引"）
与**版本号相关**的断言（一律用 `SCHEMA_VERSION`，不写死 4）——追加版本不该让
"v4 做对了什么"变得不可验证。
"""

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from spoolkit.memory import store
from spoolkit.memory.store import (
    MEMORY_SCHEMA,
    SCHEMA_VERSION,
    SEMANTIC_SCHEMA,
    TIMELINE_SCHEMA,
    MemorySchemaError,
    init_memory_schema,
)
from spoolkit.memory.timeline.models import (
    EVENT_OPS,
    EVENT_STATUSES,
    FIELD_SPECS,
    STATE_REASONS,
    STATE_STATUSES,
)
from spoolkit.store.db import open_db
from tests.memory.test_schema_semantic import LEDGER_COLUMNS, SEMANTIC_COLUMNS
from tests.memory.test_schema_version import (
    FROZEN_MEMORY_COLUMNS,
    FROZEN_MEMORY_TABLE_SQL,
)

# v4 建出来的六张表与五个索引（计划 Task 3 要点：索引至少那四类事件索引，
# 外加取代链要的部分唯一索引 `idx_state_current`）。
V4_TABLES = (
    "memory_event",
    "memory_state",
    "entity_attribute",
    "entity_alias",
    "attribute_alias",
    "memory_event_archive",
)
V4_INDEXES = (
    "idx_event_key_at",
    "idx_event_key_status",
    "idx_event_hash",
    "idx_event_at",
    "idx_state_current",
)

# 真实工作区的记忆库：**只读复制**到 pytest 的 tmp 目录上跑升级，原库不写
# （取证口径照 `plans/2026-10-02-memory-p1-plan.md` 的 T2 要点：副本落在仓内，
# 不许在原库上跑）。库不在（别人的机器 / CI）就 skip，不当红。
REAL_DB = Path(r"D:\workSpace\agentWorkSpace\.agent\memory.db")

# 升级前（P1 批次 1 收尾、`user_version=3`）**既有表**的建表文本，逐字冻结。
# `memory` 那一份从 P0 的用例导入，不另抄一遍；`episode` 是 P0 加过 5 列的现场
# （末行 `, scope ...` 那段是 `ALTER TABLE` 追加的结果，SQLite 自己拼的）。
FROZEN_EPISODE_TABLE_SQL = "\n".join(
    (
        "CREATE TABLE episode (",
        "    id         INTEGER PRIMARY KEY,",
        "    session_id TEXT NOT NULL,",
        "    task       TEXT NOT NULL,",
        "    summary    TEXT NOT NULL,",
        "    outcome    TEXT NOT NULL,",
        "    created_at REAL NOT NULL",
        ", scope TEXT NOT NULL DEFAULT 'session', scope_id TEXT NOT NULL DEFAULT '',"
        " verified INTEGER NOT NULL DEFAULT 0, verify_detail TEXT NOT NULL DEFAULT '',"
        " archived INTEGER NOT NULL DEFAULT 0)",
    )
)
FROZEN_MEMORY_FTS_SQL = (
    "CREATE VIRTUAL TABLE memory_fts USING fts5(text, kind, trigger, source)"
)
FROZEN_SEMANTIC_FTS_SQL = (
    "CREATE VIRTUAL TABLE semantic_fts USING fts5(text, trigger, evidence)"
)

# FTS5 的影子表（`memory_fts_data` 等）：FTS5 内部实现，DDL 随 SQLite 版本走，
# 不是「我们写的表结构」，所以不进指纹冻结。
FTS_SHADOW_PREFIX = ("memory_fts_", "semantic_fts_", "sqlite_")

# 既有索引的名字（升级前就有的 9 个）。
EXISTING_INDEXES = (
    "idx_episode_created",
    "idx_episode_session",
    "idx_link_dst",
    "idx_link_src",
    "idx_memory_kind",
    "idx_semantic_dedupe",
    "idx_semantic_kind",
    "idx_semantic_scope",
    "idx_write_round",
)

# 上面 11 张表 + 9 个索引的 DDL 指纹（`名字 → sqlite_master.sql`，排序后 JSON）
# 的 SHA-256。取值来自**加 v4 之前**在真 v3 库上抓的读数（`.tmp/probe_pre_v4.py`），
# 并与「加了 v4 之后只跑 v1..v3」的现场逐字比对过（任务报告里有两份读数）。
FROZEN_V3_SCHEMA_SHA256 = (
    "3de6a3212f1ed20f37d797e6f5379a0701b2f2cfa44b5979b383cada6dfc1e80"
)
# 两个既有 schema 常量的**整段文本**哈希：库里的指纹抓不到 `CREATE TABLE` 之外的
# 注释，所以常量本身也要逐字节钉住（含表外的注释行）。
FROZEN_MEMORY_SCHEMA_SHA256 = (
    "fa5bda0bd5d8edc0b68545adddba3857d3995109ee2bcc2a561c4ca22e8dfaf3"
)
FROZEN_SEMANTIC_SCHEMA_SHA256 = (
    "6b52783fe5a72c6deda95ebfec8490a32d986c22af336ab785ac4b7bb66d0129"
)

# 既有表的列清单元组（P0 的第二种钉法）。`memory` / `semantic` / 四张账本表
# 复用既有用例的常量，不重复声明（照 `test_schema_semantic.py` 的做法）。
FROZEN_EXISTING_COLUMNS: dict[str, tuple[str, ...]] = {
    "memory": FROZEN_MEMORY_COLUMNS,
    "episode": (
        "id",
        "session_id",
        "task",
        "summary",
        "outcome",
        "created_at",
        "scope",
        "scope_id",
        "verified",
        "verify_detail",
        "archived",
    ),
    "session": (
        "id",
        "project_root",
        "model",
        "context_limit",
        "created_at",
        "last_active",
    ),
    "transcript": ("id", "session_id", "role", "content", "meta", "created_at"),
    "memory_fts": ("text", "kind", "trigger", "source"),
    "semantic": SEMANTIC_COLUMNS,
    "semantic_fts": ("text", "trigger", "evidence"),
    **LEDGER_COLUMNS,
}

# 「有行数的既有表」：升级前后逐行比对的名单（FTS 影子表不数，主表数）。
EXISTING_TABLES = (
    "memory",
    "episode",
    "session",
    "transcript",
    "memory_fts",
    "semantic",
    "memory_link",
    "memory_write",
    "memory_audit",
    "memory_decision",
)

# 六张表里允许 `REAL` 的列：时间戳与置信度。**金额/数量列不许在这里**（§15.3 第 4 条）。
REAL_ALLOWED_COLUMNS = {
    "at_abs",
    "valid_from",
    "valid_to",
    "confidence",
    "computed_at",
    "created_at",  # 两张别名表的写入时刻，是时间戳不是金额
}


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row["name"] for row in conn.execute(f"PRAGMA table_info({table})"))


def _table_info(conn: sqlite3.Connection, table: str) -> tuple[tuple, ...]:
    """`PRAGMA table_info` 的六元组序列（与 `FieldSpec` 同形的那五项取出来）。"""
    return tuple(
        (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
        for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _names(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    }


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _ddl_fingerprint(conn: sqlite3.Connection) -> dict[str, str]:
    """升级前后的 DDL 指纹：名字 → 建表/建索引语句（P0 的第四种钉法的形态）。"""
    return {
        row["name"]: row["sql"] or ""
        for row in conn.execute("SELECT name, sql FROM sqlite_master")
        if not row["name"].startswith("sqlite_")
    }


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in EXISTING_TABLES
    }


def _canonical(fingerprint: dict[str, str]) -> str:
    """把 DDL 指纹变成可哈希的规范文本（排序后 JSON，不转义中文）。"""
    return json.dumps(sorted(fingerprint.items()), ensure_ascii=False, indent=1)


def _existing_fingerprint(conn: sqlite3.Connection) -> dict[str, str]:
    """升级前就有的对象（表 + 索引）的 DDL 指纹；FTS5 影子表不算我们的结构。"""
    return {
        name: sql
        for name, sql in _ddl_fingerprint(conn).items()
        if not name.startswith(FTS_SHADOW_PREFIX)
    }


def _unique_index_columns(
    conn: sqlite3.Connection, table: str
) -> set[tuple[str, ...]]:
    """表上真正的唯一索引（含主键自带的 `sqlite_autoindex_*`）覆盖的列组合。"""
    found: set[tuple[str, ...]] = set()
    for row in conn.execute(f"PRAGMA index_list({table})"):
        if not row["unique"]:
            continue
        found.add(
            tuple(
                info["name"]
                for info in conn.execute(f"PRAGMA index_info({row['name']})")
            )
        )
    return found


def _v3_db(tmp_path: Path) -> sqlite3.Connection:
    """一个**真的 v3 库**：跑过 v1 / v2 / v3 三步、`user_version=3`。

    不手工把版本号设成 3 凑：那样 `episode` 的 5 列、`memory_fts` 的四列都不是
    v3 的现场。这里让真的那三步自己跑（`_apply_step` 是既有代码）。
    """
    conn = open_db(tmp_path / "memory.db")
    conn.executescript(MEMORY_SCHEMA)
    for version, name, apply in store._MIGRATIONS[:3]:
        store._apply_step(conn, version, name, apply)
    assert _user_version(conn) == 3
    return conn


def _v3_db_with_data(tmp_path: Path) -> sqlite3.Connection:
    """v3 库 + **每张既有表至少一行**（升级前后要逐行比对）。

    直接写 SQL：这些表都有别的任务在用，这里要的只是「升级前有数据」这个现场。
    """
    conn = _v3_db(tmp_path)
    conn.execute(
        "INSERT INTO memory(kind, text, trigger, source, created_at)"
        " VALUES ('fact','旧事实','旧','ep#1',1.0)"
    )
    conn.execute(
        "INSERT INTO memory_fts(rowid, text, kind, trigger, source)"
        " VALUES (1,'旧 事 实','fact','旧','ep#1')"
    )
    conn.execute(
        "INSERT INTO episode(session_id, task, summary, outcome, created_at)"
        " VALUES ('s1','旧任务','旧任务：已完成','success',1.0)"
    )
    conn.execute(
        "INSERT INTO session(id, project_root, created_at, last_active)"
        " VALUES ('s1','D:/w',1.0,1.0)"
    )
    conn.execute(
        "INSERT INTO transcript(session_id, role, content, created_at)"
        " VALUES ('s1','user','旧对话',1.0)"
    )
    conn.execute(
        "INSERT INTO semantic(kind, text, content_hash, created_at)"
        " VALUES ('fact','旧语义','h1',1.0)"
    )
    conn.execute(
        "INSERT INTO memory_link(src_kind, src_id, dst_kind, dst_id, relation, created_at)"
        " VALUES ('episode',1,'semantic',1,'derived_from',1.0)"
    )
    conn.execute(
        "INSERT INTO memory_write(round_key, source, created_at)"
        " VALUES ('r1','user',1.0)"
    )
    conn.execute(
        "INSERT INTO memory_audit(at, actor, action, target)"
        " VALUES (1.0,'cli','add','semantic:1')"
    )
    conn.execute(
        "INSERT INTO memory_decision(at, session_id, task_id, channel, candidates, chosen)"
        " VALUES (1.0,'s1','t1','hot',3,'[]')"
    )
    conn.commit()
    return conn


# ---- 验收 1：六张表与索引就位、user_version=4 -------------------------------


def test_空库升级到v4六张表与索引就位(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    assert SCHEMA_VERSION >= 4  # v5 是后来追加的（见 test_schema_event_fts.py）
    assert _user_version(conn) == SCHEMA_VERSION
    assert set(V4_TABLES) <= _names(conn, "table")
    assert set(V4_INDEXES) <= _names(conn, "index")
    # 归档表与 `memory_event` 同形（超限搬最旧时 event id 不变）。
    assert _columns(conn, "memory_event_archive") == _columns(conn, "memory_event")
    conn.close()


def test_版本号严格递增且第4步是v4() -> None:
    versions = [version for version, _, _ in store._MIGRATIONS]
    assert versions == sorted(versions)
    assert versions[:4] == [1, 2, 3, 4]
    assert SCHEMA_VERSION == store._MIGRATIONS[-1][0]
    assert store._MIGRATIONS[3][1] == "建状态与时序六表"
    assert store._MIGRATIONS[3][2] is store._migrate_to_v4


@pytest.mark.parametrize("table", sorted(FIELD_SPECS))
def test_六表列清单与FIELD_SPECS逐字一致(table: str, tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    want = tuple(
        (spec.name, spec.type, int(spec.notnull), spec.default, spec.pk)
        for spec in FIELD_SPECS[table]
    )
    assert _table_info(conn, table) == want
    conn.close()


def test_state_columns(tmp_path: Path) -> None:
    """计划 T3 的硬约束：`memory_state` 双时态行 + 当前行唯一，`is_derived` 默认 1。

    （用例名照计划「怎么证伪」表：`test_schema_timeline.py::test_state_columns`。）
    """
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    info = _table_info(conn, "memory_state")
    assert tuple(row[0] for row in info) == tuple(
        spec.name for spec in FIELD_SPECS["memory_state"]
    )
    # 唯一主键是 `id`（§15.4 的 `superseded_by` 要引用它）；`entity` / `attribute`
    # 不再是主键——它们上面挂的是「同刻唯一」与「当前行唯一」两条**别的**约束。
    assert tuple(row[0] for row in info if row[4]) == ("id",)
    # `valid_from` NOT NULL（没有「不知道从哪生效」的行）、`valid_to` 可空（NULL = 当前）。
    valid_from = next(row for row in info if row[0] == "valid_from")
    valid_to = next(row for row in info if row[0] == "valid_to")
    assert (valid_from[1], valid_from[2]) == ("REAL", 1)
    assert (valid_to[1], valid_to[2]) == ("REAL", 0)
    # 同键同生效时刻只有一行：`UNIQUE(entity, attribute, valid_from)`。
    assert ("entity", "attribute", "valid_from") in _unique_index_columns(
        conn, "memory_state"
    )
    # 部分唯一索引：**只**约束当前行，历史行共存——所以 SQL 原文里必须有 WHERE。
    index_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_state_current'"
    ).fetchone()[0]
    flat = re.sub(r"\s+", " ", index_sql)
    assert flat.startswith("CREATE UNIQUE INDEX idx_state_current")
    assert "WHERE valid_to IS NULL" in flat
    # 自引用外键真的挂在 DDL 上（不是「一个普通 INTEGER 列」）。
    assert (
        "memory_state",
        "superseded_by",
        "id",
    ) in {
        (row["table"], row["from"], row["to"])
        for row in conn.execute("PRAGMA foreign_key_list(memory_state)")
    }
    is_derived = next(row for row in info if row[0] == "is_derived")
    assert is_derived[1] == "INTEGER" and is_derived[2] == 1
    assert is_derived[3] == "1"  # dflt_value：DDL 默认 1，恒为真（§15.1）
    # DDL 原文里也必须是 `DEFAULT 1`（常量被人顺手删掉默认值时同样红）。
    assert re.search(r"is_derived\s+INTEGER NOT NULL DEFAULT 1", TIMELINE_SCHEMA)
    # 缺基准时 `value` 留空：这一列可空（§15.6 第 1 条）。
    value = next(row for row in info if row[0] == "value")
    assert value[2] == 0
    conn.close()


def test_金额与数量列不是REAL(tmp_path: Path) -> None:
    """§15.3 第 4 条：不许浮点累加金额——`value` 一律 `TEXT`（整数最小单位 / Decimal 串）。"""
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    for table in ("memory_event", "memory_state", "memory_event_archive"):
        assert dict((row[0], row[1]) for row in _table_info(conn, table))["value"] == "TEXT"
    # 六张表里出现 `REAL` 的列只许是时间戳与置信度——`value` 变成 REAL 会在这里红。
    real_columns = {
        spec.name for specs in FIELD_SPECS.values() for spec in specs if spec.type == "REAL"
    }
    assert real_columns <= REAL_ALLOWED_COLUMNS
    conn.close()


def test_event的时间字段按设计稿摆(tmp_path: Path) -> None:
    """§15.5：`at_abs` 解析失败可为 NULL；`tz_offset_min` 必填且不给默认值。"""
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    specs = {spec.name: spec for spec in FIELD_SPECS["memory_event"]}
    assert (specs["at_abs"].type, specs["at_abs"].notnull) == ("REAL", False)
    assert (specs["tz_offset_min"].type, specs["tz_offset_min"].notnull) == (
        "INTEGER",
        True,
    )
    # 不给默认值：默认 0 等于悄悄按 UTC 算日界线（§15.5 硬约束 3）。
    assert specs["tz_offset_min"].default is None
    assert specs["tz_name"].notnull is False
    assert specs["at_source"].notnull and specs["time_precision"].notnull
    # 常量说得再好，落库的 DDL 才算数：三列的 (type, notnull, default) 再从库里核一遍。
    stored = {
        row[0]: (row[1], row[2], row[3]) for row in _table_info(conn, "memory_event")
    }
    assert stored["at_abs"] == ("REAL", 0, None)
    assert stored["tz_offset_min"] == ("INTEGER", 1, None)
    assert stored["tz_name"] == ("TEXT", 0, None)
    conn.close()


def test_event_status只有两个合法值() -> None:
    """§15.4：事件永不删除，纠错走软撤销——状态里没有第三个取值。"""
    assert EVENT_STATUSES == ("active", "retracted")
    assert EVENT_OPS == ("delta", "set", "observe")
    assert STATE_STATUSES == ("active", "conflict", "needs_review")
    assert STATE_REASONS == (
        "missing_base",
        "unit_mismatch",
        "same_instant",
        "no_reducer",
        "time_unknown",
    )
    # 常量与 DDL 上的注释是同一句话（两边任一处漂了都会红）。
    assert "active|retracted" in TIMELINE_SCHEMA


def test_双时态_历史行共存而当前行至多一行(tmp_path: Path) -> None:
    """§15.4 两种问法各自都要答得出来：历史行共存 + 每个键至多一行当前值。

    「我当时说的」靠 `valid_from <= t < valid_to` 的历史行答；
    「现在是多少」靠 `status='active' AND valid_to IS NULL` 那一行答。
    如果 `(entity, attribute)` 是联合主键（原设计），第一种问法在库里就没有行可读。
    """
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    # 历史行 + 当前行：同键、不同 `valid_from`，可以共存（取代链的立身之本）。
    conn.execute(
        "INSERT INTO memory_state(entity, attribute, value, valid_from, valid_to, computed_at)"
        " VALUES ('wallet','balance','100',1.0,1.5,1.5)"
    )
    conn.execute(
        "INSERT INTO memory_state(entity, attribute, value, valid_from, computed_at)"
        " VALUES ('wallet','balance','130',2.0,2.0)"
    )
    conn.commit()

    # 第二条当前行（valid_to IS NULL）：必须被数据库挡住（部分唯一索引）。
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memory_state(entity, attribute, value, valid_from, computed_at)"
            " VALUES ('wallet','balance','120',3.0,3.0)"
        )
    conn.rollback()

    # 同键**同** `valid_from`：被 `UNIQUE(entity, attribute, valid_from)` 挡住。
    # 这里两行都是历史行（valid_to 都非空），所以红的只可能是那条 UNIQUE 约束，
    # 不是当前行索引——两条约束各自都被钉到了。
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memory_state(entity, attribute, value, valid_from, valid_to,"
            " computed_at) VALUES ('wallet','balance','90',1.0,1.6,1.6)"
        )
    conn.rollback()

    # 不同键可以各自有一行当前值；不写 `is_derived` 时落库就是 1（推导值）。
    conn.execute(
        "INSERT INTO memory_state(entity, attribute, value, valid_from, computed_at)"
        " VALUES ('wallet','currency','CNY',1.0,1.0)"
    )
    conn.commit()

    assert conn.execute("SELECT count(*) FROM memory_state").fetchone()[0] == 3
    # 每个键**至多一行**当前值（这里是两个键 → 两行）。
    assert (
        conn.execute(
            "SELECT count(*) FROM memory_state WHERE valid_to IS NULL"
        ).fetchone()[0]
        == 2
    )
    # 问法 ①「现在是多少」：当前行。
    assert (
        conn.execute(
            "SELECT value FROM memory_state WHERE entity='wallet' AND attribute='balance'"
            " AND status='active' AND valid_to IS NULL"
        ).fetchone()[0]
        == "130"
    )
    # 问法 ②「我当时说的」（t=1.2）：半开区间 [valid_from, valid_to) 命中历史行，
    # **旧行没有被删、也没有被改值**——这正是联合主键给不了的那一条。
    assert (
        conn.execute(
            "SELECT value FROM memory_state WHERE entity='wallet' AND attribute='balance'"
            " AND valid_from <= 1.2 AND (valid_to IS NULL OR valid_to > 1.2)"
        ).fetchone()[0]
        == "100"
    )
    assert (
        conn.execute(
            "SELECT is_derived FROM memory_state WHERE attribute = 'currency'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_superseded_by外键真的生效(tmp_path: Path) -> None:
    """`superseded_by` 是**真的** `REFERENCES memory_state(id)`，不是普通列。

    `open_db` 已经 `PRAGMA foreign_keys = ON`（`store/db.py:56`），所以这里插一个
    不存在的 id 必须报 `IntegrityError`；把 `REFERENCES` 去掉就变绿 → 这条用例红。
    """
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    conn.execute(
        "INSERT INTO memory_state(entity, attribute, value, valid_from, computed_at)"
        " VALUES ('wallet','balance','100',1.0,1.0)"
    )
    conn.commit()
    old_id = conn.execute("SELECT id FROM memory_state").fetchone()[0]

    # ① 指向一个不存在的 id：外键拒绝。
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memory_state(entity, attribute, value, valid_from, computed_at,"
            " superseded_by) VALUES ('wallet','currency','CNY',1.0,1.0,999)"
        )
    conn.rollback()
    # ② 「先指向新行、再插新行」也不行：外键不是 deferrable 的，写入顺序有硬要求
    #    （T9 的取代顺序因此被定死：关旧行 → 插新行拿 id → 旧行指向新行）。
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE memory_state SET valid_to = 2.0, superseded_by = 2 WHERE id = ?",
            (old_id,),
        )
    conn.rollback()

    # 正确的取代顺序（同一事务里做完，「至多一行当前值」才不会被看见中间态）。
    conn.execute("UPDATE memory_state SET valid_to = 2.0 WHERE id = ?", (old_id,))
    new_id = conn.execute(
        "INSERT INTO memory_state(entity, attribute, value, valid_from, computed_at)"
        " VALUES ('wallet','balance','130',2.0,2.0)"
    ).lastrowid
    conn.execute(
        "UPDATE memory_state SET superseded_by = ? WHERE id = ?", (new_id, old_id)
    )
    conn.commit()

    old_row = conn.execute(
        "SELECT id, value, valid_to, superseded_by FROM memory_state WHERE id = ?",
        (old_id,),
    ).fetchone()
    assert (old_row["value"], old_row["valid_to"], old_row["superseded_by"]) == (
        "100",
        2.0,
        new_id,
    )
    # 旧行仍可查，取代链能 JOIN 到新行（旧 → 新 单向可追，§15.4）。
    assert (
        conn.execute(
            "SELECT n.value FROM memory_state o JOIN memory_state n"
            " ON n.id = o.superseded_by WHERE o.id = ?",
            (old_id,),
        ).fetchone()[0]
        == "130"
    )
    # 取代做完后，该键仍然只有一行当前值。
    assert (
        conn.execute(
            "SELECT count(*) FROM memory_state WHERE entity='wallet'"
            " AND attribute='balance' AND valid_to IS NULL"
        ).fetchone()[0]
        == 1
    )
    # 外键的**声明**也要在库里（`PRAGMA foreign_key_list` 看得到目标表与目标列）。
    assert list(conn.execute("PRAGMA foreign_key_list(memory_state)"))[0]["table"] == (
        "memory_state"
    )
    conn.close()


def test_alias_unique(tmp_path: Path) -> None:
    """§15.2：一个 `alias` 只能指一个 `canonical`，冲突被数据库挡住。

    （用例名照计划「怎么证伪」表：`test_schema_timeline.py::test_alias_unique`。）
    """
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    conn.execute(
        "INSERT INTO entity_alias(alias, canonical, created_at)"
        " VALUES ('余额','wallet',1.0)"
    )
    conn.commit()
    # 同一个 alias 想指**另一个** canonical：拒绝合并（§15.2 第 4 行）。
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO entity_alias(alias, canonical, created_at)"
            " VALUES ('余额','cash',1.0)"
        )
    conn.rollback()
    # 指回同一个 canonical 也不行：这是唯一键，不是幂等 upsert。
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO entity_alias(alias, canonical, created_at)"
            " VALUES ('余额','wallet',2.0)"
        )
    conn.rollback()
    # 换一个 alias 指同一个 canonical 可以（「钱」「现金」都指 wallet）。
    conn.execute(
        "INSERT INTO entity_alias(alias, canonical, created_at)"
        " VALUES ('钱','wallet',1.0)"
    )
    conn.commit()
    # `attribute_alias` 同形，且与 `entity_alias` 是**两个命名空间**：
    # 「余额」在实体表里指 wallet，在属性表里指 balance，互不干扰。
    conn.execute(
        "INSERT INTO attribute_alias(alias, canonical, created_at)"
        " VALUES ('余额','balance',1.0)"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO attribute_alias(alias, canonical, created_at)"
            " VALUES ('余额','amount',1.0)"
        )
    conn.rollback()
    conn.execute(
        "INSERT INTO attribute_alias(alias, canonical, created_at)"
        " VALUES ('现金','balance',1.0)"
    )
    conn.commit()
    assert conn.execute("SELECT count(*) FROM entity_alias").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM attribute_alias").fetchone()[0] == 2
    # 机制在数据库层：两张别名表上都有覆盖 `alias` 的唯一索引（主键自带的那一个）。
    for table in ("entity_alias", "attribute_alias"):
        assert ("alias",) in _unique_index_columns(conn, table)
    # `entity_attribute` 还是 `(entity, attribute)` 联合主键（注册表：一个键一行）。
    assert ("entity", "attribute") in _unique_index_columns(conn, "entity_attribute")
    # `memory_state` 的唯一性**不是**主键给的那一条，而是「同刻唯一 + 当前行唯一」：
    # 同键的历史行必须能共存（这就是不能再用 `(entity, attribute)` 做 PK 的原因）。
    assert ("entity", "attribute", "valid_from") in _unique_index_columns(
        conn, "memory_state"
    )
    conn.close()


# ---- 验收 4：既有表一个字没动（P0 的四种钉法）------------------------------


def test_既有表逐字不变_冻结建表文本与列清单(tmp_path: Path) -> None:
    conn = _v3_db_with_data(tmp_path)
    before_counts = _row_counts(conn)

    init_memory_schema(conn)

    stored = _ddl_fingerprint(conn)
    # ① 冻结建表文本（落库后的样子）。SQLite 会去掉 `IF NOT EXISTS` 与结尾分号。
    assert stored["memory"] == FROZEN_MEMORY_TABLE_SQL
    assert stored["episode"] == FROZEN_EPISODE_TABLE_SQL
    assert stored["memory_fts"] == FROZEN_MEMORY_FTS_SQL
    assert stored["semantic_fts"] == FROZEN_SEMANTIC_FTS_SQL
    # ② 列清单元组（加列、改名、删列都会在这里红）：11 张既有表全查一遍。
    for table, columns in FROZEN_EXISTING_COLUMNS.items():
        assert _columns(conn, table) == columns, table
    # 逐行不变：一张既有表的行数都没被 v4 动过。
    assert _row_counts(conn) == before_counts
    conn.close()


def test_既有表逐字不变_整库DDL指纹哈希(tmp_path: Path) -> None:
    """11 张既有表 + 9 个既有索引的 DDL 指纹，逐字节冻结成一个 SHA-256。

    取值来自**加 v4 之前**在真 v3 库上抓的读数（`.tmp/probe_pre_v4.py`），
    并与「加 v4 之后只跑 v1..v3」的现场逐字比对过（报告里有两份读数）。
    列顺序、类型、表内注释、索引名——任何一个字节变了都会红。
    """
    conn = _v3_db(tmp_path)  # 只跑到 v3：v4 的六张表不该出现在这份指纹里
    fingerprint = _existing_fingerprint(conn)

    assert set(fingerprint) == set(FROZEN_EXISTING_COLUMNS) | set(EXISTING_INDEXES)
    assert (
        hashlib.sha256(_canonical(fingerprint).encode("utf-8")).hexdigest()
        == FROZEN_V3_SCHEMA_SHA256
    )
    conn.close()


def test_既有表逐字不变_两个schema常量没被动过() -> None:
    """③ 的一半：`MEMORY_SCHEMA` / `SEMANTIC_SCHEMA` 的**整段文本**逐字节冻结。

    库里的指纹抓不到 `CREATE TABLE` 之外的注释行，所以常量本身也要钉住。
    """
    assert (
        hashlib.sha256(MEMORY_SCHEMA.encode("utf-8")).hexdigest()
        == FROZEN_MEMORY_SCHEMA_SHA256
    )
    assert (
        hashlib.sha256(SEMANTIC_SCHEMA.encode("utf-8")).hexdigest()
        == FROZEN_SEMANTIC_SCHEMA_SHA256
    )


def test_既有表逐字不变_schema片段(tmp_path: Path) -> None:
    """③ 常量本身也没被人顺手改（P0 在 `MEMORY_SCHEMA` 上用的切法，语义照搬）。"""
    block = MEMORY_SCHEMA.split("CREATE TABLE IF NOT EXISTS memory", 1)[1]
    block = "CREATE TABLE memory" + block.split("\n);", 1)[0] + "\n)"
    assert block == FROZEN_MEMORY_TABLE_SQL

    # `semantic` 那一段：从常量里切出来，与**落库后**的文本逐字比（两边同时钉）。
    stored = _ddl_fingerprint(_v3_db_with_data(tmp_path))["semantic"]
    block = SEMANTIC_SCHEMA.split("CREATE TABLE IF NOT EXISTS semantic", 1)[1]
    block = "CREATE TABLE semantic" + block.split("\n);", 1)[0] + "\n)"
    assert block == stored


def test_既有对象逐字不变_升级前后DDL指纹(tmp_path: Path) -> None:
    """④ 升级前后的 DDL 指纹：既有对象全部逐字一致，新增的**恰好**是六表五索引。

    只跑 **v4 那一步**（`store._MIGRATIONS[3]`）：v5 之后 `init_memory_schema` 会
    再追加 `memory_event_fts`，那会让"v4 恰好新增了什么"这个断言失焦——v5 的
    同一份断言在 `test_schema_event_fts.py` 里。
    """
    conn = _v3_db_with_data(tmp_path)
    before = _ddl_fingerprint(conn)
    before_counts = _row_counts(conn)

    store._apply_step(conn, *store._MIGRATIONS[3])

    after = _ddl_fingerprint(conn)
    assert {name: after[name] for name in before} == before
    new_names = set(after) - set(before)
    # 「恰好」而不是「至少」：v4 不许顺手多建第五张表、第六个索引或触发器。
    assert new_names == set(V4_TABLES) | set(V4_INDEXES)
    assert _row_counts(conn) == before_counts
    conn.close()


@pytest.mark.skipif(not REAL_DB.exists(), reason=f"真实记忆库不在：{REAL_DB}")
def test_真库副本从user_version0升到v4既有对象逐行不变(tmp_path: Path) -> None:
    """真库副本（`user_version=0`）→ v4：既有对象 DDL 与行数逐行不变，新增恰好六表五索引。

    **只读复制**：`shutil.copy2` 把真库搬到 pytest 的 tmp 目录，原库一个字节都不写
    （跑完后面再 sha256 复核一次）。这份取证是 T2 那一份的口径照搬——在真库上跑
    `init_memory_schema` 是禁止的。

    两份副本：A 只跑到 v3（真数据 + v4 之前的结构），B 一路跑到 v4——两边都从同一个
    `user_version=0` 的真库副本出发，所以「既有对象」的比较不受 v1 加列的影响。
    """
    source_digest = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    copy_a = tmp_path / "to-v3.db"
    copy_b = tmp_path / "to-v4.db"
    shutil.copy2(REAL_DB, copy_a)
    shutil.copy2(REAL_DB, copy_b)

    conn_a = open_db(copy_a)
    assert _user_version(conn_a) == 0  # 真库还没在本地迁过（P0 的迁移命令是 opt-in）
    for version, name, apply in store._MIGRATIONS[:3]:
        store._apply_step(conn_a, version, name, apply)
    before = _ddl_fingerprint(conn_a)
    before_counts = _row_counts(conn_a)

    conn_b = open_db(copy_b)
    for version, name, apply in store._MIGRATIONS[:4]:  # 只跑到 v4（v5 见另一个文件）
        store._apply_step(conn_b, version, name, apply)

    assert _user_version(conn_b) == 4
    after = _ddl_fingerprint(conn_b)
    assert {name: after[name] for name in before} == before
    assert set(after) - set(before) == set(V4_TABLES) | set(V4_INDEXES)
    assert _row_counts(conn_b) == before_counts
    # 升级是纯追加：既有表的行数一行都没少（真库里的记忆是底账）。
    assert sum(before_counts.values()) == sum(_row_counts(conn_b).values())
    conn_a.close()
    conn_b.close()
    # 原库没被碰过。
    assert hashlib.sha256(REAL_DB.read_bytes()).hexdigest() == source_digest


# ---- 验收 2：幂等 -----------------------------------------------------------


def test_幂等_连跑两次表索引版本号与行数都不变(tmp_path: Path) -> None:
    conn = _v3_db_with_data(tmp_path)
    init_memory_schema(conn)
    first = (
        sorted(_names(conn, "table")),
        sorted(_names(conn, "index")),
        _ddl_fingerprint(conn),
        _user_version(conn),
        _row_counts(conn),
    )

    init_memory_schema(conn)  # 再跑两次：不重复建表、不报错
    init_memory_schema(conn)

    assert (
        sorted(_names(conn, "table")),
        sorted(_names(conn, "index")),
        _ddl_fingerprint(conn),
        _user_version(conn),
        _row_counts(conn),
    ) == first
    conn.close()


def test_版本被人为退回3时v4重跑也不重复建表(tmp_path: Path) -> None:
    """模拟「v4 建完表但版本号没记上」（`_apply_step` 的失败语义）→ 重跑不报错。

    版本号被退到 3 之后 `init_memory_schema` 会把 v4 与后来追加的 v5 一起补上，
    所以这里断言的是 `SCHEMA_VERSION`（v4 那一层的幂等在
    `test_schema_event_fts.py::test_版本被人为退回4时v5重跑也不重复插` 里单独钉）。
    """
    conn = _v3_db_with_data(tmp_path)
    init_memory_schema(conn)
    before_counts = _row_counts(conn)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()

    init_memory_schema(conn)

    assert _user_version(conn) == SCHEMA_VERSION
    assert set(V4_TABLES) <= _names(conn, "table")
    assert _row_counts(conn) == before_counts
    conn.close()


# ---- 不建触发器 / 前向兼容 / 失败给人话 -------------------------------------


def test_时序六表上不建触发器(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
    ).fetchone()[0] == 0
    conn.close()


def test_前向兼容_库比代码新时不动库也不报错(tmp_path: Path, capsys) -> None:
    conn = _v3_db_with_data(tmp_path)
    init_memory_schema(conn)
    conn.execute("DROP INDEX idx_event_at")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()

    init_memory_schema(conn)

    assert "idx_event_at" not in _names(conn, "index")  # 一步都没跑
    assert _user_version(conn) == SCHEMA_VERSION + 1  # 版本号没被写回低值
    out = capsys.readouterr().out
    assert "高于" in out and "跳过升级" in out
    conn.close()


def test_升级失败给的是人话且v4不记版本号(tmp_path: Path, monkeypatch) -> None:
    conn = _v3_db_with_data(tmp_path)

    def boom(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("模拟的磁盘错误")

    steps = store._MIGRATIONS[:3] + ((4, "建状态与时序六表", boom),)
    monkeypatch.setattr(store, "_MIGRATIONS", steps)

    with pytest.raises(MemorySchemaError) as excinfo:
        init_memory_schema(conn)

    message = str(excinfo.value)
    assert "记忆库结构升级失败" in message
    assert "第 4 步" in message and "建状态与时序六表" in message
    assert "模拟的磁盘错误" in message
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    # 失败的那一步**不记版本号**：库还停在 3，新表一张都没有，旧数据一条不少。
    assert _user_version(conn) == 3
    assert "memory_event" not in _names(conn, "table")
    assert _row_counts(conn)["memory"] == 1
    # 修掉原因后重跑就补上（这一步是幂等的）。
    monkeypatch.undo()
    init_memory_schema(conn)
    assert _user_version(conn) == SCHEMA_VERSION
    assert set(V4_TABLES) <= _names(conn, "table")
    conn.close()
