"""T9 的归约一半：M4-T1..T4 + 取代链 + 冲突 + 金额不浮点（设计稿 §15.3/§15.4/§15.6）。

这是本轮**核心验收**的落点，逐条机械可判：

- **M4-T1**：`set 100 → delta −20 → delta +50 ⇒ 现在 130`，而且这个值**由事件链算出**
  （`reduce_events` 是纯函数：删掉 state 表重算还是 130；`timeline/` 不 import 任何
  模型/网络模块）。三种问法都对：「t=1.2」→ `100`、「第 1 天结束」→ `80`、「现在」→ `130`。
- **M4-T3**：取代链**不是删除**——旧行 `valid_to` 关在新行 `valid_from` 上、
  `superseded_by` 指向新行 id、旧行仍可查；取代三步**同一个事务**（用 `set_trace_callback`
  逐条 SQL 钉住：关旧行 → 插新行 → 指新行，中间没有 COMMIT）。
- **M4-T4**：两条互斥 `op='set'` → `status='conflict'` + `reason='same_instant'` +
  **两条都留** + 可读的冲突标记；默认 `state_auto_pick=0` 时**没有**任何自动选值的路径。
- 另三条：缺基准 `value` 留空、单位不一致拒绝相加、时间未知不假装在链首链尾；
  未登记键不套 `sum_delta`（那一条在 registry 用例里）。
- **金额不浮点**：`0.1 + 0.2 == "0.3"`，不是 `0.30000000000000004`。
"""

from __future__ import annotations

import ast
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import spoolkit.memory.timeline as timeline_pkg
from spoolkit.memory.timeline import db as tdb
from spoolkit.memory.timeline import reduce as reduce_module
from spoolkit.memory.timeline.clock import FakeClock, epoch_at
from spoolkit.memory.timeline.db import (
    fetch_current_state,
    fetch_events,
    fetch_state_at,
    fetch_state_rows,
    open_timeline,
)
from spoolkit.memory.timeline.reduce import (
    CONFIDENCE_UNKNOWN_TIME,
    EventDraft,
    append_event,
    draft_from_text,
    explain_key,
    recompute_all,
    recompute_key,
    reduce_events,
    retract_event,
    review_key,
    verify_key,
)
from spoolkit.memory.timeline.registry import register_attribute

TIMELINE_DIR = Path(timeline_pkg.__file__).resolve().parent

# §10.4 M4-T1 的三条事件（跨两天，+08:00）。
DAY1_SET = epoch_at(2026, 10, 2, 9, 12)
DAY1_SPEND = epoch_at(2026, 10, 2, 9, 13)
DAY2_EARN = epoch_at(2026, 10, 3, 10, 4)
NOW = epoch_at(2026, 10, 3, 10, 5)
CLOCK = FakeClock(NOW, tz_offset_min=480)

# `timeline/` 不许 import 的东西：模型与网络。值必须能由事件链算出，
# "不调用任何模型"这条是 M4-T1 ② 的机械判据（§10.4）。
FORBIDDEN_IMPORT_PREFIXES = (
    "openai",
    "anthropic",
    "httpx",
    "requests",
    "urllib",
    "socket",
    "spoolkit.agent",
    "spoolkit.llm",
    "spoolkit.provider",
)


def _db(tmp_path: Path) -> sqlite3.Connection:
    return open_timeline(tmp_path / "memory.db")


def _draft(
    op: str,
    value: str,
    at_abs: float | None,
    *,
    entity: str = "wallet",
    attribute: str = "balance",
    unit: str = "CNY",
    raw_text: str | None = None,
    speaker: str = "user",
    precision: str = "exact",
    source_ref: str = "session:s1#turn:1",
) -> EventDraft:
    return EventDraft(
        entity=entity,
        attribute=attribute,
        op=op,
        value=value,
        raw_text=raw_text or f"{op} {value}",
        at_abs=at_abs,
        unit=unit,
        at_source="explicit" if at_abs is not None else "unknown",
        time_precision=precision if at_abs is not None else "unknown",
        tz_offset_min=480,
        tz_name="Asia/Shanghai",
        source_ref=source_ref,
        speaker=speaker,
    )


