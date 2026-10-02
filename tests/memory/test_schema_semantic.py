"""schema v3：`semantic` / `semantic_fts` / 四张账本表（`user_version` 2→3）。

设计稿 §4.3 第 1、2、4、5、6、7 块 + §9.3（旧库怎么迁）+ 计划 Task 2。这里钉的
是六件事：

1. **六张表与全部索引就位**、`user_version=3`；
2. **`memory` 表 DDL 逐字不变**（决议 7 的老规矩）：沿用 P0 在
   `tests/memory/test_schema_version.py` 里的四种钉法——冻结建表文本、列清单
   元组、`MEMORY_SCHEMA` 片段、升级前后 DDL 指纹（真库副本那一份是 Task 2 的
   取证步骤，跑在仓外副本上，不进用例）；
3. **幂等**：连跑两次不报错、不重复建表；
4. **`idx_semantic_dedupe` 是部分唯一索引**：`WHERE status='active'`，
   正反两面都钉——`active` 时同 hash 撞唯一冲突、归档/取代之后同 hash 可再写；
5. **`semantic` 上没有触发器**（与 P0 在 `memory_fts` 上同一口径：触发器拿不到
   中文分词那个 Python 函数）；
6. 前向兼容与失败给人话（沿用 `MemorySchemaError` 的风格）。
"""

import sqlite3
from pathlib import Path

import pytest

from spoolkit.memory import store
from spoolkit.memory.gate import content_hash
from spoolkit.memory.semantic import (
    SEMANTIC_STATUSES,
    get_semantic,
    insert_semantic,
    list_semantic,
    search_semantic,
    touch_semantic,
)
from spoolkit.memory.store import (
    MEMORY_SCHEMA,
    SCHEMA_VERSION,
    MemorySchemaError,
    add_episode,
    add_memory,
    init_memory_schema,
)
from spoolkit.store.db import open_db
from tests.memory.test_schema_version import (
    FROZEN_MEMORY_COLUMNS,
    FROZEN_MEMORY_TABLE_SQL,
)

# v3 建出来的六个名字（设计稿 §4.3 第 1、2、4、5、6、7 块）。
V3_TABLES = (
    "semantic",
    "semantic_fts",
    "memory_link",
    "memory_write",
    "memory_audit",
    "memory_decision",
)
V3_INDEXES = (
    "idx_semantic_kind",
    "idx_semantic_scope",
    "idx_semantic_dedupe",
    "idx_link_src",
    "idx_link_dst",
    "idx_write_round",
)

# `semantic` 的列清单（§4.3 第 1 块逐字；顺序也算）。
SEMANTIC_COLUMNS = (
    "id",
    "kind",
    "scope",
    "scope_id",
    "text",
    "trigger",
    "steps",
    "source",
    "evidence",
    "confidence",
    "importance",
    "content_hash",
    "status",
    "superseded_by",
    "created_at",
    "last_used_at",
    "use_count",
    "applied_count",
    "success_count",
)

# 四张账本表的列清单（§4.3 第 4、5、6、7 块 + 偏差② 的 `embed_status`）。
LEDGER_COLUMNS = {
    "memory_link": (
        "id",
        "src_kind",
        "src_id",
        "dst_kind",
        "dst_id",
        "relation",
        "created_at",
    ),
    "memory_write": (
        "id",
        "round_key",
        "source",
        "written",
        "skipped",
        "detail",
        "created_at",
    ),
    "memory_audit": (
        "id",
        "at",
        "actor",
        "action",
        "target",
        "before",
        "after",
        "reason",
    ),
    "memory_decision": (
        "id",
        "at",
        "session_id",
        "task_id",
        "channel",
        "candidates",
        "chosen",
        "dropped",
        "embed_status",
    ),
}


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row["name"] for row in conn.execute(f"PRAGMA table_info({table})"))


def _names(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
        )
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


