"""时序六表的读写薄层：**只读 `store.py` 的建表入口，一个字节都不改 schema**。

设计稿 §15.7 说清了接法：六张表进**同一个库**（`<工作区>/.agent/memory.db`）、
**同一条迁移路径**（`store.py` 的 `_MIGRATIONS`，v4 已建好），本任务的改动面只有
`timeline/` 包内的代码——**DDL / `_MIGRATIONS` / FTS 一个字都不动**。

这里放的是"怎么读写那六张表"的机械部分（列清单、行 → dataclass、事务边界、audit），
归约与注册表的**判断**不在这里：

- 所有写入都经 `transaction()`，它**只在自己开启事务时提交**——这样"插事件 + 重算"
  或"关旧行 + 插新行 + 指新行"天然在**同一个事务**里（§15.4 的硬要求）；
- `memory_state` 的三种写入在 `reduce.py` 里集中（`state` 只能由归约器写，§15.1）；
- 时间一律由调用方传（`at` / `computed_at`），本模块**不取现在**（§15.5 硬约束 1）。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.timeline.models import AttributeSpec, Event, StateRow
from spoolkit.store.db import open_db

# `memory_event` 的插入列（顺序即 SQL 里的顺序；`id` 自增，不写）。
EVENT_COLUMNS: tuple[str, ...] = (
    "entity",
    "attribute",
    "op",
    "value",
    "unit",
    "at_abs",
    "tz_offset_min",
    "tz_name",
    "at_source",
    "time_precision",
    "source_ref",
    "raw_text",
    "speaker",
    "confidence",
    "content_hash",
    "status",
)

# `memory_state` 的插入列。`valid_to` / `superseded_by` 由取代链负责，不在这里。
STATE_COLUMNS: tuple[str, ...] = (
    "entity",
    "attribute",
    "value",
    "unit",
    "derived_from",
    "reducer",
    "valid_from",
    "valid_to",
    "superseded_by",
    "confidence",
    "status",
    "reason",
    "computed_at",
    "is_derived",
)

_ALIAS_TABLES = {"entity": "entity_alias", "attribute": "attribute_alias"}


def open_timeline(path: Path) -> sqlite3.Connection:
    """打开（必要时建库）并按 `store.py` 的迁移路径升到 v4。**只调用，不复制 DDL。**"""
    conn = open_db(path)
    init_memory_schema(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """事务边界：**只提交自己开的那个事务**。

    调用方已经开着事务（例如"插事件 → 重算"）时，这里退化成 no-op——
    否则内层提交会把外层拆成两半，而 §15.4 的"三步同一事务"正是靠这条保住的。
    """
    owns = not conn.in_transaction
    try:
        yield conn
        if owns:
            conn.commit()
    except BaseException:
        if owns:
            conn.rollback()
        raise


# ---- 事件 --------------------------------------------------------------------


def insert_event(conn: sqlite3.Connection, values: dict) -> int:
    """插一条事件（append-only，本表**没有** DELETE 路径，§15.4）。返回新 id。"""
    columns = ", ".join(EVENT_COLUMNS)
    placeholders = ", ".join("?" for _ in EVENT_COLUMNS)
    cursor = conn.execute(
        f"INSERT INTO memory_event({columns}) VALUES ({placeholders})",
        tuple(values[name] for name in EVENT_COLUMNS),
    )
    return int(cursor.lastrowid or 0)


def find_event_by_hash(
    conn: sqlite3.Connection, entity: str, attribute: str, content_hash: str
) -> sqlite3.Row | None:
    """按 `content_hash` 找同键已存在的同一条观察（幂等：命中只返回，不新增）。"""
    if not content_hash:
        return None
    return conn.execute(
        "SELECT * FROM memory_event WHERE entity = ? AND attribute = ?"
        " AND content_hash = ? ORDER BY id LIMIT 1",
        (entity, attribute, content_hash),
    ).fetchone()


def fetch_events(
    conn: sqlite3.Connection,
    entity: str,
    attribute: str,
    *,
    include_retracted: bool = True,
) -> tuple[Event, ...]:
    """取该键的全部事件，按 `id` 升序（重排口径在 `reduce.py`，这里只负责取全）。"""
    sql = "SELECT * FROM memory_event WHERE entity = ? AND attribute = ?"
    if not include_retracted:
        sql += " AND status = 'active'"
    sql += " ORDER BY id"
    return tuple(_to_event(row) for row in conn.execute(sql, (entity, attribute)))


def get_event(conn: sqlite3.Connection, event_id: int) -> Event | None:
    row = conn.execute("SELECT * FROM memory_event WHERE id = ?", (event_id,)).fetchone()
    return None if row is None else _to_event(row)


def set_event_status(
    conn: sqlite3.Connection, event_id: int, status: str
) -> None:
    """软撤销的唯一入口（`active` ↔ `retracted`）；**没有 DELETE**（§15.4）。"""
    conn.execute("UPDATE memory_event SET status = ? WHERE id = ?", (status, event_id))


def list_keys(conn: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    """库里出现过的全部 `(entity, attribute)`（全量重算用）。"""
    return tuple(
        (row["entity"], row["attribute"])
        for row in conn.execute(
            "SELECT DISTINCT entity, attribute FROM memory_event ORDER BY entity, attribute"
        )
    )


def _to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=int(row["id"]),
        entity=row["entity"],
        attribute=row["attribute"],
        op=row["op"],
        value=row["value"],
        tz_offset_min=int(row["tz_offset_min"]),
        at_source=row["at_source"],
        time_precision=row["time_precision"],
        source_ref=row["source_ref"],
        raw_text=row["raw_text"],
        content_hash=row["content_hash"],
        unit=row["unit"],
        at_abs=None if row["at_abs"] is None else float(row["at_abs"]),
        tz_name=row["tz_name"],
        speaker=row["speaker"],
        confidence=float(row["confidence"]),
        status=row["status"],
    )


# ---- 状态行 ------------------------------------------------------------------


def insert_state(conn: sqlite3.Connection, values: dict) -> int:
    """插一行 state（只被 `reduce.py` 调用：`state` 只能由归约器写，§15.1）。"""
    columns = ", ".join(STATE_COLUMNS)
    placeholders = ", ".join("?" for _ in STATE_COLUMNS)
    cursor = conn.execute(
        f"INSERT INTO memory_state({columns}) VALUES ({placeholders})",
        tuple(values[name] for name in STATE_COLUMNS),
    )
    return int(cursor.lastrowid or 0)


def fetch_current_state(
    conn: sqlite3.Connection, entity: str, attribute: str
) -> sqlite3.Row | None:
    """问法 ①「现在是多少」：部分唯一索引保证这行至多一条（§15.4）。"""
    return conn.execute(
        "SELECT * FROM memory_state WHERE entity = ? AND attribute = ?"
        " AND valid_to IS NULL ORDER BY id LIMIT 1",
        (entity, attribute),
    ).fetchone()


def fetch_state_at(
    conn: sqlite3.Connection, entity: str, attribute: str, at: float
) -> sqlite3.Row | None:
    """问法 ②「我当时说的」：半开区间 `[valid_from, valid_to)`（§15.4）。"""
    return conn.execute(
        "SELECT * FROM memory_state WHERE entity = ? AND attribute = ?"
        " AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
        " ORDER BY valid_from DESC LIMIT 1",
        (entity, attribute, float(at), float(at)),
    ).fetchone()


def fetch_state_rows(
    conn: sqlite3.Connection, entity: str, attribute: str
) -> tuple[sqlite3.Row, ...]:
    """该键的全部 state 行（含历史行），按 `valid_from` 升序。旧行**不删不改值**。"""
    return tuple(
        conn.execute(
            "SELECT * FROM memory_state WHERE entity = ? AND attribute = ?"
            " ORDER BY valid_from, id",
            (entity, attribute),
        )
    )


def count_current_rows(
    conn: sqlite3.Connection, entity: str, attribute: str
) -> int:
    """该键现在有几行 `valid_to IS NULL`——任何时刻都必须 ≤ 1（部分唯一索引）。"""
    return int(
        conn.execute(
            "SELECT count(*) FROM memory_state WHERE entity = ? AND attribute = ?"
            " AND valid_to IS NULL",
            (entity, attribute),
        ).fetchone()[0]
    )


def to_state_row(row: sqlite3.Row) -> StateRow:
    return StateRow(
        entity=row["entity"],
        attribute=row["attribute"],
        value=row["value"],
        valid_from=float(row["valid_from"]),
        computed_at=float(row["computed_at"]),
        unit=row["unit"],
        derived_from=row["derived_from"],
        reducer=row["reducer"],
        valid_to=None if row["valid_to"] is None else float(row["valid_to"]),
        superseded_by=(
            None if row["superseded_by"] is None else int(row["superseded_by"])
        ),
        confidence=float(row["confidence"]),
        status=row["status"],
        reason=row["reason"],
        is_derived=int(row["is_derived"]),
        id=int(row["id"]),
    )


def derived_ids(row: sqlite3.Row | None) -> tuple[int, ...]:
    """把 `derived_from`（JSON 数组）读回有序 event id；坏数据当空链。"""
    if row is None:
        return ()
    try:
        payload = json.loads(row["derived_from"] or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, list):
        return ()
    return tuple(int(item) for item in payload)


# ---- 注册表与别名 ------------------------------------------------------------


def get_attribute(
    conn: sqlite3.Connection, entity: str, attribute: str
) -> AttributeSpec | None:
    row = conn.execute(
        "SELECT * FROM entity_attribute WHERE entity = ? AND attribute = ?",
        (entity, attribute),
    ).fetchone()
    if row is None:
        return None
    return AttributeSpec(
        entity=row["entity"],
        attribute=row["attribute"],
        type=row["type"],
        reducer=row["reducer"],
    )


def upsert_attribute(
    conn: sqlite3.Connection,
    entity: str,
    attribute: str,
    type_: str | None,
    reducer: str | None,
) -> None:
    conn.execute(
        "INSERT INTO entity_attribute(entity, attribute, type, reducer)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(entity, attribute)"
        " DO UPDATE SET type = excluded.type, reducer = excluded.reducer",
        (entity, attribute, type_, reducer),
    )


def get_alias(conn: sqlite3.Connection, kind: str, alias: str) -> sqlite3.Row | None:
    table = _ALIAS_TABLES[kind]
    return conn.execute(
        f"SELECT * FROM {table} WHERE alias = ?", (alias,)
    ).fetchone()


def insert_alias(
    conn: sqlite3.Connection,
    kind: str,
    *,
    alias: str,
    canonical: str,
    lang: str,
    scope: str,
    created_at: float,
    source: str,
) -> None:
    table = _ALIAS_TABLES[kind]
    conn.execute(
        f"INSERT INTO {table}(alias, canonical, lang, scope, created_at, source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (alias, canonical, lang, scope, float(created_at), source),
    )


def update_alias(
    conn: sqlite3.Connection, kind: str, *, alias: str, canonical: str, source: str
) -> None:
    table = _ALIAS_TABLES[kind]
    conn.execute(
        f"UPDATE {table} SET canonical = ?, source = ? WHERE alias = ?",
        (canonical, source, alias),
    )


def list_aliases(conn: sqlite3.Connection, kind: str) -> tuple[sqlite3.Row, ...]:
    table = _ALIAS_TABLES[kind]
    return tuple(conn.execute(f"SELECT * FROM {table} ORDER BY alias"))


# ---- 审计（§15.6 的第二条出口）-----------------------------------------------


def audit(
    conn: sqlite3.Connection,
    *,
    at: float,
    actor: str,
    action: str,
    target: str,
    before: str = "",
    after: str = "",
    reason: str = "",
) -> None:
    """写一条 `memory_audit`：谁、何时、为什么判成 `conflict` / `needs_review`。

    这是 §15.6「三条出口」里的第二条，也是"别名冲突两条都留"里**新那条的落点**
    （《memory_audit》表既有，本任务只写它，不改它）。
    """
    conn.execute(
        "INSERT INTO memory_audit(at, actor, action, target, before, after, reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (float(at), actor, action, target, before, after, reason),
    )


def list_audit(
    conn: sqlite3.Connection, *, action: str | None = None
) -> tuple[sqlite3.Row, ...]:
    if action is None:
        return tuple(conn.execute("SELECT * FROM memory_audit ORDER BY id"))
    return tuple(
        conn.execute(
            "SELECT * FROM memory_audit WHERE action = ? ORDER BY id", (action,)
        )
    )