def _balance(conn: sqlite3.Connection) -> None:
    """落 §10.4 M4-T1 的三条事件（走 T8 的 `parse_time` 拿时间，证明两半能拼上）。"""
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    drafts = (
        (
            "我身上有 100 块",
            "set",
            "100",
            "2026-10-02 09:12",
            "session:s1#turn:1",
        ),
        ("花了 20", "delta", "-20", "2026-10-02 09:13", "session:s1#turn:2"),
        ("我赚了 50", "delta", "50", "2026-10-03 10:04", "session:s1#turn:3"),
    )
    for raw_text, op, value, time_text, source_ref in drafts:
        append_event(
            conn,
            draft_from_text(
                raw_text,
                clock=CLOCK,
                entity="wallet",
                attribute="balance",
                op=op,
                value=value,
                unit="CNY",
                time_text=time_text,
                speaker="user",
                source_ref=source_ref,
                round_key=f"s1#{source_ref}",
            ),
            clock=CLOCK,
        )


# ---- M4-T1：130 必须由事件链算出 ---------------------------------------------


def test_M4_T1_余额130(tmp_path: Path) -> None:
    """M4-T1 ①③④：`set 100 / delta −20 / delta +50` ⇒ 当前值 130，证据链可追溯。"""
    conn = _db(tmp_path)
    _balance(conn)

    row = fetch_current_state(conn, "wallet", "balance")
    assert row["value"] == "130"
    assert (row["reducer"], row["unit"], row["is_derived"]) == ("sum_delta", "CNY", 1)
    assert (row["status"], row["reason"]) == ("active", "")
    assert row["valid_to"] is None
    assert json.loads(row["derived_from"]) == [1, 2, 3]
    # 130 就是这三个数算出来的（不是模型说的、也不是抄来的）。
    assert Decimal(row["value"]) == Decimal("100") + Decimal("-20") + Decimal("50")
    # 每一条证据都能回溯到原始那句（§15.1：raw_text 不许改写）。
    assert {event.id: event.raw_text for event in fetch_events(conn, "wallet", "balance")} == {
        1: "我身上有 100 块",
        2: "花了 20",
        3: "我赚了 50",
    }
    # 三种问法（§10.4 M4-T1 ④）：当时那一刻 / 第 1 天结束 / 现在。
    assert fetch_state_at(conn, "wallet", "balance", DAY1_SET)["value"] == "100"
    assert fetch_state_at(conn, "wallet", "balance", DAY1_SPEND)["value"] == "80"
    assert fetch_state_at(conn, "wallet", "balance", NOW)["value"] == "130"
    conn.close()


def test_M4_T1_三问用生效区间答(tmp_path: Path) -> None:
    """把时刻缩到 1.0 / 1.5 / 2.0：t=1.2 → 100、t=1.9 → 80、现在 → 130。"""
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "100", 1.0), clock=CLOCK)
    append_event(conn, _draft("delta", "-20", 1.5), clock=CLOCK)
    append_event(conn, _draft("delta", "50", 2.0), clock=CLOCK)

    assert fetch_state_at(conn, "wallet", "balance", 1.2)["value"] == "100"
    assert fetch_state_at(conn, "wallet", "balance", 1.9)["value"] == "80"
    assert fetch_state_at(conn, "wallet", "balance", 1.4)["value"] == "100"
    assert fetch_state_at(conn, "wallet", "balance", 0.5) is None  # 那时还没有值
    assert fetch_current_state(conn, "wallet", "balance")["value"] == "130"
    # 半开区间 [from, to)：边界那一刻属于**新**那一行。
    assert fetch_state_at(conn, "wallet", "balance", 1.5)["value"] == "80"
    assert fetch_state_at(conn, "wallet", "balance", 2.0)["value"] == "130"
    rows = fetch_state_rows(conn, "wallet", "balance")
    assert [(row["value"], row["valid_from"], row["valid_to"]) for row in rows] == [
        ("100", 1.0, 1.5),
        ("80", 1.5, 2.0),
        ("130", 2.0, None),
    ]
    conn.close()