def _p0_db(tmp_path: Path) -> sqlite3.Connection:
    """一个**真的 P0 库**：跑过 v1 / v2 两步、`user_version=2`、有旧数据。

    不用「`executescript(MEMORY_SCHEMA)` + 手工把版本号设成 2」凑：那样
    `episode` 缺 P0 加的 5 列，就不是 P0 的现场了。这里直接让 P0 的那两步
    自己跑（`_apply_step` 是 P0 代码），拿到的结构与「P0 收尾时的库」一致。
    """
    conn = open_db(tmp_path / "memory.db")
    conn.executescript(MEMORY_SCHEMA)
    for version, name, apply in store._MIGRATIONS[:2]:
        store._apply_step(conn, version, name, apply)
    assert _user_version(conn) == 2
    return conn


# ---- 验收 1：六张表与索引就位、user_version=3 ---------------------------------


def test_空库升级到v3六张表与索引就位(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)

    assert SCHEMA_VERSION == 3
    assert _user_version(conn) == 3
    assert set(V3_TABLES) <= _names(conn, "table")
    assert set(V3_INDEXES) <= _names(conn, "index")
    # 列清单逐字照 §4.3（加列、改名、删列都会在这里红）。
    assert _columns(conn, "semantic") == SEMANTIC_COLUMNS
    for table, expected in LEDGER_COLUMNS.items():
        assert _columns(conn, table) == expected, table
    # `semantic_fts` 是三列，且**非 external content**（偏差①）。
    assert _columns(conn, "semantic_fts") == ("text", "trigger", "evidence")
    fts_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='semantic_fts'"
    ).fetchone()[0]
    assert "content=" not in fts_sql
    # 幂等口径：v3 只新增表，不碰旧表。
    assert _columns(conn, "memory") == FROZEN_MEMORY_COLUMNS
    conn.close()


def test_decision表从建表起就带embed_status(tmp_path: Path) -> None:
    """偏差②：§14.4 规矩 6 的出口①——P1 的形态是 `w_embed=0.0` 明确关闭。"""
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    default = conn.execute(
        "SELECT dflt_value FROM pragma_table_info('memory_decision')"
        " WHERE name = 'embed_status'"
    ).fetchone()[0]
    assert default == "'off:disabled'"
    conn.close()


def test_semantic_statuses_比设计稿多一个needs_review() -> None:
    """「口径裁决 5」：`status` 加取值而不是加列（§9.3 第 2 步要不静默丢弃）。"""
    assert SEMANTIC_STATUSES == ("active", "archived", "superseded", "needs_review")


# ---- 验收 2：旧库升级后 `memory` 一字不动 -------------------------------------


def test_旧库升级后memory表DDL逐字不变且行数不变(tmp_path: Path) -> None:
    conn = _p0_db(tmp_path)
    add_episode(conn, "s1", "查天气", "查天气：已完成", "success")
    add_memory(conn, kind="fact", text="测试命令是 pytest -q")
    conn.commit()
    memory_rows = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    episode_rows = conn.execute("SELECT count(*) FROM episode").fetchone()[0]
    before = _ddl_fingerprint(conn)["memory"]

    init_memory_schema(conn)

    assert _user_version(conn) == 3
    # ① 冻结建表文本（落库后的样子）。
    after_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory'"
    ).fetchone()[0]
    assert after_sql == FROZEN_MEMORY_TABLE_SQL
    # ② 列清单元组。
    assert _columns(conn, "memory") == FROZEN_MEMORY_COLUMNS
    # ③ `MEMORY_SCHEMA` 片段（常量本身也没被人顺手改）。
    block = MEMORY_SCHEMA.split("CREATE TABLE IF NOT EXISTS memory", 1)[1]
    block = "CREATE TABLE memory" + block.split("\n);", 1)[0] + "\n)"
    assert block == FROZEN_MEMORY_TABLE_SQL
    # ④ 升级前后的 DDL 指纹（真库副本那一份是 Task 2 的取证步骤，跑在仓外副本上）。
    assert _ddl_fingerprint(conn)["memory"] == before
    # 旧表与旧索引的名字都还在、行数一条不差（§9.3 第 3 步）。
    assert {"memory", "memory_fts", "episode"} <= _names(conn, "table")
    assert conn.execute("SELECT count(*) FROM memory").fetchone()[0] == memory_rows
    assert conn.execute("SELECT count(*) FROM episode").fetchone()[0] == episode_rows
    conn.close()


