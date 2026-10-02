"""时序六表的行类型与取值口径（设计稿 §15.1 / §15.2 / §15.3 / §15.4 / §15.6）。

这里**只有声明，没有逻辑**：DDL 在 `store.py` 的 `TIMELINE_SCHEMA`（P1 的建表口子
只有 `_MIGRATIONS` 一处），归约器 / 取代链 / 归档是 T9 与 T11 的事。本模块给三样：

- **取值枚举**：每张表允许的取值写死成常量。`EVENT_STATUSES` 只有
  `active` / `retracted`——§15.4 的「事件永不删除，纠错只走软撤销」就是这句话；
  `STATE_REASONS` 是 §15.1 / §15.6 点名的那五个 `reason`。
- **`FIELD_SPECS`**：六张表逐列的 DDL 口径（名字 / 类型 / NOT NULL / 默认值 /
  主键位次），与 `PRAGMA table_info` 的五元组同形。它与 `TIMELINE_SCHEMA` 是
  **两份独立的表示**：`tests/memory/test_schema_timeline.py` 拿 `table_info`
  与它逐列比对——`store.py` 那边或这里，任一处被顺手改坏都会红。这份「双记账」
  是故意的：一次建六张表，抄错一处就会长期错。
- **`Event` / `StateRow` / `AttributeSpec`** 三个 frozen dataclass：读取侧的行类型。
"""

from dataclasses import dataclass

# ---- 取值枚举（§15.1 / §15.3 / §15.4 / §15.5 / §15.6）----------------------

# `memory_event.op`：增量 / 新值 / 只是观察到（不改变值）。
EVENT_OPS = ("delta", "set", "observe")

# `memory_event.status`：只有这两个。事件永不删除，纠错走软撤销（§15.4）。
EVENT_STATUSES = ("active", "retracted")

# `memory_event.at_source`：绝对时间 / 相对词 / 本轮 turn / 解析失败（§15.5 表）。
AT_SOURCES = ("explicit", "relative", "turn", "unknown")

# `memory_event.time_precision`：精度不同，排序与冲突判定规则不同（§15.6 第 3 条）。
TIME_PRECISIONS = ("exact", "minute", "day", "turn", "unknown")

# `memory_event.speaker`：来源优先级 user > tool > agent，**只用来给建议**（§15.6 第 3 条）。
SPEAKERS = ("user", "agent", "tool")

# `memory_state.status`：不确定时状态本身带着「我不确定」（§15.6 三条出口之一）。
STATE_STATUSES = ("active", "conflict", "needs_review")

# `memory_state.reason`：§15.1 点名的五个；缺基准时 `value` **留空**，不给猜的数。
STATE_REASONS = (
    "missing_base",
    "unit_mismatch",
    "same_instant",
    "no_reducer",
    "time_unknown",
)

# 归约器（§15.3）：按属性类型选，不由模型当轮决定；未注册 → `none`（不推）。
REDUCERS = ("sum_delta", "latest", "set", "max", "min", "none")

# `entity_attribute.type`：§15.3 对照表里点到的类型。注册表是开放的，出现表外的
# `type` 与「没有 type」同路：`status='needs_review'` + `reason='no_reducer'`。
ATTRIBUTE_TYPES = (
    "monetary",
    "count",
    "duration",
    "text",
    "enum",
    "status",
    "tags",
    "files",
    "best_score",
)


# ---- 列口径（与 `PRAGMA table_info` 同形，供用例逐列比对）-------------------


@dataclass(frozen=True)
class FieldSpec:
    """一列的 DDL 口径。

    `default` 是 DDL 原文里的默认值，形态与 SQLite 的 `dflt_value` 一致
    （字符串带引号：`"''"` / `"'active'"`；数字不带：`"1"` / `"0.5"`）。
    `pk` 是主键位次（0 = 不是主键），联合主键里第一列是 1、第二列是 2。
    """

    name: str
    type: str
    notnull: bool = False
    default: str | None = None
    pk: int = 0


_EVENT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("id", "INTEGER", False, None, 1),
    FieldSpec("entity", "TEXT", True),
    FieldSpec("attribute", "TEXT", True),
    FieldSpec("op", "TEXT", True),
    FieldSpec("value", "TEXT", True),  # 金额/数量用 TEXT，**不是 REAL**（§15.3 第 4 条）
    FieldSpec("unit", "TEXT", True, "''"),
    FieldSpec("at_abs", "REAL", False),  # 解析失败为 NULL（§15.5 第 4 行）
    FieldSpec("tz_offset_min", "INTEGER", True),  # 必填，且不给默认值（§15.5 硬约束 3）
    FieldSpec("tz_name", "TEXT", False),
    FieldSpec("at_source", "TEXT", True),
    FieldSpec("time_precision", "TEXT", True),
    FieldSpec("source_ref", "TEXT", True),
    FieldSpec("raw_text", "TEXT", True),
    FieldSpec("speaker", "TEXT", True, "''"),
    FieldSpec("confidence", "REAL", True, "0.5"),
    FieldSpec("content_hash", "TEXT", True),
    FieldSpec("status", "TEXT", True, "'active'"),
)