def test_M4_T1_不调模型也能从事件链重算出130(tmp_path: Path) -> None:
    """M4-T1 ②：同一份 event 链在**不调用任何模型**的条件下重算出 130。"""
    conn = _db(tmp_path)
    _balance(conn)

    # ① 把缓存扔了，只从 memory_event 重算（state 不许存不能重算的东西，§15.3 硬规矩 1）。
    conn.execute("DELETE FROM memory_state")
    conn.commit()
    assert fetch_current_state(conn, "wallet", "balance") is None

    results = recompute_all(conn, clock=CLOCK)
    assert [result.reduction.value for result in results] == ["130"]
    assert fetch_current_state(conn, "wallet", "balance")["value"] == "130"

    # ② 纯函数：只有 event 进、值出，跑两次一模一样（确定性 = 可测性的前提）。
    events = fetch_events(conn, "wallet", "balance")
    first = reduce_events(events, "sum_delta")
    second = reduce_events(events, "sum_delta")
    assert first.value == second.value == "130"
    assert first.derived_from == (1, 2, 3)

    # ③ 源码级：`timeline/` 不 import 任何模型/网络模块（"全程不调模型"）。
    imported: list[str] = []
    for path in sorted(TIMELINE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
    for name in imported:
        assert not name.startswith(FORBIDDEN_IMPORT_PREFIXES), name
    conn.close()


# ---- M4-T3：取代链不是删除 ---------------------------------------------------


def test_M4_T3_取代链不是删除(tmp_path: Path) -> None:
    """M4-T3：旧行 `valid_to` 关在新行 `valid_from` 上、指向新行 id、**仍可查**。"""
    conn = _db(tmp_path)
    _balance(conn)

    rows = fetch_state_rows(conn, "wallet", "balance")
    assert len(rows) == 3  # 三次更新留三行历史，一行都没被删
    first, second, third = rows
    assert (first["value"], first["valid_to"], first["superseded_by"]) == (
        "100",
        DAY1_SPEND,
        second["id"],
    )
    assert (second["value"], second["valid_to"], second["superseded_by"]) == (
        "80",
        DAY2_EARN,
        third["id"],
    )
    assert (third["value"], third["valid_to"], third["superseded_by"]) == ("130", None, None)
    # 取代链单向可追（旧 → 新），而且是指向 memory_state(id) 的真外键。
    followed = conn.execute(
        "SELECT n.value FROM memory_state o JOIN memory_state n ON n.id = o.superseded_by"
        " WHERE o.id = ?",
        (first["id"],),
    ).fetchone()[0]
    assert followed == "80"
    # 旧行**没有被改值**：按 id 直接读得到当初那个 100。
    assert conn.execute(
        "SELECT value FROM memory_state WHERE id = ?", (first["id"],)
    ).fetchone()[0] == "100"
    # 事件一条不少（append-only）：取代只是关旧行 + 插新行。
    assert conn.execute("SELECT count(*) FROM memory_event").fetchone()[0] == 3
    # 任一时刻**至多一行** `valid_to IS NULL`（部分唯一索引挡着，第二行插不进来）。
    assert tdb.count_current_rows(conn, "wallet", "balance") == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memory_state(entity, attribute, value, valid_from, computed_at)"
            " VALUES ('wallet','balance','120', 999.0, 999.0)"
        )
    conn.rollback()
    conn.close()


def test_M4_T3_取代三步在同一事务(tmp_path: Path) -> None:
    """取代顺序是硬要求：关旧行 → 插新行 → 旧行指向新行，**中间没有 COMMIT**。

    用 `set_trace_callback` 把这一轮真正跑过的 SQL 逐条记下来判：把三步拆到两个事务里
    （或把顺序倒过来、或漏掉 `superseded_by`），这条就红。
    """
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "100", 1.0), clock=CLOCK)

    records: list[str] = []
    conn.set_trace_callback(records.append)
    append_event(conn, _draft("set", "130", 2.0), clock=CLOCK)  # 第二次 → 取代
    conn.set_trace_callback(None)
    statements = [item.strip() for item in records]

    close_at = _index_of(statements, "UPDATE memory_state SET valid_to")
    insert_at = _index_of(statements, "INSERT INTO memory_state")
    point_at = _index_of(statements, "UPDATE memory_state SET superseded_by")
    assert -1 < close_at < insert_at < point_at, statements
    # 三步之间一条 COMMIT / ROLLBACK 都不能有：有就说明被拆成两个事务了。
    between = [item.upper() for item in statements[close_at : point_at + 1]]
    assert "COMMIT" not in between
    assert "ROLLBACK" not in between
    assert "BEGIN" in [item.upper() for item in statements[:close_at]]
    assert [item.upper() for item in statements].count("COMMIT") == 1
    conn.close()


def _index_of(statements: list[str], prefix: str) -> int:
    return next(
        (index for index, item in enumerate(statements) if item.startswith(prefix)), -1
    )