def test_旧库升级后旧路径照读(tmp_path: Path) -> None:
    """`memory` / `memory_fts` 保留不删：旧代码路径不会因为加了新表而炸。"""
    conn = _p0_db(tmp_path)
    add_memory(conn, kind="fact", text="测试命令是 pytest -q")
    init_memory_schema(conn)

    rows = conn.execute(
        "SELECT m.* FROM memory_fts f JOIN memory m ON m.id = f.rowid"
        " WHERE memory_fts MATCH ?",
        ('"pytest"',),
    ).fetchall()
    assert [row["text"] for row in rows] == ["测试命令是 pytest -q"]
    conn.close()


# ---- 验收 3：幂等 -------------------------------------------------------------


def test_幂等_连跑两次表索引与版本号都不变(tmp_path: Path) -> None:
    conn = _p0_db(tmp_path)
    add_memory(conn, kind="fact", text="测试命令是 pytest -q")
    init_memory_schema(conn)
    first = (
        sorted(_names(conn, "table")),
        sorted(_names(conn, "index")),
        _ddl_fingerprint(conn),
        _user_version(conn),
    )

    init_memory_schema(conn)  # 再跑一次：不重复建表、不报错
    init_memory_schema(conn)

    assert (
        sorted(_names(conn, "table")),
        sorted(_names(conn, "index")),
        _ddl_fingerprint(conn),
        _user_version(conn),
    ) == first
    assert conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 1
    conn.close()


def test_版本被人为退回时v3重跑也不重复建表(tmp_path: Path) -> None:
    """模拟「v3 建完表但版本号没记上」（`_apply_step` 的失败语义）→ 重跑不报错。"""
    conn = _p0_db(tmp_path)
    init_memory_schema(conn)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()

    init_memory_schema(conn)

    assert _user_version(conn) == 3
    assert _columns(conn, "semantic") == SEMANTIC_COLUMNS
    conn.close()


# ---- 验收 4：`idx_semantic_dedupe` 是部分唯一索引（正反两面） -----------------


def test_去重索引是带where的部分唯一索引(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_semantic_dedupe'"
    ).fetchone()[0]
    assert "UNIQUE INDEX" in sql
    assert "WHERE status = 'active'" in sql
    conn.close()


def test_active时同hash撞唯一冲突_归档之后可以再写(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    first = insert_semantic(conn, "fact", "测试命令是 pytest -q")

    # 反面①：同一个 hash 的 `active` 行被数据库拒绝。
    # （「测试命令是 PYTEST -Q。」归一化后与第一条逐字相同——指纹同、
    #   `content_hash` 唯一部分索引是**结构性去重**，不靠调用方记得检查。）
    with pytest.raises(sqlite3.IntegrityError):
        insert_semantic(conn, "fact", " 测试命令是 PYTEST -Q。")
    conn.rollback()

    # 正面②：`archived` 之后同一个 hash **可以再次写入**。
    conn.execute("UPDATE semantic SET status='archived' WHERE id = ?", (first.id,))
    conn.commit()
    second = insert_semantic(conn, "fact", "测试命令是 pytest -q")
    assert second.id != first.id
    assert second.content_hash == first.content_hash

    # 正面③：`superseded` 同理（取代链要能一直往下走，§5.3 ⑤）。
    conn.execute("UPDATE semantic SET status='superseded' WHERE id = ?", (second.id,))
    conn.commit()
    third = insert_semantic(conn, "fact", "测试命令是 pytest -q")
    assert third.id not in (first.id, second.id)
    assert [row.status for row in list_semantic(conn)] == [
        "archived",
        "superseded",
        "active",
    ]
    conn.close()


# ---- 验收 5：`semantic` 上不加触发器 -----------------------------------------


def test_semantic_fts_不用触发器(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    on_semantic = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='semantic'"
    ).fetchone()[0]
    assert on_semantic == 0
    # 与 P0 的 `test_memory_fts_是四列且不用触发器` 同一形态：整库一个触发器都没有。
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
    ).fetchone()[0] == 0
    conn.close()


