"""schema v5：`memory_event_fts`（`user_version` 4→5）——补 T11 报出的缺口。

**缺的是什么**：T11 把事件链做成一个整体单元，但 `memory_event` 没有 FTS 索引，
事件侧打不出 `bm25()` 原值，于是 `rank._chain_rel_part` 只能**如实**把链的 `rel`
记成 0——链只剩新近度与重要度在排序，"这个问题更相关"在链上等于不存在。
v5 补的就是这条数据通路（生产路径的读数见 `tests/memory/test_chain_relevance_v5.py`）。

这里钉六件事：

1. **`memory_event_fts` 就位**、`user_version=5`、列口径 = `MEMORY_EVENT_FTS_COLUMNS`
   （只索引 `raw_text`，理由写在 `store.py` 常量上方）、**不建触发器**；
2. **既有表逐字/逐行不变**，用本仓已有的四种钉法：① 冻结建表文本
   （`memory` / `episode` / 两张 FTS 表，从 v4 的用例导入，不另抄一份）、
   ② 列清单元组（既有 11 张表 + v4 六表走 `FIELD_SPECS`）、
   ③ schema 常量片段与整段哈希（`MEMORY_SCHEMA` / `SEMANTIC_SCHEMA` /
   `TIMELINE_SCHEMA` 三份**整段文本**的 SHA-256，加上库里的 DDL 指纹哈希）、
   ④ 升级前后 DDL 指纹（新增对象**恰好**是 `memory_event_fts` 与它的 FTS5 影子表）；
   **真库副本**（`D:/workSpace/agentWorkSpace/.agent/memory.db`，`user_version=0`）
   上跑同一份比对（`test_真库副本...`，库不在就 skip；**只读复制到 tmp**，
   原库一个字节都不写，跑完再 sha256 复核）；
3. **回填**：v5 之前写进 `memory_event` 的事件（用 SQL 直插，模拟旧代码）在升级后
   **全部**可检索；重复跑 v5 **不重复插**（行数一对一）；
4. **幂等**：`init_memory_schema` 连跑三次，表 / 索引 / DDL 指纹 / 版本号 / 行数全不变；
5. **前向兼容**：库比代码新时一步都不跑、版本号不被写回低值；
6. **失败给人话且不记版本号**：v5 那一步炸了 → `user_version` 停在 4、
   `memory_event_fts` 不存在、既有数据一条不少；修掉原因重跑就补上。

冻结读数的出处：`SHA256_V4_*` 与 `FROZEN_TIMELINE_SCHEMA_SHA256` 都是**加 v5
之前**抓的（`.tmp/probe_pre_v5.py` + 同一份代码的既有常量哈希），不是加完再回填的
——所以"v5 没动既有对象"这句话有两份独立读数对得上（全新 v4 库 / 真库副本升到 v4，
两个指纹逐位相同）。
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from spoolkit.memory import store
from spoolkit.memory.store import (
    MEMORY_EVENT_FTS_COLUMNS,
    MEMORY_SCHEMA,
    SCHEMA_VERSION,
    SEMANTIC_SCHEMA,
    TIMELINE_SCHEMA,
    MemorySchemaError,
    init_memory_schema,
    normalize_for_fts,
)
from spoolkit.memory.timeline.models import FIELD_SPECS
from spoolkit.store.db import open_db
from tests.memory.test_schema_timeline import (
    EXISTING_INDEXES,
    FROZEN_EPISODE_TABLE_SQL,
    FROZEN_EXISTING_COLUMNS,
    FROZEN_MEMORY_FTS_SQL,
    FROZEN_MEMORY_TABLE_SQL,
    FROZEN_SEMANTIC_FTS_SQL,
    V4_INDEXES,
    V4_TABLES,
    _canonical,
    _columns,
    _ddl_fingerprint,
    _existing_fingerprint,
    _names,
    _row_counts,
    _table_info,
    _user_version,
)

# 真实工作区的记忆库：**只读复制**到 pytest 的 tmp 目录上跑升级，原库不写。
REAL_DB = Path(r"D:\workSpace\agentWorkSpace\.agent\memory.db")

# ---- 加 v5 之前抓的四份读数（独立于本文件，见模块 docstring）-------------------

# ① 全新 v4 库与「真库副本升到 v4」两条路给出**同一个**指纹：
#    31 个对象（11 张表 + 9 个既有索引 + v4 的六表五索引）的 (名字 → DDL) 规范文本。
FROZEN_V4_SCHEMA_SHA256 = (
    "d98bc69cf860c6765c96fe672e2bcadd400b45e18ba532caa6c2542638c66166"
)
# ② 三个 schema 常量**整段文本**的哈希（库里的指纹抓不到 `CREATE TABLE` 之外的注释）。
FROZEN_MEMORY_SCHEMA_SHA256 = (
    "fa5bda0bd5d8edc0b68545adddba3857d3995109ee2bcc2a561c4ca22e8dfaf3"
)
FROZEN_SEMANTIC_SCHEMA_SHA256 = (
    "6b52783fe5a72c6deda95ebfec8490a32d986c22af336ab785ac4b7bb66d0129"
)
FROZEN_TIMELINE_SCHEMA_SHA256 = (
    "bf4cd3e3a8a9b74d25b20209c9b653b7f0d83f559f6a7a7c5214470df8519a38"
)

# v4 六表在升级前后的 DDL：一个字都不许动（`FIELD_SPECS` 是第二份独立的表示）。
FROZEN_V4_COLUMNS = {
    table: tuple(
        (spec.name, spec.type, int(spec.notnull), spec.default, spec.pk)
        for spec in FIELD_SPECS[table]
    )
    for table in V4_TABLES
}

# 新表落库后的样子（FTS5 会原样保留 DDL；SQLite 不去掉 fts5 的参数列表）。
FROZEN_MEMORY_EVENT_FTS_SQL = (
    "CREATE VIRTUAL TABLE memory_event_fts USING fts5(raw_text)"
)

# 事件表里"v5 之前就写好"的两条事件（模拟旧代码：`memory_event` 有行、FTS 还没有）。
OLD_EVENT_ROWS = (
    (
        "wallet", "balance", "set", "100", "CNY", 1.0, 480, None, "explicit", "minute",
        "session:s1#turn:1", "现在身上有 100 块", "user", 0.9, "hash-1", "active",
    ),
    (
        "wallet", "balance", "delta", "-20", "CNY", 2.0, 480, None, "explicit", "minute",
        "session:s1#turn:2", "今天花了 20", "user", 0.7, "hash-2", "active",
    ),
)


def _to_v4(conn: sqlite3.Connection) -> None:
    """只跑到 v4（v5 之前那一步）：`_apply_step` 是既有代码，不手工设版本号。"""
    for version, name, apply in store._MIGRATIONS[:4]:
        store._apply_step(conn, version, name, apply)
    assert _user_version(conn) == 4


def _v4_db(tmp_path: Path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "memory.db")
    _to_v4(conn)
    return conn


def _v4_db_with_data(tmp_path: Path) -> sqlite3.Connection:
    """v4 库 + 每张既有表至少一行 + `memory_event` 里两条**没有索引**的旧事件。

    `memory_event` 的那两行用**直插 SQL** 写（不是 `reduce.append_event`）：v5 之前
    写进去的事件正是这个现场——表里有行、`memory_event_fts` 还不存在。用
    `append_event` 反而测不到回填（它现在已经会同步写索引了）。
    """
    conn = _v4_db(tmp_path)
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
        "INSERT INTO semantic_fts(rowid, text, trigger, evidence)"
        " VALUES (1,'旧 语 义','','ep#1')"
    )
    for row in OLD_EVENT_ROWS:
        conn.execute(
            "INSERT INTO memory_event(entity, attribute, op, value, unit, at_abs,"
            " tz_offset_min, tz_name, at_source, time_precision, source_ref, raw_text,"
            " speaker, confidence, content_hash, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
    conn.execute(
        "INSERT INTO memory_state(entity, attribute, value, valid_from, computed_at)"
        " VALUES ('wallet','balance','80',2.0,2.0)"
    )
    conn.execute(
        "INSERT INTO entity_attribute(entity, attribute, type, reducer)"
        " VALUES ('wallet','balance','monetary','sum_delta')"
    )
    conn.execute(
        "INSERT INTO memory_link(src_kind, src_id, dst_kind, dst_id, relation, created_at)"
        " VALUES ('episode',1,'semantic',1,'derived_from',1.0)"
    )
    conn.execute(
        "INSERT INTO memory_write(round_key, source, created_at) VALUES ('r1','user',1.0)"
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


def _fts_rows(conn: sqlite3.Connection) -> tuple[tuple[int, str], ...]:
    """`memory_event_fts` 的全部行（rowid + 索引文本），按 rowid 排。"""
    return tuple(
        (int(row["rowid"]), row["raw_text"])
        for row in conn.execute("SELECT rowid, raw_text FROM memory_event_fts ORDER BY rowid")
    )


def _all_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """升级前后必须逐行不变的每张表：既有 10 张 + `semantic_fts` + v4 的三张。

    `memory_event_fts` **不在里面**——它正是 v5 要新增的东西（行数当然会从 0 变多）。
    """
    tables = tuple(_row_counts(conn)) + (
        "semantic_fts",
        "memory_event",
        "memory_state",
        "memory_event_archive",
    )
    return {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in tables
    }


# 「升级前后逐行不变」的表名单：既有 10 张 + `semantic_fts` + v4 的三张。
# 真库副本走 `_counts_if_present`（来源版本由外部决定，只数库里真有的表）；
# 合成夹具走 `_all_row_counts`（名字写死没问题：那些表一定是它自己建的）。
PRESERVED_TABLES = (
    "semantic_fts",
    "memory_event",
    "memory_state",
    "memory_event_archive",
)


def _counts_if_present(conn: sqlite3.Connection) -> dict[str, int]:
    """只数**库里真有**的那些表（真库副本的来源版本不由测试决定）。

    写死名单去 `count(*)` 在 v0 库上会直接报「no such table」——那与迁移对不对
    无关，只是夹具在假设来源版本。
    """
    present = _names(conn, "table")
    tables = tuple(_row_counts(conn)) + PRESERVED_TABLES
    return {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in tables
        if table in present
    }


def _event_match(conn: sqlite3.Connection, query: str) -> tuple[int, ...]:
    """按**生产口径**（`store._match_query`：逐字规范化 + OR）查事件表，返回 rowid。

    直接给 FTS5 喂中文原句是查不到的（索引侧已经把中文字逐字拆开了），所以这里
    复用生产的那一个函数，不另写一套分词——两套口径才是真正的坑。
    """
    match = store._match_query(query)
    if not match:
        return ()
    return tuple(
        int(row[0])
        for row in conn.execute(
            "SELECT rowid FROM memory_event_fts WHERE memory_event_fts MATCH ?"
            " ORDER BY rowid",
            (match,),
        )
    )


# ---- 验收 1：新表就位、版本号 5、列口径与不建触发器 ---------------------------


def test_空库升级到v5_memory_event_fts就位(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    assert SCHEMA_VERSION == 5
    assert _user_version(conn) == 5
    assert "memory_event_fts" in _names(conn, "table")
    # 列口径：只索引 `raw_text`（理由写在 store.py 的 MEMORY_EVENT_FTS_COLUMNS 上方）。
    assert MEMORY_EVENT_FTS_COLUMNS == ("raw_text",)
    assert _columns(conn, "memory_event_fts") == MEMORY_EVENT_FTS_COLUMNS
    # 落库后的 DDL 逐字冻结。
    assert _ddl_fingerprint(conn)["memory_event_fts"] == FROZEN_MEMORY_EVENT_FTS_SQL
    # 应用层显式写索引：这条链上一个触发器都没有（触发器拿不到中文分词）。
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
    ).fetchone()[0] == 0
    conn.close()


def test_版本号严格递增且最新一步是v5() -> None:
    versions = [version for version, _, _ in store._MIGRATIONS]
    assert versions == sorted(versions) == [1, 2, 3, 4, 5]
    assert SCHEMA_VERSION == store._MIGRATIONS[-1][0] == 5
    assert store._MIGRATIONS[-1][1] == "建 memory_event_fts 并回填事件"
    assert store._MIGRATIONS[-1][2] is store._migrate_to_v5
    # v1..v4 那四步的号与函数没被顺手改过（v5 是**追加**，不是改写）。
    assert [entry[1] for entry in store._MIGRATIONS[:4]] == [
        "建基础表并给 episode 加列加索引",
        "重建 memory_fts 为四列并回填",
        "建 semantic 与四张账本表",
        "建状态与时序六表",
    ]
    assert store._MIGRATIONS[3][2] is store._migrate_to_v4


# ---- 验收 2：既有表逐字/逐行不变（四种钉法 + 真库副本）------------------------


def test_既有表逐字不变_冻结建表文本与列清单(tmp_path: Path) -> None:
    conn = _v4_db_with_data(tmp_path)
    before_counts = _all_row_counts(conn)

    init_memory_schema(conn)

    stored = _ddl_fingerprint(conn)
    # ① 冻结建表文本（SQLite 会去掉 `IF NOT EXISTS` 与结尾分号）。
    assert stored["memory"] == FROZEN_MEMORY_TABLE_SQL
    assert stored["episode"] == FROZEN_EPISODE_TABLE_SQL
    assert stored["memory_fts"] == FROZEN_MEMORY_FTS_SQL
    assert stored["semantic_fts"] == FROZEN_SEMANTIC_FTS_SQL
    # ② 列清单元组：既有 11 张表（含四张账本表）逐列不变。
    for table, columns in FROZEN_EXISTING_COLUMNS.items():
        assert _columns(conn, table) == columns, table
    # ② 的另一半：v4 六表走 `FIELD_SPECS`（DDL 与常量两份表示，任一处改坏都红）。
    for table, want in FROZEN_V4_COLUMNS.items():
        assert _table_info(conn, table) == want, table
    # 逐行不变：既有表的行数一行都没被 v5 动过（事件与两张 FTS 表的行数也在里面）。
    assert _all_row_counts(conn) == before_counts
    assert conn.execute("SELECT count(*) FROM memory_event").fetchone()[0] == len(
        OLD_EVENT_ROWS
    )
    conn.close()


def test_既有表逐字不变_整库DDL指纹哈希(tmp_path: Path) -> None:
    """③ 的一半 + ④ 的基准：**只跑到 v4** 的整库指纹等于加 v5 之前的读数。

    取值来自 `.tmp/probe_pre_v5.py`（**加 v5 之前**在全新 v4 库上抓的），
    并与同一份代码里 `MEMORY_SCHEMA` / `SEMANTIC_SCHEMA` 的既有冻结哈希对得上。
    列顺序、类型、表内注释、索引名任何一个字节变了都会红。
    """
    conn = _v4_db(tmp_path)
    fingerprint = _existing_fingerprint(conn)

    assert set(fingerprint) == (
        set(FROZEN_EXISTING_COLUMNS) | set(EXISTING_INDEXES) | set(V4_TABLES) | set(V4_INDEXES)
    )
    assert (
        hashlib.sha256(_canonical(fingerprint).encode("utf-8")).hexdigest()
        == FROZEN_V4_SCHEMA_SHA256
    )
    conn.close()


def test_既有表逐字不变_三个schema常量没被动过() -> None:
    """③：三个 schema 常量的**整段文本**逐字节冻结（注释行也算，库里的指纹抓不到）。"""
    for text, digest in (
        (MEMORY_SCHEMA, FROZEN_MEMORY_SCHEMA_SHA256),
        (SEMANTIC_SCHEMA, FROZEN_SEMANTIC_SCHEMA_SHA256),
        (TIMELINE_SCHEMA, FROZEN_TIMELINE_SCHEMA_SHA256),
    ):
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == digest


def test_既有表逐字不变_schema片段(tmp_path: Path) -> None:
    """③ 常量本身没被人顺手改（P0 在 `MEMORY_SCHEMA` 上用的切法，语义照搬）。"""
    block = MEMORY_SCHEMA.split("CREATE TABLE IF NOT EXISTS memory", 1)[1]
    block = "CREATE TABLE memory" + block.split("\n);", 1)[0] + "\n)"
    assert block == FROZEN_MEMORY_TABLE_SQL

    stored = _ddl_fingerprint(_v4_db_with_data(tmp_path))["memory_event"]
    block = TIMELINE_SCHEMA.split("CREATE TABLE IF NOT EXISTS memory_event ", 1)[1]
    block = "CREATE TABLE memory_event " + block.split("\n);", 1)[0] + "\n)"
    assert block == stored


def test_既有对象逐字不变_升级前后DDL指纹(tmp_path: Path) -> None:
    """④：v4→v5 既有对象全部逐字一致，新增的**恰好**是 `memory_event_fts` 与其影子表。"""
    conn = _v4_db_with_data(tmp_path)
    before = _ddl_fingerprint(conn)
    before_counts = _all_row_counts(conn)

    init_memory_schema(conn)

    after = _ddl_fingerprint(conn)
    assert {name: after[name] for name in before} == before
    new_names = set(after) - set(before)
    # FTS5 的影子表（`memory_event_fts_data` / `_idx` / `_content` / `_docsize` /
    # `_config`）是 SQLite 的内部实现，随 SQLite 版本走，不是"我们写的结构"：
    # 它们与 `memory_event_fts` 一起出现是必然的，此外不许有任何一个新对象。
    assert all(name.startswith("memory_event_fts") for name in new_names), new_names
    assert "memory_event_fts" in new_names
    assert new_names - {"memory_event_fts"} == {
        name for name in new_names if name.startswith("memory_event_fts_")
    }
    # 逐行不变（含两张既有 FTS 表与 v4 的三张表的行数）。
    assert _all_row_counts(conn) == before_counts
    conn.close()


@pytest.mark.skipif(
    not REAL_DB.exists(),
    reason=f"真实记忆库不在（这不是通过，是测不了）：{REAL_DB}",
)
def test_真库副本迁移幂等且版本单调不回退(tmp_path: Path) -> None:
    """真库副本：**与来源版本无关**的性质（原先钉的是「它恰好是 v0」）。

    为什么改（本条是另一条稳定红）：断言 `_user_version(真库副本) == 0` 把
    「用户工作区里的库还没迁过」当成了夹具性质；而任何在自己工作区跑起来的
    `spool` 会话都会自动迁移，现在那个库是 5 → 必红。**错在测试**（耦合了
    由外部决定的真实路径状态），不是库错了。绝不为让测试过而去改用户的库。

    现在钉住的性质与来源版本无关，而且都是**真库才能给的证据**：
    ① 只读复制（原库 sha256 不变）；② 既有对象逐行不变（事件一行都不丢）；
    ③ 版本单调不回退；④ 幂等（连跑两次指纹逐位相同）；
    ⑤ v5 这一步的对象就位、且回填与既有 `memory_event` 行数一对一。

    「真库 v0 → v4 的指纹等于全新 v4 库」这条强证据需要 v0 来源，改由合成夹具
    承担：`test_既有表逐字不变_整库DDL指纹哈希`（全新 v4 库 → 冻结哈希）。
    """
    source_digest = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    copy_b = tmp_path / "real-migrated.db"
    shutil.copy2(REAL_DB, copy_b)

    conn = open_db(copy_b)
    source_version = _user_version(conn)
    before = _ddl_fingerprint(conn)
    before_counts = _counts_if_present(conn)

    init_memory_schema(conn)

    after = _ddl_fingerprint(conn)
    after_counts = _counts_if_present(conn)

    # ① 原库一个字节都没写。
    assert hashlib.sha256(REAL_DB.read_bytes()).hexdigest() == source_digest
    # ② 既有对象逐行不变（含 memory_event：升级不许丢事件）。
    assert set(after) >= set(before)
    assert {name: after[name] for name in before} == before
    assert after_counts == before_counts
    assert (
        conn.execute("SELECT count(*) FROM memory_event").fetchone()[0]
        == before_counts["memory_event"]
    )
    # ③ 版本单调不回退，且到得了 max(来源版本, SCHEMA_VERSION)。
    assert _user_version(conn) == max(source_version, SCHEMA_VERSION)
    assert _user_version(conn) >= source_version
    # ⑤ v5 这一步的对象就位；回填与既有事件行数一对一（与来源版本无关）。
    assert "memory_event_fts" in _names(conn, "table")
    assert (
        conn.execute("SELECT count(*) FROM memory_event_fts").fetchone()[0]
        == conn.execute("SELECT count(*) FROM memory_event").fetchone()[0]
    )
    added = set(after) - set(before)
    if source_version >= SCHEMA_VERSION:
        assert added == set(), "库已经是最新时，这次迁移必须是空操作"
    else:
        assert all(name.startswith("memory_event_fts") for name in added), added

    # ④ 幂等：连跑两次，DDL 指纹、行数、版本号、索引行逐位相同。
    first = (
        after,
        after_counts,
        _user_version(conn),
        _fts_rows(conn) if "memory_event_fts" in _names(conn, "table") else (),
    )
    init_memory_schema(conn)
    init_memory_schema(conn)
    assert (
        _ddl_fingerprint(conn),
        _counts_if_present(conn),
        _user_version(conn),
        _fts_rows(conn),
    ) == first
    conn.close()


# ---- 验收 3：回填（v5 之前写进去的事件，升级后可检索）------------------------


def test_回填_升级前写入的事件全部可检索且不重复插(tmp_path: Path) -> None:
    conn = _v4_db_with_data(tmp_path)
    # v5 之前：表里有两条事件，索引还不存在（这正是旧代码的现场）。
    assert "memory_event_fts" not in _names(conn, "table")
    assert conn.execute("SELECT count(*) FROM memory_event").fetchone()[0] == 2

    init_memory_schema(conn)

    # 全部回填：索引行数一对一（不是"只补增量"、也不是重复插）。
    assert len(_fts_rows(conn)) == 2
    assert tuple(rowid for rowid, _ in _fts_rows(conn)) == (1, 2)
    # 用事件文本里的词**真查得到**（走生产口径的分词）。
    assert _event_match(conn, "现在还剩多少") == (1,)
    assert _event_match(conn, "花") == (2,)
    assert _event_match(conn, "身上有") == (1,)
    # 索引侧是逐字拆过的中文（`memory_event_fts` 与 `memory_fts` 同一口径：
    # 中文字逐字用空格分开，默认分词器才切得出可检索单元）。
    assert _fts_rows(conn)[0][1] == normalize_for_fts("现在身上有 100 块")
    assert normalize_for_fts("现在身上有 100 块").startswith(" 现  在  身")
    # 库里没出现的事件词查不到（不做"全命中"的假象；OR 口径下要选逐字都不重合的词）。
    assert _event_match(conn, "悟空") == ()

    # **重复跑 v5 不重复插**：再跑两次，行数与内容逐字不变。
    first = _fts_rows(conn)
    for _ in range(2):
        store._apply_step(conn, 5, "建 memory_event_fts 并回填事件", store._migrate_to_v5)
    assert _fts_rows(conn) == first
    assert conn.execute("SELECT count(*) FROM memory_event_fts").fetchone()[0] == 2
    conn.close()


# ---- 验收 4：幂等 / 前向兼容 / 失败给人话 ------------------------------------


def test_幂等_连跑三次表索引版本号与行数都不变(tmp_path: Path) -> None:
    conn = _v4_db_with_data(tmp_path)
    init_memory_schema(conn)
    first = (
        sorted(_names(conn, "table")),
        sorted(_names(conn, "index")),
        _ddl_fingerprint(conn),
        _user_version(conn),
        _row_counts(conn),
        _fts_rows(conn),
    )

    init_memory_schema(conn)
    init_memory_schema(conn)

    assert (
        sorted(_names(conn, "table")),
        sorted(_names(conn, "index")),
        _ddl_fingerprint(conn),
        _user_version(conn),
        _row_counts(conn),
        _fts_rows(conn),
    ) == first
    conn.close()


def test_版本被人为退回4时v5重跑也不重复插(tmp_path: Path) -> None:
    """模拟「v5 建完表但版本号没记上」（`_apply_step` 的失败语义）→ 重跑不报错、不重复插。"""
    conn = _v4_db_with_data(tmp_path)
    init_memory_schema(conn)
    before = _fts_rows(conn)
    conn.execute("PRAGMA user_version = 4")
    conn.commit()

    init_memory_schema(conn)

    assert _user_version(conn) == 5
    assert _fts_rows(conn) == before
    conn.close()


def test_前向兼容_库比代码新时不动库也不报错(tmp_path: Path, capsys) -> None:
    conn = _v4_db_with_data(tmp_path)
    init_memory_schema(conn)
    conn.execute("DROP TABLE memory_event_fts")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()

    init_memory_schema(conn)

    assert "memory_event_fts" not in _names(conn, "table")  # 一步都没跑
    assert _user_version(conn) == SCHEMA_VERSION + 1  # 版本号没被写回低值
    out = capsys.readouterr().out
    assert "高于" in out and "跳过升级" in out
    conn.close()


def test_升级失败给的是人话且v5不记版本号(tmp_path: Path, monkeypatch) -> None:
    conn = _v4_db_with_data(tmp_path)

    def boom(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("模拟的磁盘错误")

    steps = store._MIGRATIONS[:4] + ((5, "建 memory_event_fts 并回填事件", boom),)
    monkeypatch.setattr(store, "_MIGRATIONS", steps)

    with pytest.raises(MemorySchemaError) as excinfo:
        init_memory_schema(conn)

    message = str(excinfo.value)
    assert "记忆库结构升级失败" in message
    assert "第 5 步" in message and "建 memory_event_fts 并回填事件" in message
    assert "模拟的磁盘错误" in message
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    # 失败的那一步**不记版本号**：库还停在 4，新表没有，旧数据一条不少。
    assert _user_version(conn) == 4
    assert "memory_event_fts" not in _names(conn, "table")
    assert conn.execute("SELECT count(*) FROM memory_event").fetchone()[0] == 2
    assert _row_counts(conn)["memory"] == 1
    # 修掉原因后重跑就补上（这一步是幂等的），并且旧事件被回填。
    monkeypatch.undo()
    init_memory_schema(conn)
    assert _user_version(conn) == 5
    assert _event_match(conn, "现在还剩多少") == (1,)
    conn.close()