def test_同刻两条事件只留一行当前值(tmp_path: Path) -> None:
    """同一刻的两条事件（"我身上有 100 块，花了 20"）：一行当前值，值就地刷新。"""
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "100", 5.0), clock=CLOCK)
    append_event(conn, _draft("delta", "-20", 5.0), clock=CLOCK)

    rows = fetch_state_rows(conn, "wallet", "balance")
    assert len(rows) == 1
    assert rows[0]["value"] == "80"
    assert rows[0]["valid_from"] == 5.0
    assert tdb.count_current_rows(conn, "wallet", "balance") == 1
    # 同刻重算也留痕（否则"值怎么变的"就没有第二条出口）。
    actions = {row["action"] for row in tdb.list_audit(conn)}
    assert "recompute_same_instant" in actions
    conn.close()


# ---- M4-T4：冲突不许静默选一个 -----------------------------------------------


def test_M4_T4_冲突不静默(tmp_path: Path) -> None:
    """M4-T4：两条互斥 `op='set'` → conflict + same_instant + **两条都留** + 可读标记。"""
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(
        conn, _draft("set", "100", 9.0, raw_text="我身上有 100 块"), clock=CLOCK
    )
    append_event(
        conn, _draft("set", "120", 9.0, raw_text="我身上有 120 块"), clock=CLOCK
    )

    row = fetch_current_state(conn, "wallet", "balance")
    assert (row["status"], row["reason"]) == ("conflict", "same_instant")
    assert row["value"] is None  # **不给一个猜的数**
    assert json.loads(row["derived_from"]) == [1, 2]
    # 两条 event 都在（一条都没被删、也没被改）。
    assert {event.value for event in fetch_events(conn, "wallet", "balance")} == {
        "100",
        "120",
    }
    # 可读的冲突标记：说得出"为什么判冲突"、参与判定的 event id、两条的值。
    explanation = explain_key(conn, "wallet", "balance")
    why = explanation.why()
    assert why["status"] == "conflict"
    assert why["conflict"]["events"] == [1, 2]
    assert why["conflict"]["values"] == {"1": "100", "2": "120"}
    assert "event:1" in why["conflict"]["reason"] and "event:2" in why["conflict"]["reason"]
    assert "互斥" in why["conflict"]["reason"]
    label = explanation.as_label()
    assert label.startswith("wallet.balance = ?（未定：2 条互斥事件")
    assert "event:1" in label and "event:2" in label
    rendered = explanation.render()
    assert "我身上有 100 块" in rendered and "我身上有 120 块" in rendered
    # 第二条出口也留痕（谁、何时、为什么判成 conflict）。
    conflict_audits = tdb.list_audit(conn)
    assert any(
        audit["target"].startswith("state:") and "互斥" in audit["reason"]
        for audit in conflict_audits
    )
    conn.close()


def test_M4_T4_默认没有自动选值的路径(tmp_path: Path) -> None:
    """`state_auto_pick` 默认 0：不开时**没有任何**自动选值；开了也要标明是自动选的。"""
    from spoolkit import limits

    assert limits.resolve("state_auto_pick", None)[0] == 0

    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "100", 9.0), clock=CLOCK)
    append_event(conn, _draft("set", "120", 9.0), clock=CLOCK)
    assert fetch_current_state(conn, "wallet", "balance")["value"] is None

    # 显式打开（测试专用）→ 按 speaker 优先级给建议，但 status 仍然是 conflict。
    result = recompute_key(conn, "wallet", "balance", clock=CLOCK, auto_pick=True)
    assert result.reduction.auto_picked == 2
    row = fetch_current_state(conn, "wallet", "balance")
    assert (row["value"], row["status"], row["reason"]) == ("120", "conflict", "same_instant")
    assert any("自动选" in audit["reason"] for audit in tdb.list_audit(conn))
    conn.close()


def test_冲突只在同精度窗口内判(tmp_path: Path) -> None:
    """§15.6 第 3 条的"同一精度窗口"：`day` 精度下同一天的两条互斥 set 也算冲突。

    值相同（"100" 与 "100.0"）不算互斥——冲突的判据是**值不同**。
    """
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "100", 10.0, precision="day"), clock=CLOCK)
    append_event(conn, _draft("set", "100.0", 20.0, precision="day"), clock=CLOCK)
    assert fetch_current_state(conn, "wallet", "balance")["value"] == "100"

    append_event(conn, _draft("set", "120", 30.0, precision="day"), clock=CLOCK)
    row = fetch_current_state(conn, "wallet", "balance")
    assert (row["status"], row["reason"], row["value"]) == ("conflict", "same_instant", None)
    conn.close()