# ---- 验收 7/6：失败给人话、前向兼容 -------------------------------------------


def test_升级失败给的是人话且不记版本号(tmp_path: Path, monkeypatch) -> None:
    conn = _p0_db(tmp_path)

    def boom(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("模拟的磁盘错误")

    steps = store._MIGRATIONS[:2] + ((3, "建 semantic 与四张账本表", boom),)
    monkeypatch.setattr(store, "_MIGRATIONS", steps)

    with pytest.raises(MemorySchemaError) as excinfo:
        init_memory_schema(conn)

    message = str(excinfo.value)
    assert "记忆库结构升级失败" in message
    assert "第 3 步" in message and "建 semantic 与四张账本表" in message
    assert "模拟的磁盘错误" in message
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    # 失败的那一步不落版本号；前两步已经记上，重跑不会重复加列。
    assert _user_version(conn) == 2
    assert "semantic" not in _names(conn, "table")
    conn.close()


def test_前向兼容_库比代码新时不动库也不报错(tmp_path: Path, capsys) -> None:
    conn = _p0_db(tmp_path)
    init_memory_schema(conn)
    conn.execute("DROP INDEX idx_semantic_kind")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()

    init_memory_schema(conn)

    assert "idx_semantic_kind" not in _names(conn, "index")  # 一步都没跑
    assert _user_version(conn) == SCHEMA_VERSION + 1  # 版本号没被写回低值
    out = capsys.readouterr().out
    assert "高于" in out and "跳过升级" in out
    conn.close()


# ---- semantic.py：写入口与最简检索 -------------------------------------------


def test_写入即建索引_三列都查得到(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    item = insert_semantic(
        conn,
        "fact",
        "测试命令是 pytest -q",
        trigger="pytest,测试",
        evidence="docs/cli.md:12",
    )

    assert item.content_hash == content_hash("测试命令是 pytest -q")
    # 索引三列（§4.3 第 2 块）：正文、触发词、证据都要能查（§14.4 规矩 2）。
    assert [row.id for row in search_semantic(conn, "测试命令")] == [item.id]
    assert [row.id for row in search_semantic(conn, "pytest")] == [item.id]
    assert [row.id for row in search_semantic(conn, "cli")] == [item.id]
    # `kind` / `scope` / `status` **不进 FTS**（按列过滤走 SQL 索引）。
    assert search_semantic(conn, "fact") == []
    # 查得到一条时索引行数 = 主表行数。
    assert conn.execute("SELECT count(*) FROM semantic_fts").fetchone()[0] == 1
    conn.close()


def test_检索默认条数从limits取(tmp_path: Path) -> None:
    """本任务对 `recall_limit`（§6.2 表）的接线处：默认 5 条、可覆盖。"""
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    for index in range(7):
        insert_semantic(conn, "fact", f"归档策略第 {index} 条")

    assert len(search_semantic(conn, "归档策略")) == 5
    assert len(search_semantic(conn, "归档策略", limit=3)) == 3
    assert len(search_semantic(conn, "归档策略", overrides={"recall_limit": 2})) == 2
    # 空查询不检索（与 `search_memories` 同一口径）。
    assert search_semantic(conn, "   ") == []
    conn.close()


def test_取用与读取_含被归档与被取代的行(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    item = insert_semantic(conn, "lesson", "解析自定义格式先上语法约束", trigger="正则")

    touch_semantic(conn, item.id)
    touch_semantic(conn, item.id)
    got = get_semantic(conn, item.id)
    assert got is not None and got.use_count == 2 and got.last_used_at is not None
    assert get_semantic(conn, 999) is None

    conn.execute("UPDATE semantic SET status='archived' WHERE id = ?", (item.id,))
    conn.commit()
    # 默认不过滤 status：问「这条去哪了」时要看得见被归档的行。
    assert [row.id for row in list_semantic(conn)] == [item.id]
    assert list_semantic(conn, status="active") == []
    assert list_semantic(conn, kind="fact") == []
    assert [row.id for row in list_semantic(conn, kind="lesson", status="archived")] == [
        item.id
    ]
    conn.close()
