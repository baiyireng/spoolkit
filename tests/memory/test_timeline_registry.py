"""T9 的注册表一半：键注册表 / 别名映射 / 冲突拒绝合并（设计稿 §15.2 + 计划 Task 9）。

四组证据：

1. **注册表决定归约器**：§15.3 的对照表逐条（`monetary/count/duration`→`sum_delta`、
   `text/enum/status`→`latest`、`tags/files`→`set`、`best_score`→`max`），
   显式登记的 `reducer` 优先，表外的 `type` 与"没有 type"同路；
2. **未注册键不许默认套 `sum_delta`**：`reducer='none'` + `reason='no_reducer'` +
   `value` 留空，而且它**算不出 130**（哪怕链上就是 `set 100 / delta −20 / +50`）；
3. **别名两个命名空间**（实体表指实体、属性表指属性），归一化在写入侧做一次；
4. **冲突拒绝合并**（`test_别名冲突拒绝合并`）：同一个 alias 想指另一个 canonical →
   拒绝 + `memory_audit(action='alias_conflict')`（`before` 旧、`after` 被拒的 →
   **两条都留**）+ 新写入 `needs_review`；只有 `move_alias(force=True)` 能改且必写 audit。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from spoolkit.memory.timeline import db as tdb
from spoolkit.memory.timeline.clock import FakeClock, epoch_at
from spoolkit.memory.timeline.db import fetch_current_state, open_timeline
from spoolkit.memory.timeline.models import ATTRIBUTE_TYPES, REDUCERS
from spoolkit.memory.timeline.reduce import EventDraft, append_event
from spoolkit.memory.timeline.registry import (
    REDUCER_BY_TYPE,
    add_alias,
    move_alias,
    normalize_segment,
    register_attribute,
    resolve_dotted,
    resolve_key,
    split_key,
)

CLOCK = FakeClock(epoch_at(2026, 10, 3, 10, 0), tz_offset_min=480)


def _db(tmp_path: Path) -> sqlite3.Connection:
    return open_timeline(tmp_path / "memory.db")


def _draft(entity: str, attribute: str, op: str, value: str, at_abs: float) -> EventDraft:
    return EventDraft(
        entity=entity,
        attribute=attribute,
        op=op,
        value=value,
        raw_text=f"{op} {value}",
        at_abs=at_abs,
        unit="CNY",
        at_source="explicit",
        time_precision="exact",
        speaker="user",
    )


def test_对照表与常量对得上() -> None:
    """`REDUCER_BY_TYPE` 的键必须都在模型的 `ATTRIBUTE_TYPES` 里，值都在 `REDUCERS` 里。"""
    assert set(REDUCER_BY_TYPE) <= set(ATTRIBUTE_TYPES)
    assert set(REDUCER_BY_TYPE.values()) <= set(REDUCERS)
    # §15.3 表：这三类走 sum_delta；text/enum/status 走 latest；tags/files 是并集。
    assert {REDUCER_BY_TYPE[name] for name in ("monetary", "count", "duration")} == {
        "sum_delta"
    }
    assert {REDUCER_BY_TYPE[name] for name in ("text", "enum", "status")} == {"latest"}
    assert {REDUCER_BY_TYPE[name] for name in ("tags", "files")} == {"set"}
    assert REDUCER_BY_TYPE["best_score"] == "max"


@pytest.mark.parametrize(
    "type_name,reducer",
    tuple(sorted(REDUCER_BY_TYPE.items())),
)
def test_注册表决定用哪个归约器(type_name: str, reducer: str, tmp_path: Path) -> None:
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type=type_name, clock=CLOCK)

    key = resolve_key(conn, "wallet", "balance")
    assert (key.type, key.reducer, key.registered, key.reason) == (
        type_name,
        reducer,
        True,
        "",
    )
    assert key.dotted == "wallet.balance"
    conn.close()


def test_显式登记的reducer优先于type(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    # text 默认 latest，但登记表里写了 min 就听登记表的（模型给的候选不算数，§15.3）。
    register_attribute(conn, "exam", "score", type="text", reducer="min", clock=CLOCK)
    assert resolve_key(conn, "exam", "score").reducer == "min"
    # 表外的 type 与"没有 type"同路：不推，也不报错（注册表是开放的）。
    register_attribute(conn, "mood", "level", type="没见过这种类型", clock=CLOCK)
    unknown_type = resolve_key(conn, "mood", "level")
    assert (unknown_type.reducer, unknown_type.reason) == ("none", "no_reducer")
    register_attribute(conn, "blank", "value", clock=CLOCK)
    blank = resolve_key(conn, "blank", "value")
    assert (blank.reducer, blank.registered, blank.reason) == ("none", True, "no_reducer")
    conn.close()


def test_未注册键不许默认套sum_delta(tmp_path: Path) -> None:
    """§15.6 第 5 条：注册表里没有这个键 → `no_reducer` + `value` 留空。"""
    conn = _db(tmp_path)
    key = resolve_key(conn, "wallet", "balance")
    assert (key.reducer, key.registered, key.reason) == ("none", False, "no_reducer")

    # 链上就是 §15.3 的余额例子，但**没登记** → 不许算出 130。
    append_event(conn, _draft("wallet", "balance", "set", "100", 1.0), clock=CLOCK)
    append_event(conn, _draft("wallet", "balance", "delta", "-20", 1.5), clock=CLOCK)
    append_event(conn, _draft("wallet", "balance", "delta", "50", 2.0), clock=CLOCK)

    row = fetch_current_state(conn, "wallet", "balance")
    assert row is not None  # 行要在：不确定本身要能被读到（§15.6 的出口 1）
    assert row["value"] is None
    assert (row["status"], row["reason"], row["reducer"]) == (
        "needs_review",
        "no_reducer",
        "none",
    )
    assert row["value"] != "130"
    conn.close()


def test_别名映射两个命名空间(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    assert add_alias(conn, "余额", "wallet", kind="entity", clock=CLOCK).ok
    assert add_alias(conn, "余额", "balance", kind="attribute", clock=CLOCK).ok
    assert add_alias(conn, "Wallet ", "wallet.balance", kind="entity", clock=CLOCK).ok

    key = resolve_dotted(conn, "余额.余额")
    assert (key.entity, key.attribute, key.reducer) == ("wallet", "balance", "sum_delta")
    assert (key.entity_alias, key.attribute_alias) == ("余额", "余额")
    # 两个命名空间互不干扰：实体表里的「余额」指 wallet，属性表里的指 balance。
    assert resolve_key(conn, "余额", "balance").entity == "wallet"
    assert resolve_key(conn, "wallet", "余额").attribute == "balance"
    # 归一化只在写入侧做一次（唯一入口），带空格/大写的别名与 canonical 都归一。
    assert resolve_key(conn, "Wallet ", " Balance").dotted == "wallet.balance"
    assert normalize_segment("  分期 付款 ") == "分期_付款"

    # 别名写入的事件落成 canonical 键：库里没有别名那一份。
    append_event(conn, _draft("余额", "余额", "set", "100", 1.0), clock=CLOCK)
    assert len(tdb.fetch_events(conn, "wallet", "balance")) == 1
    assert tdb.fetch_events(conn, "余额", "余额") == ()
    assert fetch_current_state(conn, "wallet", "balance")["value"] == "100"
    conn.close()


def test_键最多两段() -> None:
    assert split_key("wallet.balance") == ("wallet", "balance")
    with pytest.raises(ValueError):
        split_key("a.b.c")
    with pytest.raises(ValueError):
        split_key("wallet")
    with pytest.raises(ValueError):
        normalize_segment("  ")
    with pytest.raises(ValueError):
        normalize_segment("wallet.balance")


def test_别名冲突拒绝合并(tmp_path: Path) -> None:
    """§15.2 第 3 行：拒绝合并 + 两条都留 + 新写入 `needs_review`；只有 force 能改。"""
    conn = _db(tmp_path)
    assert add_alias(conn, "余额", "wallet", kind="entity", clock=CLOCK).ok

    # 同一个 alias 想指**另一个** canonical：拒绝，不许静默挑一个。
    refused = add_alias(conn, "余额", "cash", kind="entity", clock=CLOCK)
    assert refused.ok is False
    assert refused.conflict is True
    assert refused.status == "needs_review"  # 新写入该带的标记
    assert refused.existing == "wallet"
    assert refused.canonical == "wallet"  # 生效的仍是原来那个
    assert "拒绝" in refused.reason

    # 「两条都留」：原行一个字节没动，被拒的那个 canonical 留在 audit 里。
    rows = tdb.list_aliases(conn, "entity")
    assert len(rows) == 1
    assert rows[0]["canonical"] == "wallet"
    assert tdb.get_alias(conn, "entity", "cash") is None
    conflicts = tdb.list_audit(conn, action="alias_conflict")
    assert len(conflicts) == 1
    assert (conflicts[0]["before"], conflicts[0]["after"]) == ("wallet", "cash")
    assert conflicts[0]["target"] == "entity_alias:余额"
    # 拒绝之后解析仍然走原来那条（不会被"新写入"悄悄改掉）。
    assert resolve_key(conn, "余额", "balance").entity == "wallet"

    # `move_alias` 不给 force 走同一条拒绝路径；给了 force 才改，并且**必写 audit**。
    assert move_alias(conn, "余额", "cash", kind="entity", clock=CLOCK).ok is False
    assert len(tdb.list_audit(conn, action="alias_conflict")) == 2
    forced = move_alias(conn, "余额", "cash", kind="entity", clock=CLOCK, force=True)
    assert forced.ok is True and forced.forced is True
    assert forced.existing == "wallet"
    moves = tdb.list_audit(conn, action="alias_move")
    assert len(moves) == 1
    assert (moves[0]["before"], moves[0]["after"]) == ("wallet", "cash")
    assert len(tdb.list_aliases(conn, "entity")) == 1
    assert resolve_key(conn, "余额", "cash").entity == "cash"
    conn.close()


def test_别名幂等且不误判冲突(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    first = add_alias(conn, "钱", "wallet", kind="entity", clock=CLOCK)
    again = add_alias(conn, "钱", "wallet", kind="entity", clock=CLOCK)
    assert first.ok and again.ok and again.conflict is False
    assert "幂等" in again.reason or "同一个" in again.reason
    # canonical 用全键写法时取与命名空间对应的那一段（CLI 的 `alias add 钱 wallet.balance`）。
    assert add_alias(conn, "现金", "wallet.balance", kind="entity", clock=CLOCK).canonical == "wallet"
    assert add_alias(conn, "现金", "wallet.balance", kind="attribute", clock=CLOCK).canonical == "balance"
    conn.close()


def test_注册表冲突拒绝合并(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK).ok
    # 同参数重登记 = 幂等，不算冲突。
    same = register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    assert same.ok is True and same.conflict is False
    # 想换 type/reducer：拒绝，旧登记保留，两条都留在 audit 里。
    refused = register_attribute(
        conn, "wallet", "balance", type="text", reducer="latest", clock=CLOCK
    )
    assert refused.ok is False and refused.conflict is True
    # 旧登记原样保留（type=monetary、reducer 没写 → 按 type 推 sum_delta）。
    assert (refused.spec.type, refused.spec.reducer) == ("monetary", None)
    assert resolve_key(conn, "wallet", "balance").reducer == "sum_delta"
    rows = tdb.list_audit(conn, action="attr_conflict")
    assert len(rows) == 1
    assert rows[0]["before"] == "type=monetary reducer="
    assert rows[0]["after"] == "type=text reducer=latest"
    # 显式 force 才能改，且同样写 audit。
    forced = register_attribute(
        conn, "wallet", "balance", type="text", reducer="latest", clock=CLOCK, force=True
    )
    assert forced.ok is True and forced.forced is True
    assert resolve_key(conn, "wallet", "balance").reducer == "latest"
    assert len(tdb.list_audit(conn, action="attr_change")) == 1
    conn.close()


def test_非法归约器直接报错(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    with pytest.raises(ValueError):
        register_attribute(conn, "wallet", "balance", reducer="avg", clock=CLOCK)
    with pytest.raises(ValueError):
        add_alias(conn, "余额", "wallet", kind="表名", clock=CLOCK)
    conn.close()