# ---- 另三条：缺基准 / 单位不一致 / 时间未知 -----------------------------------


def test_缺基准时值留空(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("delta", "-20", 1.0), clock=CLOCK)
    append_event(conn, _draft("delta", "50", 2.0), clock=CLOCK)

    row = fetch_current_state(conn, "wallet", "balance")
    assert row["value"] is None  # **不给一个猜的数**
    assert (row["status"], row["reason"]) == ("needs_review", "missing_base")
    assert explain_key(conn, "wallet", "balance").why()["reason"] == "missing_base"
    # 缺基准是"链不完整"，不是"冲突"：两条 delta 都还在。
    assert [event.value for event in fetch_events(conn, "wallet", "balance")] == ["-20", "50"]
    conn.close()


def test_单位不一致拒绝相加(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "100", 1.0, unit="CNY"), clock=CLOCK)
    append_event(conn, _draft("delta", "-20", 2.0, unit="USD"), clock=CLOCK)

    row = fetch_current_state(conn, "wallet", "balance")
    assert row["value"] is None  # 20 元与 20 美元不许相加
    assert (row["status"], row["reason"], row["unit"]) == ("conflict", "unit_mismatch", "")
    conn.close()


def test_时间未知不假装在链首或链尾(tmp_path: Path) -> None:
    """§15.6 第 6 条：基准时间未知 → `time_unknown`；不影响结论的观察仍进证据链。"""
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "100", None, raw_text="前几天有 100"), clock=CLOCK)
    row = fetch_current_state(conn, "wallet", "balance")
    assert (row["value"], row["status"], row["reason"]) == (
        None,
        "needs_review",
        "time_unknown",
    )
    assert json.loads(row["derived_from"]) == [1]
    conn.close()

    # 时间未知的 `observe` 不改变值，所以**不影响结论**，但它仍在证据链里（风险 ③）。
    conn = _db(tmp_path / "second")
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "100", 1.0), clock=CLOCK)
    append_event(conn, _draft("delta", "-20", 2.0), clock=CLOCK)
    append_event(
        conn,
        _draft("observe", "80", None, raw_text="前几天看到还剩 80"),
        clock=CLOCK,
    )
    row = fetch_current_state(conn, "wallet", "balance")
    assert (row["value"], row["status"], row["reason"]) == ("80", "active", "")
    assert json.loads(row["derived_from"]) == [1, 2, 3]
    assert explain_key(conn, "wallet", "balance").events[-1].at_abs is None
    conn.close()


def test_时间未知的事件照样入库(tmp_path: Path) -> None:
    """§15.5：解析不出来**不阻塞写入**，原始观察不可丢；只是不能当精确时刻用。"""
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(
        conn,
        draft_from_text(
            "前几天花了 20",
            clock=CLOCK,
            entity="wallet",
            attribute="balance",
            op="delta",
            value="-20",
            unit="CNY",
            speaker="user",
        ),
        clock=CLOCK,
    )
    event = fetch_events(conn, "wallet", "balance")[0]
    assert event.at_abs is None
    assert (event.at_source, event.time_precision) == ("unknown", "unknown")
    assert event.raw_text == "前几天花了 20"  # 原文不许改写
    assert event.confidence == CONFIDENCE_UNKNOWN_TIME  # 时间 unknown 的默认低
    # 渲染带 `?` 标记的形态由 T11 接；这里先保证"时间不可用"这件事在行里可查。
    row = fetch_current_state(conn, "wallet", "balance")
    assert (row["status"], row["reason"]) == ("needs_review", "missing_base")
    conn.close()


# ---- 金额不浮点 --------------------------------------------------------------