_STATE_FIELDS: tuple[FieldSpec, ...] = (
    # 自增主键：取代链 `superseded_by` 的引用目标（§15.4）。原设计是
    # `(entity, attribute)` 联合主键，与 §15.4 的「新旧两行并存」冲突，已就地改成
    # **双时态行 + 当前行唯一**（见 `store.py` 的 `TIMELINE_SCHEMA` 上方注释）。
    FieldSpec("id", "INTEGER", False, None, 1),
    FieldSpec("entity", "TEXT", True),
    FieldSpec("attribute", "TEXT", True),
    FieldSpec("value", "TEXT", False),  # 缺基准时留空（§15.6 第 1 条）
    FieldSpec("unit", "TEXT", True, "''"),
    FieldSpec("derived_from", "TEXT", True, "''"),
    FieldSpec("reducer", "TEXT", True, "'none'"),
    FieldSpec("valid_from", "REAL", True),  # NOT NULL：没有「不知道从哪生效」的行
    FieldSpec("valid_to", "REAL", False),  # NULL = 至今有效（§15.4）
    FieldSpec("superseded_by", "INTEGER", False),  # REFERENCES memory_state(id)
    FieldSpec("confidence", "REAL", True, "0.5"),
    FieldSpec("status", "TEXT", True, "'active'"),
    FieldSpec("reason", "TEXT", True, "''"),
    FieldSpec("computed_at", "REAL", True),
    FieldSpec("is_derived", "INTEGER", True, "1"),  # DDL 默认 1，恒为真（§15.1）
)

_ENTITY_ATTRIBUTE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("entity", "TEXT", True, None, 1),
    FieldSpec("attribute", "TEXT", True, None, 2),
    FieldSpec("type", "TEXT", False),
    FieldSpec("reducer", "TEXT", False),
)

_ENTITY_ALIAS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("alias", "TEXT", True, None, 1),  # 唯一主键 = 一个 alias 只指一个 canonical
    FieldSpec("canonical", "TEXT", True),
    FieldSpec("lang", "TEXT", True, "''"),
    FieldSpec("scope", "TEXT", True, "'workspace'"),
    FieldSpec("created_at", "REAL", True),
    FieldSpec("source", "TEXT", True, "''"),
)

_ATTRIBUTE_ALIAS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("alias", "TEXT", True, None, 1),
    FieldSpec("canonical", "TEXT", True),
    FieldSpec("lang", "TEXT", True, "''"),
    FieldSpec("scope", "TEXT", True, "'workspace'"),
    FieldSpec("created_at", "REAL", True),
    FieldSpec("source", "TEXT", True, "''"),
)

# 六张表逐列的口径。归档表与 `memory_event` 同形（`id` 保持原值）。
FIELD_SPECS: dict[str, tuple[FieldSpec, ...]] = {
    "memory_event": _EVENT_FIELDS,
    "memory_state": _STATE_FIELDS,
    "entity_attribute": _ENTITY_ATTRIBUTE_FIELDS,
    "entity_alias": _ENTITY_ALIAS_FIELDS,
    "attribute_alias": _ATTRIBUTE_ALIAS_FIELDS,
    "memory_event_archive": _EVENT_FIELDS,
}


# ---- 行类型（读取侧用；写入侧的口径由 T9 的归约器与 T7 的门禁定）------------


@dataclass(frozen=True)
class Event:
    """`memory_event` 的一行：某个时刻的一条原始观察。"""

    id: int
    entity: str
    attribute: str
    op: str
    value: str
    tz_offset_min: int
    at_source: str
    time_precision: str
    source_ref: str
    raw_text: str
    content_hash: str
    unit: str = ""
    at_abs: float | None = None
    tz_name: str | None = None
    speaker: str = ""
    confidence: float = 0.5
    status: str = "active"


@dataclass(frozen=True)
class StateRow:
    """`memory_state` 的一行：某个 `(entity, attribute)` 由事件链推出的值。

    `is_derived` 默认 1 且**恒为真**：state 只由确定性归约器写（§15.1）。
    `id` 是库里的自增主键（取代链 `superseded_by` 指向它，§15.4）：刚算出来、
    还没落库的行没有 id，所以它可空且排在最后——不给构造方添必填参数。
    """

    entity: str
    attribute: str
    value: str | None
    valid_from: float
    computed_at: float
    unit: str = ""
    derived_from: str = ""  # JSON：有序 event id 列表
    reducer: str = "none"
    valid_to: float | None = None
    superseded_by: int | None = None
    confidence: float = 0.5
    status: str = "active"
    reason: str = ""
    is_derived: int = 1
    id: int | None = None  # 库里的主键；未落库时为 None


@dataclass(frozen=True)
class AttributeSpec:
    """`entity_attribute` 的一行：键注册表里的一条。

    `type` / `reducer` 都可空——「没有 type」与「没有这一行」同路：走
    `needs_review` + `reason='no_reducer'`，**不许默认套 `sum_delta`**（§15.6 第 5 条）。
    """

    entity: str
    attribute: str
    type: str | None = None
    reducer: str | None = None
