"""确定性归约器 + 取代链 + 冲突（设计稿 §15.3 / §15.4 / §15.6 + 计划 T9）。

**这一层是"值从哪来"的唯一答案**：`memory_state` 里的每一个数都由事件链算出，
模型不许直接写（§15.1），所以本模块的写法是：

- `reduce_events(events, reducer)` 是**纯函数**——只有 event 进、值出，不碰库、
  不碰时钟、不碰模型。M4-T1 的 `130` 就是它算的，所以"这个值必须由事件链推出"
  不是一句声明，是可复算的（§10.4 M4-T1 ②）。
- `Decimal` 全程替代 `float`（§15.3 第 4 条）：`0.1 + 0.2` 必须是 `0.3`，
  不是 `0.30000000000000004`——账不平在时序记忆里是最难定位的一类 bug。
- 取代链**三步、同一事务**（§15.4）：① 关旧行（`valid_to` = 新行 `valid_from`）
  → ② 插新行、取回 `id` → ③ 旧行 `superseded_by` = 新行 id。顺序不能换：
  `superseded_by` 是**真外键**，而"至多一行 `valid_to IS NULL`"由部分唯一索引挡着。
- 六类不确定**不许静默挑一个**（§15.6）：`missing_base` / `unit_mismatch` /
  `same_instant` / `no_reducer` / `time_unknown` 各有明确的 `status` + `reason`，
  冲突的两条事件**都留**，`conflict_note` 是可读的判定理由（`explain_key` 的第二出口）。

一处实现口径（设计稿没给死数值，写在这里备查，计划 T9 风险 ①）：
**"同一精度窗口"= 同 `time_precision` 且同窗口起点**（`day` 按各自事件的时区取零点、
`minute` 取分钟起点、`exact`/`turn` 取绝对时刻）。`time_precision='unknown'` 的
**不参与**冲突判定（§15.5），但**仍进 `derived_from`**（§15.6 风险 ③）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from spoolkit import limits
from spoolkit.memory.timeline import db as tdb
from spoolkit.memory.timeline import registry
from spoolkit.memory.timeline.clock import Clock, day_start, local_dt, minute_start
from spoolkit.memory.timeline.models import (
    EVENT_OPS,
    REDUCERS,
    Event,
    StateRow,
)
from spoolkit.memory.timeline.parse import TimeResult, parse_time
from spoolkit.memory.timeline.registry import ResolvedKey

# 时间未知的观察默认给低置信度（§15.1：「时间 unknown 的观察默认低」）。
# 设计稿没给数值，这里是实现口径：低于 DDL 默认 0.5 即为"低"。
CONFIDENCE_UNKNOWN_TIME = 0.2
CONFIDENCE_DEFAULT = 0.5

# 话语来源优先级 `user > tool > agent`，**只用来给建议**（§15.6 第 3 条），
# 默认关（`state_auto_pick=0`）；不许用它删任何一条事件。
SPEAKER_RANK = {"user": 3, "tool": 2, "agent": 1, "": 0}


@dataclass(frozen=True)
class Reduction:
    """一次归约的完整结果：值 + 状态 + 理由 + 证据链（纯函数产出，不落库）。"""

    value: str | None
    reducer: str
    status: str  # active|conflict|needs_review
    reason: str  # ""|missing_base|unit_mismatch|same_instant|no_reducer|time_unknown
    derived_from: tuple[int, ...]  # 证据链：链上全部 event id，升序（§15.3 第 6 条）
    ordered: tuple[Event, ...]  # 参与归约的事件，按 at_abs 升序（同刻按 id）
    unit: str = ""
    conflict_events: tuple[int, ...] = ()
    conflict_note: str = ""
    skipped_retracted: tuple[int, ...] = ()
    auto_picked: int | None = None

    @property
    def sorted_events(self) -> tuple[Event, ...]:
        """按 id 升序的事件（落库侧渲染证据链用）。"""
        return tuple(sorted(self.ordered, key=lambda event: event.id))

    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(f"event:{event_id}" for event_id in self.derived_from)

    def events_by_id(self) -> dict[int, Event]:
        return {event.id: event for event in self.ordered}

    @property
    def conflict_count(self) -> int:
        return len(self.conflict_events)


@dataclass(frozen=True)
class EventDraft:
    """一条待落库的观察（写入侧的形状）。时间字段直接来自 T8 的 `TimeResult`。"""

    entity: str
    attribute: str
    op: str
    value: str
    raw_text: str
    at_abs: float | None = None
    unit: str = ""
    at_source: str = "explicit"
    time_precision: str = "exact"
    tz_offset_min: int | None = None  # None → 用注入时钟的时区
    tz_name: str | None = None
    source_ref: str = ""
    speaker: str = ""
    confidence: float | None = None
    round_key: str = ""
    content_hash: str = ""


@dataclass(frozen=True)
class AppendResult:
    """一次 `append_event` 的结果。`duplicate=True` = 幂等命中，**没有新行**。"""

    event_id: int
    created: bool
    duplicate: bool
    key: ResolvedKey
    reduction: Reduction | None = None


@dataclass(frozen=True)
class RecomputeResult:
    """一次 `recompute_key` / `verify_key` 的结果（含落库后的当前行）。"""

    key: ResolvedKey
    reduction: Reduction
    state: StateRow | None
    action: str  # no_events|created|unchanged|updated|superseded|mismatch|reviewed
    valid_from: float | None = None
    superseded_id: int | None = None


# ---- 纯函数：值从事件链来（M4-T1 的 130 就在这里）-----------------------------


def reduce_events(
    events: tuple[Event, ...] | list[Event],
    reducer: str,
    *,
    auto_pick: bool = False,
) -> Reduction:
    """按 §15.3 的算法归约一条事件链。**纯函数**：无库、无时钟、无模型。

    `reducer='none'`（未登记）**不推**：`needs_review` + `no_reducer`，
    **不许默认套 `sum_delta`**（§15.6 第 5 条）。
    """
    ordered = _order_events(events)
    derived = tuple(sorted(event.id for event in ordered))
    skipped = tuple(event.id for event in ordered if event.status == "retracted")
    if reducer not in REDUCERS or reducer == "none":
        return Reduction(
            None, "none", "needs_review", "no_reducer", derived, ordered,
            skipped_retracted=skipped,
        )
    if not ordered:
        return Reduction(
            None, reducer, "needs_review", "missing_base", derived, ordered,
            skipped_retracted=skipped,
        )

    active = tuple(event for event in ordered if event.status != "retracted")
    unit = _single_unit(active)
    unit_mismatch = _has_unit_mismatch(active) and reducer in (
        "sum_delta",
        "max",
        "min",
        "set",
    )
    # 并集类（`set` 归约器）不判同刻冲突：它的语义就是并集，"两条不同值"是正常的。
    conflicts = () if reducer == "set" else _conflict_groups(active)
    conflict_ids = tuple(event.id for group in conflicts for event in group)
    conflict_note = _conflict_note(conflicts)

    if unit_mismatch:
        return Reduction(
            None, reducer, "conflict", "unit_mismatch", derived, ordered,
            unit="", conflict_events=conflict_ids, conflict_note=conflict_note,
            skipped_retracted=skipped,
        )

    picked: Event | None = None
    if conflicts:
        if not auto_pick:
            # **两条都留**：这里连"选一个"的代码路径都不走（§15.6 第 3 条）。
            return Reduction(
                None, reducer, "conflict", "same_instant", derived, ordered,
                unit=unit, conflict_events=conflict_ids, conflict_note=conflict_note,
                skipped_retracted=skipped,
            )
        picked = _pick_by_speaker(conflicts, ordered)

    if reducer == "sum_delta":
        result = _reduce_sum_delta(
            active, ordered, derived, unit, skipped, conflict_ids, conflict_note, picked
        )
    elif reducer == "latest":
        result = _reduce_latest(
            active, ordered, derived, unit, skipped, conflict_ids, conflict_note, picked
        )
    elif reducer == "set":
        result = _reduce_union(active, ordered, derived, unit, skipped)
    else:
        result = _reduce_extreme(
            reducer, active, ordered, derived, unit, skipped, conflict_ids,
            conflict_note, picked,
        )

    if conflicts:
        # `state_auto_pick=1`：给的是**带标记的建议值**——status/reason 照旧是"未定"，
        # 只是 value 有数了；`auto_picked` 与 audit 的 reason 说明它是自动选的（§15.6）。
        result = replace(
            result,
            status="conflict",
            reason="same_instant",
            conflict_events=conflict_ids,
            conflict_note=conflict_note,
            auto_picked=None if picked is None else picked.id,
        )
    return result


def _time_unknown_reduction(
    reducer: str, ops: tuple[str, ...], active, derived, ordered, unit, skipped
) -> Reduction | None:
    """§15.6 第 6 条：影响结论的那条是**未知时间** → `time_unknown`，`value` 留空。

    **不假装它在链首或链尾**：排序上它排在最后只是展示，一旦它会影响算出来的数
    （基准的取值、增量的先后、`latest` 谁最新），就不给值。
    设计稿的判定顺序是 `missing_base` → `unit_mismatch` → `same_instant` → `time_unknown`，
    所以这个检查放在"有没有可用基准"**之后**、真正做算术**之前**。
    """
    hits = [
        event for event in active if event.op in ops and _time_unknown(event)
    ]
    if not hits:
        return None
    refs = ", ".join(f"event:{event.id}" for event in hits)
    return Reduction(
        None, reducer, "needs_review", "time_unknown", derived, ordered,
        unit=unit, skipped_retracted=skipped,
        conflict_note=f"时间未知的事件影响结论（{refs}）：不猜它在链首还是链尾",
    )


def _reduce_sum_delta(
    active, ordered, derived, unit, skipped, conflict_ids, conflict_note, picked
) -> Reduction:
    """§15.3 的 `sum_delta`：最后一次 `set` 为基准，之后（含同刻）的 `delta` 累加。"""
    sets = [event for event in active if event.op == "set"]
    base = picked if picked is not None else (sets[-1] if sets else None)
    if base is None:
        # 有 delta 但没有 set → 缺基准，`value` **留空**（§15.6 第 1 条）。
        return Reduction(
            None, "sum_delta", "needs_review", "missing_base", derived, ordered,
            unit=unit, skipped_retracted=skipped,
            conflict_note=conflict_note or _missing_base_note(active, derived),
        )
    blocked = _time_unknown_reduction(
        "sum_delta", ("set", "delta"), active, derived, ordered, unit, skipped
    )
    if blocked is not None:
        return blocked
    base_value = _decimal(base.value)
    if base_value is None:
        return Reduction(
            None, "sum_delta", "needs_review", "missing_base", derived, ordered,
            unit=unit, skipped_retracted=skipped,
            conflict_note=f"基准 {base.value!r}（event:{base.id}）不是可加的数值：不猜",
        )
    position = {event.id: index for index, event in enumerate(ordered)}
    base_position = position[base.id]
    total = base_value
    for event in active:
        if event.op != "delta" or position[event.id] <= base_position:
            continue
        delta = _decimal(event.value)
        if delta is None:
            return Reduction(
                None, "sum_delta", "needs_review", "missing_base", derived, ordered,
                unit=unit, skipped_retracted=skipped,
                conflict_note=f"增量 {event.value!r}（event:{event.id}）不是数值：不猜",
            )
        total += delta
    return Reduction(
        _format_decimal(total), "sum_delta", "active", "", derived, ordered,
        unit=unit, skipped_retracted=skipped,
    )


def _reduce_latest(
    active, ordered, derived, unit, skipped, conflict_ids, conflict_note, picked
) -> Reduction:
    candidates = [event for event in active if event.op in ("set", "observe")]
    if not candidates:
        return Reduction(
            None, "latest", "needs_review", "missing_base", derived, ordered,
            unit=unit, skipped_retracted=skipped,
            conflict_note=_missing_base_note(active, derived),
        )
    blocked = _time_unknown_reduction(
        "latest", ("set", "observe"), active, derived, ordered, unit, skipped
    )
    if blocked is not None:
        return blocked
    chosen = picked if picked is not None else candidates[-1]
    return Reduction(
        str(chosen.value).strip(), "latest", "active", "", derived, ordered,
        unit=chosen.unit or unit, skipped_retracted=skipped,
    )


def _reduce_union(active, ordered, derived, unit, skipped) -> Reduction:
    """`tags` / `files`：并集去重，**不排序、不相加**（§15.3 表）。"""
    values: list[str] = []
    for event in active:
        if event.op != "set":
            continue
        text = str(event.value).strip()
        if text not in values:
            values.append(text)
    if not values:
        return Reduction(
            None, "set", "needs_review", "missing_base", derived, ordered,
            unit=unit, skipped_retracted=skipped,
            conflict_note=_missing_base_note(active, derived),
        )
    return Reduction(
        ", ".join(values), "set", "active", "", derived, ordered,
        unit=unit, skipped_retracted=skipped,
    )


def _reduce_extreme(
    reducer, active, ordered, derived, unit, skipped, conflict_ids,
    conflict_note, picked,
) -> Reduction:
    sets = [event for event in active if event.op == "set"]
    if not sets:
        return Reduction(
            None, reducer, "needs_review", "missing_base", derived, ordered,
            unit=unit, skipped_retracted=skipped,
            conflict_note=_missing_base_note(active, derived),
        )
    blocked = _time_unknown_reduction(
        reducer, ("set",), active, derived, ordered, unit, skipped
    )
    if blocked is not None:
        return blocked
    numbers = [(event, _decimal(event.value)) for event in sets]
    if any(number is None for _, number in numbers):
        return Reduction(
            None, reducer, "needs_review", "missing_base", derived, ordered,
            unit=unit, skipped_retracted=skipped,
            conflict_note=f"{reducer} 需要数值：链上有读不出的值，不猜",
        )
    if picked is not None and _decimal(picked.value) is not None:
        chosen = _decimal(picked.value)
    elif reducer == "max":
        chosen = max(number for _, number in numbers)
    else:
        chosen = min(number for _, number in numbers)
    return Reduction(
        _format_decimal(chosen), reducer, "active", "", derived, ordered,
        unit=unit, skipped_retracted=skipped,
    )


def _order_events(events) -> tuple[Event, ...]:
    """`at_abs` 升序、同刻按 id 升序；**时间未知的排在最后**（§15.5 的渲染口径）。

    "不假装它在链首或链尾"由 `_time_unknown` 那一条拦下：这里排在哪里只影响展示，
    影响结论时一律落 `time_unknown`。
    """
    return tuple(
        sorted(
            events,
            key=lambda event: (
                event.at_abs is None,
                event.at_abs if event.at_abs is not None else 0.0,
                event.id,
            ),
        )
    )


def _time_unknown(event: Event) -> bool:
    return event.at_abs is None or event.time_precision == "unknown"


def _single_unit(active) -> str:
    units = {event.unit for event in active if event.unit}
    return units.pop() if len(units) == 1 else ""


def _has_unit_mismatch(active) -> bool:
    return len({event.unit for event in active if event.unit}) > 1


def _window(event: Event) -> tuple | None:
    """"同一精度窗口"的判据（见模块 docstring 的实现口径）。"""
    if _time_unknown(event):
        return None
    at_abs = float(event.at_abs or 0.0)
    tz_min = int(event.tz_offset_min)
    if event.time_precision == "day":
        return ("day", day_start(at_abs, tz_min))
    if event.time_precision == "minute":
        return ("minute", minute_start(at_abs, tz_min))
    return (event.time_precision, at_abs)


def _conflict_groups(active) -> tuple[tuple[Event, ...], ...]:
    """同精度窗口内的**互斥** `op='set'`（值不同）分组（§15.6 第 3 条）。

    并集类（`set` 归约器）不在这里判冲突：它的语义就是并集，"两条不同值"是正常的。
    """
    buckets: dict[tuple, list[Event]] = {}
    for event in active:
        if event.op != "set":
            continue
        window = _window(event)
        if window is None:
            continue
        buckets.setdefault(window, []).append(event)
    groups = []
    for items in buckets.values():
        if len(items) < 2:
            continue
        if len({_value_key(event.value) for event in items}) < 2:
            continue  # 值相同（"100" 与 "100.0" 同值）= 不互斥
        groups.append(tuple(items))
    return tuple(groups)


def _value_key(value: str) -> tuple:
    number = _decimal(value)
    if number is not None:
        return ("num", number)
    return ("text", str(value).strip())


def _conflict_note(groups) -> str:
    if not groups:
        return ""
    parts = []
    for group in groups:
        values = " / ".join(str(event.value) for event in group)
        refs = ", ".join(f"event:{event.id}" for event in group)
        precision = group[0].time_precision
        parts.append(
            f"{len(group)} 条互斥的 op='set'（{values}）落在同一精度窗口"
            f"（{precision}），见 {refs}"
        )
    return "同刻冲突：" + "；".join(parts)


def _missing_base_note(active, derived) -> str:
    ops = "、".join(sorted({event.op for event in active})) or "空链"
    refs = ", ".join(f"event:{event_id}" for event_id in derived) or "无"
    return f"链上（{refs}）只有 {ops}，没有可用的基准值：value 留空，不给猜的数"


def _pick_by_speaker(groups, ordered) -> Event:
    """`state_auto_pick=1` 时的建议值：`user > tool > agent`，同档取更晚的一条。

    默认关（`state_auto_pick=0`）；**开启时也必须在输出里标明是自动选的**
    （`Reduction.auto_picked` 与 audit 的 reason，§15.6 第 3 条）。
    """
    position = {event.id: index for index, event in enumerate(ordered)}
    candidates = [event for group in groups for event in group]
    return max(
        candidates,
        key=lambda event: (SPEAKER_RANK.get(event.speaker, 0), position[event.id]),
    )


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _format_decimal(number: Decimal) -> str:
    """把 `Decimal` 写成**不用科学计数法**的规范串（金额/数量落库的形态）。"""
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-"):
        return "0"
    return text


# ---- 写入侧：事件 ------------------------------------------------------------


def draft_from_text(
    raw_text: str,
    *,
    clock: Clock,
    entity: str,
    attribute: str,
    op: str,
    value: str,
    unit: str = "",
    time_text: str | None = None,
    source_ref: str = "",
    speaker: str = "",
    confidence: float | None = None,
    session_started_at: float | None = None,
    turn_at: float | None = None,
    round_key: str = "",
) -> EventDraft:
    """从一句原文（T8 的解析）造一条待落库的观察。

    时间走 `parse_time`：解析不出来就 `at_abs=None` + `time_precision='unknown'`，
    **照样入库**（§15.5"不阻塞写入"：原始观察不可丢，只是不能当精确时刻用）。
    """
    result = parse_time(
        time_text if time_text is not None else raw_text,
        clock=clock,
        session_started_at=session_started_at,
        turn_at=turn_at,
    )
    return EventDraft(
        entity=entity,
        attribute=attribute,
        op=op,
        value=value,
        raw_text=raw_text,
        at_abs=result.at_abs,
        unit=unit,
        at_source=result.at_source,
        time_precision=result.time_precision,
        tz_offset_min=result.tz_offset_min,
        tz_name=result.tz_name,
        source_ref=source_ref,
        speaker=speaker,
        confidence=confidence if confidence is not None else _default_confidence(result),
        round_key=round_key,
    )


def _default_confidence(result: TimeResult) -> float:
    if result.time_precision == "unknown":
        return CONFIDENCE_UNKNOWN_TIME
    return CONFIDENCE_DEFAULT


def event_content_hash(
    entity: str, attribute: str, op: str, value: str, unit: str, fingerprint: str
) -> str:
    """事件的去重指纹：`sha1(结构化字段)` 前 16 位（与 §4.3 的位数口径一致）。

    **不用 `gate.content_hash`**：它按 §4.3 的口径去标点、折大小写，会把
    `delta -20` 与 `delta +20` 抹成同一个指纹——那是给自然语言句子用的归一化，
    用在结构化取值上会把符号和小数点丢掉。
    """
    payload = "\x1f".join(
        (entity, attribute, op, str(value).strip(), str(unit), fingerprint)
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def append_event(
    conn,
    draft: EventDraft,
    *,
    clock: Clock,
    actor: str = "system",
    recompute: bool = True,
) -> AppendResult:
    """落一条事件并按需重算它那一个键（**只重算受影响的键**，§15.3 硬规矩 2）。

    幂等：同一 `round_key`（或同一 `content_hash`）写两次 → 一条（§15.7 ④）。
    归一化与别名解析在这一处做一次，落库的是 canonical 键（§15.2 第 4 行）。
    """
    if draft.op not in EVENT_OPS:
        raise ValueError(f"没有这个 op：{draft.op!r}（§15.1 只有 {EVENT_OPS}）")
    if not str(draft.value).strip():
        raise ValueError("事件的 value 不能是空串（§15.1：必填）")
    key = registry.resolve_key(conn, draft.entity, draft.attribute)
    tz_min = draft.tz_offset_min if draft.tz_offset_min is not None else clock.tz_offset_min()
    confidence = (
        draft.confidence
        if draft.confidence is not None
        else (
            CONFIDENCE_UNKNOWN_TIME
            if draft.time_precision == "unknown"
            else CONFIDENCE_DEFAULT
        )
    )
    digest = draft.content_hash or event_content_hash(
        key.entity,
        key.attribute,
        draft.op,
        draft.value,
        draft.unit,
        draft.round_key or draft.raw_text,
    )
    values = {
        "entity": key.entity,
        "attribute": key.attribute,
        "op": draft.op,
        "value": str(draft.value),
        "unit": draft.unit,
        "at_abs": draft.at_abs,
        "tz_offset_min": int(tz_min),
        "tz_name": draft.tz_name,
        "at_source": draft.at_source,
        "time_precision": draft.time_precision,
        "source_ref": draft.source_ref,
        "raw_text": draft.raw_text,
        "speaker": draft.speaker,
        "confidence": float(confidence),
        "content_hash": digest,
        "status": "active",
    }
    with tdb.transaction(conn):
        existing = tdb.find_event_by_hash(conn, key.entity, key.attribute, digest)
        if existing is not None:
            return AppendResult(
                int(existing["id"]), False, True, key, None
            )
        event_id = tdb.insert_event(conn, values)
        reduction = None
        if recompute:
            result = recompute_key(
                conn,
                key.entity,
                key.attribute,
                clock=clock,
                actor=actor,
                trigger_event_id=event_id,
            )
            reduction = result.reduction
        return AppendResult(event_id, True, False, key, reduction)


def retract_event(
    conn,
    event_id: int,
    *,
    clock: Clock,
    actor: str = "cli",
    reason: str = "",
) -> RecomputeResult | None:
    """软撤销一条事件（§15.4：纠错唯一路径），然后重算它那一个键。

    行**仍在库里**（`status='retracted'`）；它不再进归约，**但仍进证据链**
    （§15.3 第 2 步）。`memory_event` 没有 DELETE 路径。
    """
    event = tdb.get_event(conn, event_id)
    if event is None:
        raise KeyError(f"没有这条事件：event:{event_id}")
    now = clock.now()
    with tdb.transaction(conn):
        tdb.set_event_status(conn, event_id, "retracted")
        tdb.audit(
            conn,
            at=now,
            actor=actor,
            action="retract",
            target=f"event:{event_id}",
            before="active",
            after="retracted",
            reason=reason or "软撤销：不进归约，但保留在证据链里（§15.3 第 2 步）",
        )
        return recompute_key(conn, event.entity, event.attribute, clock=clock, actor=actor)


# ---- 写入侧：状态（只能由归约器写，§15.1）------------------------------------


def recompute_key(
    conn,
    entity: str,
    attribute: str,
    *,
    clock: Clock,
    actor: str = "system",
    force_reducer: str | None = None,
    auto_pick: bool | None = None,
    overrides: dict | None = None,
    trigger_event_id: int | None = None,
) -> RecomputeResult:
    """重算**一个键**（不是全库，§15.3 硬规矩 2）并把结果写成 state 行。

    `trigger_event_id` = 触发这次重算的那条事件（`append_event` 传）。它早于当前行
    起点时就是**事件晚到**：整条链重排重算（不是"追加"），audit 记
    `recompute_backfill`（§15.6 第 4 条）。
    """
    key = registry.resolve_key(
        conn, entity, attribute, force_reducer=force_reducer
    )
    events = tdb.fetch_events(conn, key.entity, key.attribute)
    if auto_pick is None:
        auto_pick = bool(limits.resolve("state_auto_pick", overrides)[0])
    reduction = reduce_events(events, key.reducer, auto_pick=auto_pick)
    if not events:
        return RecomputeResult(key, reduction, None, "no_events")
    computed_at = clock.now()
    valid_from = _trigger_valid_from(events, clock=clock)
    with tdb.transaction(conn):
        current = tdb.fetch_current_state(conn, key.entity, key.attribute)
        status, reason = reduction.status, reduction.reason
        if _review_pending(current) and reduction.status == "active":
            # §15.3 硬规矩 3：不一致的标记"直到有人看过"——后续重算**不自动清掉它**
            # （清它的唯一路径是 `review_key`，那是"有人看过"这个动作）。
            if float(current["valid_from"]) >= float(valid_from):
                status = "needs_review"
                reason = ""
        audit_action, audit_reason = _backfill_audit(
            events, current=current, trigger_event_id=trigger_event_id
        )
        row, action, superseded_id = _write_state(
            conn,
            key,
            reduction,
            current=current,
            valid_from=valid_from,
            computed_at=computed_at,
            status=status,
            reason=reason,
            actor=actor,
            audit_action=audit_action,
            audit_reason=audit_reason,
        )
    return RecomputeResult(
        key, reduction, row, action, valid_from=valid_from, superseded_id=superseded_id
    )


def _backfill_audit(events, *, current, trigger_event_id: int | None) -> tuple[str, str]:
    """事件晚到的判定：触发事件比当前行的起点还早 → 整链重算 + `recompute_backfill`。"""
    if trigger_event_id is None or current is None:
        return ("", "")
    event = next((item for item in events if item.id == trigger_event_id), None)
    if event is None or event.at_abs is None:
        return ("", "")
    if float(event.at_abs) >= float(current["valid_from"]):
        return ("", "")
    return (
        "recompute_backfill",
        f"事件晚到：event:{trigger_event_id} 的 at_abs={float(event.at_abs)} 早于当前行"
        f" valid_from={float(current['valid_from'])}，整条链按时间重排重算（不是追加）",
    )


def recompute_all(
    conn, *, clock: Clock, actor: str = "system"
) -> tuple[RecomputeResult, ...]:
    """全量重算（`memory state verify` 用；每个键一次，与 `recompute_key` 同一算法）。"""
    results = []
    for entity, attribute in tdb.list_keys(conn):
        results.append(
            recompute_key(conn, entity, attribute, clock=clock, actor=actor)
        )
    return tuple(results)


def verify_key(
    conn,
    entity: str,
    attribute: str,
    *,
    clock: Clock,
    actor: str = "system",
    auto_pick: bool | None = None,
    overrides: dict | None = None,
) -> RecomputeResult:
    """§15.3 硬规矩 2/3：把**缓存行**与**从事件链重算的值**比一比。

    不一致时：**以重算值为准**（不许保留缓存值）、写
    `memory_audit(action='recompute_mismatch', before=缓存, after=重算)`、
    并把该行标 `needs_review`（`reason=''` 就是"待复核"这个标记；五个
    `STATE_REASONS` 里没有第六个出口）**直到有人看过**（`review_key`）。
    """
    key = registry.resolve_key(conn, entity, attribute)
    events = tdb.fetch_events(conn, key.entity, key.attribute)
    if auto_pick is None:
        auto_pick = bool(limits.resolve("state_auto_pick", overrides)[0])
    reduction = reduce_events(events, key.reducer, auto_pick=auto_pick)
    if not events:
        return RecomputeResult(key, reduction, None, "no_events")
    computed_at = clock.now()
    valid_from = _trigger_valid_from(events, clock=clock)
    with tdb.transaction(conn):
        current = tdb.fetch_current_state(conn, key.entity, key.attribute)
        cached = None if current is None else current["value"]
        mismatch = current is None or (
            str(current["value"]) != str(reduction.value)
            or current["status"] != reduction.status
            or current["reason"] != reduction.reason
        )
        if not mismatch:
            return RecomputeResult(
                key, reduction, tdb.to_state_row(current), "unchanged",
                valid_from=valid_from,
            )
        row, action, superseded_id = _write_state(
            conn,
            key,
            reduction,
            current=current,
            valid_from=valid_from,
            computed_at=computed_at,
            status="needs_review",
            reason="",
            actor=actor,
            audit_action="recompute_mismatch",
            audit_reason=(
                f"缓存 {cached!r} 与重算 {reduction.value!r} 不一致：以重算值为准，"
                "标 needs_review 直到有人看过（§15.3 硬规矩 3）"
            ),
        )
    return RecomputeResult(
        key, reduction, row, "mismatch", valid_from=valid_from,
        superseded_id=superseded_id,
    )


def review_key(
    conn, entity: str, attribute: str, *, clock: Clock, actor: str = "cli"
) -> RecomputeResult:
    """"有人看过"这个动作：把 `needs_review`（`reason=''`）的标记清掉，写 audit。"""
    key = registry.resolve_key(conn, entity, attribute)
    events = tdb.fetch_events(conn, key.entity, key.attribute)
    reduction = reduce_events(events, key.reducer)
    computed_at = clock.now()
    if not events:
        return RecomputeResult(key, reduction, None, "no_events")
    valid_from = _trigger_valid_from(events, clock=clock)
    with tdb.transaction(conn):
        current = tdb.fetch_current_state(conn, key.entity, key.attribute)
        if not _review_pending(current):
            return RecomputeResult(
                key, reduction, None if current is None else tdb.to_state_row(current),
                "unchanged", valid_from=valid_from,
            )
        conn.execute(
            "UPDATE memory_state SET status = ?, reason = ?, computed_at = ? WHERE id = ?",
            (reduction.status, reduction.reason, float(computed_at), current["id"]),
        )
        tdb.audit(
            conn,
            at=computed_at,
            actor=actor,
            action="review",
            target=f"state:{current['id']}",
            before="needs_review",
            after=reduction.status,
            reason="有人看过这份重算结果：清掉待复核标记",
        )
        row = conn.execute(
            "SELECT * FROM memory_state WHERE id = ?", (current["id"],)
        ).fetchone()
    return RecomputeResult(
        key, reduction, tdb.to_state_row(row), "reviewed", valid_from=valid_from
    )


def _review_pending(current) -> bool:
    """`needs_review` + 空 reason = "重算与缓存不一致、待复核"这个标记。"""
    return (
        current is not None
        and current["status"] == "needs_review"
        and not (current["reason"] or "")
    )


def _trigger_valid_from(events, *, clock: Clock) -> float:
    """新 state 行的 `valid_from` = 链上**已知时刻**里最新那条的 `at_abs`。

    它是"这个值从哪一刻生效"：余额例子里 e1/e2/e3 分别让 [t1,t2) / [t2,t3) / [t3,∞)
    各有一段值，所以"我当时说的"与"现在是多少"两种问法各自都有行可读（§15.4）。
    整条链的时间都未知时退回"重算时刻"——**不假装**它发生在别的什么时候。
    """
    known = [event for event in events if event.at_abs is not None]
    if not known:
        return float(clock.now())
    newest = max(known, key=lambda event: (float(event.at_abs or 0.0), event.id))
    return float(newest.at_abs or 0.0)


def _state_values(
    key: ResolvedKey,
    reduction: Reduction,
    *,
    valid_from: float,
    computed_at: float,
    status: str,
    reason: str,
) -> dict:
    events = [event for event in reduction.ordered if event.status != "retracted"]
    confidence = max((event.confidence for event in events), default=CONFIDENCE_DEFAULT)
    return {
        "entity": key.entity,
        "attribute": key.attribute,
        "value": reduction.value,
        # unit 跟着 value 走：值留空时不宣称单位；`state_auto_pick` 给了建议值时保留单位。
        "unit": reduction.unit if reduction.value is not None else "",
        "derived_from": json.dumps(list(reduction.derived_from)),
        "reducer": reduction.reducer,
        "valid_from": float(valid_from),
        "valid_to": None,
        "superseded_by": None,
        "confidence": float(confidence),
        "status": status,
        "reason": reason,
        "computed_at": float(computed_at),
        "is_derived": 1,
    }


def _write_state(
    conn,
    key: ResolvedKey,
    reduction: Reduction,
    *,
    current,
    valid_from: float,
    computed_at: float,
    status: str,
    reason: str,
    actor: str,
    audit_action: str = "",
    audit_reason: str = "",
) -> tuple[StateRow, str, int | None]:
    """把一次归约落成 state 行：新建 / 同刻就地刷新 / **取代链三步**。

    - 没有当前行 → 新建（第一次有结论）；
    - 触发时刻与当前行 `valid_from` **相同**（或更早，含事件晚到）→ 就地刷新：半开区间
      在这一刻宽度为零，没有"旧值"可供回溯，而 `UNIQUE(entity, attribute, valid_from)`
      也不允许再插一行；
    - 触发时刻更晚（含事件晚到） → **取代**：关旧行 → 插新行 → 旧行指新行。
      **旧行不删、不改值**，所以"我当时说的"永远答得出来（§15.4）。
    """
    values = _state_values(
        key,
        reduction,
        valid_from=valid_from,
        computed_at=computed_at,
        status=status,
        reason=reason,
    )
    before = "" if current is None else (current["value"] or "")
    default_reason = audit_reason or _audit_reason(reduction)
    if current is None:
        new_id = tdb.insert_state(conn, values)
        tdb.audit(
            conn,
            at=computed_at,
            actor=actor,
            action=audit_action or "add",
            target=f"state:{new_id}",
            before="",
            after=values["value"] or "",
            reason=default_reason,
        )
        return (tdb.to_state_row(_row_by_id(conn, new_id)), "created", None)

    if float(valid_from) <= float(current["valid_from"]):
        if _same_state(current, values):
            return (tdb.to_state_row(current), "unchanged", None)
        conn.execute(
            "UPDATE memory_state SET value = ?, unit = ?, derived_from = ?, reducer = ?,"
            " status = ?, reason = ?, computed_at = ?, confidence = ? WHERE id = ?",
            (
                values["value"],
                values["unit"],
                values["derived_from"],
                values["reducer"],
                values["status"],
                values["reason"],
                values["computed_at"],
                values["confidence"],
                current["id"],
            ),
        )
        tdb.audit(
            conn,
            at=computed_at,
            actor=actor,
            action=audit_action or "recompute_same_instant",
            target=f"state:{current['id']}",
            before=before,
            after=values["value"] or "",
            reason=default_reason
            or "同刻重算：半开区间宽度为零，无旧值可回溯，故就地刷新（不插第二行）",
        )
        return (tdb.to_state_row(_row_by_id(conn, current["id"])), "updated", None)

    # 取代链：三步、同一事务（顺序是硬要求，§15.4）。
    old_id = int(current["id"])
    conn.execute(
        "UPDATE memory_state SET valid_to = ? WHERE id = ?",
        (float(valid_from), old_id),
    )
    new_id = tdb.insert_state(conn, values)
    conn.execute(
        "UPDATE memory_state SET superseded_by = ? WHERE id = ?", (new_id, old_id)
    )
    if current["superseded_by"] is not None:
        # 被取代的行不该再有 superseded_by；真出现说明有人手改过库，留痕。
        tdb.audit(
            conn,
            at=computed_at,
            actor=actor,
            action="recompute_mismatch",
            target=f"state:{old_id}",
            before=str(current["superseded_by"]),
            after=str(new_id),
            reason="被取代的行原本已指向别的行：以新的取代链为准，留痕备查",
        )
    tdb.audit(
        conn,
        at=computed_at,
        actor=actor,
        action=audit_action or "supersede",
        target=f"state:{new_id}",
        before=before,
        after=values["value"] or "",
        reason=default_reason
        or f"取代不是删除：旧行 state:{old_id} 关闭于 {valid_from}、指向 state:{new_id}",
    )
    return (tdb.to_state_row(_row_by_id(conn, new_id)), "superseded", old_id)


def _same_state(current, values: dict) -> bool:
    return (
        (current["value"] or "") == (values["value"] or "")
        and (current["unit"] or "") == (values["unit"] or "")
        and current["status"] == values["status"]
        and (current["reason"] or "") == (values["reason"] or "")
        and (current["derived_from"] or "") == values["derived_from"]
        and current["reducer"] == values["reducer"]
    )


def _row_by_id(conn, row_id):
    return conn.execute("SELECT * FROM memory_state WHERE id = ?", (row_id,)).fetchone()


def _audit_reason(reduction: Reduction) -> str:
    refs = ", ".join(f"event:{event_id}" for event_id in reduction.derived_from)
    parts = [f"reducer={reduction.reducer}"]
    if reduction.conflict_note:
        parts.append(reduction.conflict_note)
    if reduction.skipped_retracted:
        parts.append(
            "已软撤销（不进归约、仍在证据链）："
            + ", ".join(f"event:{event_id}" for event_id in reduction.skipped_retracted)
        )
    if reduction.auto_picked is not None:
        parts.append(f"state_auto_pick=1 自动选的 event:{reduction.auto_picked}（只是建议）")
    parts.append(f"证据链：{refs or '无'}")
    return "；".join(parts)


# ---- 第三条出口的数据源：为什么是这个值 / 为什么判冲突 ------------------------


@dataclass(frozen=True)
class KeyExplanation:
    """`why` 的机械判据来源（第三条出口，§15.6；CLI 是 T14 的事）。"""

    key: ResolvedKey
    events: tuple[Event, ...]
    reduction: Reduction
    current: StateRow | None

    def as_label(self) -> str:
        """带标签的一行：不确定的值**必须带标签**进上下文（§15.6）。"""
        if self.reduction.status == "conflict":
            refs = ", ".join(
                f"event:{event_id}" for event_id in self.reduction.conflict_events
            )
            return (
                f"{self.key.dotted} = ?（未定：{self.reduction.conflict_count}"
                f" 条互斥事件，见 {refs}）"
            )
        if self.reduction.status == "needs_review":
            return (
                f"{self.key.dotted} = ?（未定：{self.reduction.reason}"
                f"，见 {', '.join(self.reduction.evidence_refs()) or '无事件'}）"
            )
        unit = f" {self.reduction.unit}" if self.reduction.unit else ""
        return f"{self.key.dotted} = {self.reduction.value}{unit}"

    def why(self) -> dict:
        """结构化回答"为什么是这个值 / 为什么判冲突"（含参与判定的 event id）。"""
        events = self.reduction.sorted_events
        return {
            "key": self.key.dotted,
            "reducer": self.reduction.reducer,
            "status": self.reduction.status,
            "reason": self.reduction.reason,
            "value": self.reduction.value,
            "unit": self.reduction.unit,
            "derived_from": list(self.reduction.derived_from),
            "skipped_retracted": list(self.reduction.skipped_retracted),
            "auto_picked": self.reduction.auto_picked,
            "conflict": {
                "reason": self.reduction.conflict_note,
                "events": list(self.reduction.conflict_events),
                "values": {
                    str(event.id): event.value
                    for event in events
                    if event.id in self.reduction.conflict_events
                },
            },
            "events": [
                {
                    "ref": f"event:{event.id}",
                    "at_abs": event.at_abs,
                    "tz_offset_min": event.tz_offset_min,
                    "time_precision": event.time_precision,
                    "op": event.op,
                    "value": event.value,
                    "unit": event.unit,
                    "raw_text": event.raw_text,
                    "speaker": event.speaker,
                    "status": event.status,
                }
                for event in events
            ],
        }

    def render(self) -> str:
        """给人看的多行形态（§15.1 的 `state show` 形制；CLI 在 T14 接）。"""
        lines = [self.as_label()]
        lines.append(
            f"reducer={self.reduction.reducer}  status={self.reduction.status}"
            f"  reason={self.reduction.reason or '-'}"
            f"  derived_from={list(self.reduction.derived_from)}"
        )
        if self.reduction.conflict_note:
            lines.append(f"  {self.reduction.conflict_note}")
        if self.reduction.skipped_retracted:
            lines.append(
                "  已软撤销（不进归约、仍在证据链）："
                + ", ".join(
                    f"event:{event_id}" for event_id in self.reduction.skipped_retracted
                )
            )
        for event in self.reduction.sorted_events:
            lines.append("  " + _render_event(event))
        return "\n".join(lines)


def explain_key(conn, entity: str, attribute: str) -> KeyExplanation:
    """把"候选事件（有序）、用了哪个 reducer、为什么是 conflict/needs_review"一次说清。"""
    key = registry.resolve_key(conn, entity, attribute)
    events = tdb.fetch_events(conn, key.entity, key.attribute)
    reduction = reduce_events(events, key.reducer)
    current_row = tdb.fetch_current_state(conn, key.entity, key.attribute)
    current = None if current_row is None else tdb.to_state_row(current_row)
    return KeyExplanation(key=key, events=events, reduction=reduction, current=current)


def _render_event(event: Event) -> str:
    if event.at_abs is None:
        stamp = "时间未知"
    else:
        stamp = local_dt(event.at_abs, event.tz_offset_min).strftime("%Y-%m-%d %H:%M %z")
    unit = f" {event.unit}" if event.unit else ""
    return (
        f"event:{event.id}  {stamp}  {event.op:<6} {event.value}{unit}"
        f"  「{event.raw_text}」  {event.speaker or '-'}"
    )