def test_金额不浮点(tmp_path: Path) -> None:
    """§15.3 第 4 条：`0.1 + 0.2` 必须是 `0.3`，不是 `0.30000000000000004`。"""
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "0.1", 1.0, unit="CNY"), clock=CLOCK)
    append_event(conn, _draft("delta", "0.2", 2.0, unit="CNY"), clock=CLOCK)

    row = fetch_current_state(conn, "wallet", "balance")
    assert row["value"] == "0.3"
    assert Decimal(row["value"]) == Decimal("0.1") + Decimal("0.2") == Decimal("0.30")
    # 用 float 相加会得到另一个字符串（把实现改成 float → 上面两条红）。
    assert row["value"] != str(0.1 + 0.2)
    assert float(row["value"]) != 0.1 + 0.2
    # 落库的是 TEXT（不是 REAL 列里的浮点数）。
    assert conn.execute(
        "SELECT typeof(value) FROM memory_state WHERE value = '0.3'"
    ).fetchone()[0] == "text"
    conn.close()


def test_整数金额不带小数点尾巴(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "100", 1.0), clock=CLOCK)
    append_event(conn, _draft("delta", "-20", 2.0), clock=CLOCK)
    append_event(conn, _draft("delta", "50", 3.0), clock=CLOCK)
    assert fetch_current_state(conn, "wallet", "balance")["value"] == "130"
    assert fetch_current_state(conn, "wallet", "balance")["value"] != "130.0"
    conn.close()


# ---- 其余归约器 / retract / 幂等 / verify ------------------------------------


def test_其余归约器逐条(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    register_attribute(conn, "agent", "package_manager", type="text", clock=CLOCK)
    register_attribute(conn, "user", "skills", type="tags", clock=CLOCK)
    register_attribute(conn, "exam", "best", type="best_score", clock=CLOCK)

    append_event(
        conn, _draft("set", "pip", 1.0, entity="agent", attribute="package_manager"), clock=CLOCK
    )
    append_event(
        conn, _draft("set", "uv", 2.0, entity="agent", attribute="package_manager"), clock=CLOCK
    )
    assert fetch_current_state(conn, "agent", "package_manager")["value"] == "uv"

    append_event(conn, _draft("set", "Python", 1.0, entity="user", attribute="skills"), clock=CLOCK)
    append_event(conn, _draft("set", "Go", 2.0, entity="user", attribute="skills"), clock=CLOCK)
    append_event(conn, _draft("set", "Python", 3.0, entity="user", attribute="skills"), clock=CLOCK)
    union = fetch_current_state(conn, "user", "skills")
    assert union["value"] == "Python, Go"  # 并集去重、按链上首次出现（不排序、不相加）
    assert union["reducer"] == "set"
    # 并集类不判"同刻冲突"：同刻两条不同值就是并集本身（§15.3 的 tags/files 口径）。
    append_event(
        conn, _draft("set", "Rust", 3.0, entity="user", attribute="skills"), clock=CLOCK
    )
    assert fetch_current_state(conn, "user", "skills")["value"] == "Python, Go, Rust"
    assert fetch_current_state(conn, "user", "skills")["status"] == "active"

    append_event(conn, _draft("set", "88", 1.0, entity="exam", attribute="best"), clock=CLOCK)
    append_event(conn, _draft("set", "92", 2.0, entity="exam", attribute="best"), clock=CLOCK)
    append_event(conn, _draft("set", "75", 3.0, entity="exam", attribute="best"), clock=CLOCK)
    assert fetch_current_state(conn, "exam", "best")["value"] == "92"

    # `min` 没有类型对照表，只有显式登记才用得上。
    register_attribute(conn, "exam", "worst", reducer="min", clock=CLOCK)
    append_event(conn, _draft("set", "88", 1.0, entity="exam", attribute="worst"), clock=CLOCK)
    append_event(conn, _draft("set", "75", 2.0, entity="exam", attribute="worst"), clock=CLOCK)
    assert fetch_current_state(conn, "exam", "worst")["value"] == "75"
    conn.close()


def test_retract是软撤销不是删除(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _balance(conn)
    # 撤销"花了 20"：值被重算，事件仍在库里。
    retract_event(conn, 2, clock=CLOCK, actor="cli", reason="记错了")

    assert fetch_current_state(conn, "wallet", "balance")["value"] == "150"
    assert conn.execute("SELECT count(*) FROM memory_event").fetchone()[0] == 3
    assert conn.execute(
        "SELECT status FROM memory_event WHERE id = 2"
    ).fetchone()[0] == "retracted"
    # 软撤销不进归约，但**保留在证据链里**（§15.3 第 2 步）。
    assert json.loads(fetch_current_state(conn, "wallet", "balance")["derived_from"]) == [1, 2, 3]
    assert tdb.list_audit(conn, action="retract")[0]["target"] == "event:2"

    # 撤销掉基准（那条 set）→ 缺基准，值留空（不是回到 0）。
    retract_event(conn, 1, clock=CLOCK, actor="cli", reason="基准记错了")
    row = fetch_current_state(conn, "wallet", "balance")
    assert (row["value"], row["reason"]) == (None, "missing_base")
    conn.close()


def test_幂等_同一round_key写两次只有一条(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    draft = _draft("set", "100", 1.0)
    draft = replace(draft, round_key="s1#turn:1")

    first = append_event(conn, draft, clock=CLOCK)
    again = append_event(conn, draft, clock=CLOCK)
    assert first.created is True and first.duplicate is False
    assert again.created is False and again.duplicate is True
    assert again.event_id == first.event_id
    assert conn.execute("SELECT count(*) FROM memory_event").fetchone()[0] == 1
    assert fetch_current_state(conn, "wallet", "balance")["value"] == "100"
    conn.close()


def test_verify发现缓存与重算不一致(tmp_path: Path) -> None:
    """§15.3 硬规矩 3：以重算值为准 + audit + `needs_review` 直到有人看过。"""
    conn = _db(tmp_path)
    _balance(conn)
    # 手工把缓存改坏（模拟"当初有一次归约错了"）。
    conn.execute("UPDATE memory_state SET value = '999' WHERE valid_to IS NULL")
    conn.commit()

    result = verify_key(conn, "wallet", "balance", clock=CLOCK)
    assert result.action == "mismatch"
    assert result.state is not None and result.state.value == "130"
    row = fetch_current_state(conn, "wallet", "balance")
    assert (row["value"], row["status"], row["reason"]) == ("130", "needs_review", "")
    audit = tdb.list_audit(conn, action="recompute_mismatch")[0]
    assert (audit["before"], audit["after"]) == ("999", "130")
    assert "有人看过" in audit["reason"]

    # 后续重算**不自动清掉**这个标记（"直到有人看过"）。
    assert recompute_key(conn, "wallet", "balance", clock=CLOCK).state.status == "needs_review"
    # "有人看过"这个动作由 `review_key` 表达，并且必须写 audit。
    reviewed = review_key(conn, "wallet", "balance", clock=CLOCK, actor="cli")
    assert reviewed.action == "reviewed"
    assert reviewed.state.status == "active"
    assert tdb.list_audit(conn, action="review")[0]["target"].startswith("state:")
    conn.close()


def test_事件晚到重算整条链(tmp_path: Path) -> None:
    """§15.6 第 4 条：晚到的 event 让**整条链**重排重算（不是追加），并留 backfill 审计。"""
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    append_event(conn, _draft("set", "100", 1.0), clock=CLOCK)
    append_event(conn, _draft("delta", "-20", 2.0), clock=CLOCK)
    append_event(conn, _draft("delta", "50", 3.0), clock=CLOCK)
    assert fetch_current_state(conn, "wallet", "balance")["value"] == "130"

    # 晚到的一条：时刻在 1.0 与 2.0 之间（比当前行的 valid_from=3.0 早）。
    append_event(conn, _draft("delta", "10", 1.5, raw_text="其实还收了 10"), clock=CLOCK)

    row = fetch_current_state(conn, "wallet", "balance")
    assert row["value"] == "140"  # 100 + 10 - 20 + 50：整条链按时间重排
    assert json.loads(row["derived_from"]) == [1, 2, 3, 4]
    assert tdb.count_current_rows(conn, "wallet", "balance") == 1
    backfills = tdb.list_audit(conn, action="recompute_backfill")
    assert len(backfills) == 1
    assert "event:4" in backfills[0]["reason"]
    conn.close()


def test_state只能由归约器写() -> None:
    """`state` 只能由归约器写（§15.1）：写函数是模块私有的，且插行只出现在 reduce.py。"""
    assert hasattr(reduce_module, "_write_state")
    assert not hasattr(reduce_module, "write_state")
    writers = {
        path.name
        for path in sorted(TIMELINE_DIR.glob("*.py"))
        if "insert_state(" in path.read_text(encoding="utf-8")
    }
    assert writers == {"db.py", "reduce.py"}  # db.py 是定义，reduce.py 是唯一的调用方
    # 归约器的公开面里没有"直接写 state"的口子。
    assert "insert_state" not in reduce_module.__dict__
