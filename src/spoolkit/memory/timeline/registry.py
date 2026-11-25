"""键注册表与别名映射：`entity.attribute` 的入口（设计稿 §15.2 + 计划 T9）。

三件事：

1. **键规范化只在写入侧做一次**（§15.2 第 4 行）：`normalize_segment` 是全包唯一的
   规范化函数——检索侧**不允许**临时猜别名，否则同一份数据两次检索会落到两个键上。
2. **注册表决定用哪个归约器**（§15.3）：`entity_attribute.type` → `REDUCER_BY_TYPE`；
   表里写了 `reducer` 就听它的。**没有这一行 / 没有 type / type 在表外** → 一律
   `reducer='none'` + `reason='no_reducer'`——**不许默认套 `sum_delta`**（§15.6 第 5 条）。
3. **别名冲突拒绝合并**（§15.2 第 3 行）：一个 `alias` 只能指一个 `canonical`。
   已被占用、又想来指**另一个** canonical 时：**拒绝**（不静默挑一个）、写
   `memory_audit(action='alias_conflict')`（`before` = 原有 canonical、`after` = 被拒的
   那个 → **两条都留**）、并让**新写入**带 `status='needs_review'`。
   强行改只有一条路：`move_alias(force=True)`，它**必须写 audit**。

本模块只做"键的入口"这件事：值怎么算在 `reduce.py`，行怎么读写在 `db.py`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from spoolkit.memory.timeline import db as tdb
from spoolkit.memory.timeline.clock import Clock
from spoolkit.memory.timeline.models import REDUCERS, AttributeSpec

# §15.3 的对照表：属性类型 → 归约器。表中没有的类型**不推**（`none`），
# 也不给"看起来像"的默认值——`best_score` → `max` 是表里写的；`min` 只在显式登记时用。
REDUCER_BY_TYPE: dict[str, str] = {
    "monetary": "sum_delta",
    "count": "sum_delta",
    "duration": "sum_delta",
    "text": "latest",
    "enum": "latest",
    "status": "latest",
    "tags": "set",
    "files": "set",
    "best_score": "max",
}

_KEY_AUDIT_PREFIX = "entity_attribute"


def normalize_segment(segment: str) -> str:
    """把键的一段压成 §15.2 的形状：全小写、下划线分隔、无空格、最多两段。

    归一化**只在写入侧做一次**（本函数是全包唯一的定义处）。中文不受 `lower()` 影响。
    """
    text = (segment or "").strip().lower()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"-+", "_", text)
    text = text.strip("_")
    if not text:
        raise ValueError("键的一段不能是空串（§15.2：entity 用单数名词、attribute 用名词）")
    if "." in text:
        raise ValueError(f"键最多两段，段里不许再有 '.'：{segment!r}")
    return text


def split_key(dotted: str) -> tuple[str, str]:
    """把 `wallet.balance` 拆成两段；**不是两段就报错**，不猜。"""
    parts = (dotted or "").split(".")
    if len(parts) != 2:
        raise ValueError(f"键必须是 entity.attribute 两段：{dotted!r}")
    return (normalize_segment(parts[0]), normalize_segment(parts[1]))


@dataclass(frozen=True)
class ResolvedKey:
    """一次键解析的结果：别名映射后的 canonical + 该用哪个归约器。"""

    entity: str
    attribute: str
    type: str | None
    reducer: str  # sum_delta|latest|set|max|min|none
    registered: bool
    reason: str = ""  # "" 或 no_reducer
    entity_alias: str | None = None  # 命中的实体别名（归一化后），没命中为 None
    attribute_alias: str | None = None

    @property
    def dotted(self) -> str:
        return f"{self.entity}.{self.attribute}"


@dataclass(frozen=True)
class AliasResult:
    """一次别名写入的结果。

    冲突时 `ok=False` + `conflict=True` + `status='needs_review'`：
    **新写入该带的标记**由调用方落到它自己那行上（`memory_event.status` 只有
    `active` / `retracted`，所以这个标记落到 state 行与 audit 上，§15.2 / §15.6）。
    """

    ok: bool
    kind: str
    alias: str
    canonical: str
    existing: str | None = None
    conflict: bool = False
    forced: bool = False
    status: str = "active"
    reason: str = ""


@dataclass(frozen=True)
class RegisterResult:
    """一次注册表写入的结果（同一个键想换成另一个 type/reducer 也要**拒绝合并**）。"""

    ok: bool
    spec: AttributeSpec
    conflict: bool = False
    forced: bool = False
    existing: AttributeSpec | None = None
    reason: str = ""


def resolve_alias(conn, kind: str, text: str) -> tuple[str, str | None]:
    """查别名表。返回（canonical，命中的 alias）；没命中就返回（原文，None）。"""
    segment = normalize_segment(text)
    row = tdb.get_alias(conn, kind, segment)
    if row is None:
        return (segment, None)
    return (row["canonical"], segment)


def resolve_key(
    conn,
    entity: str,
    attribute: str,
    *,
    force_reducer: str | None = None,
) -> ResolvedKey:
    """把用户说的两段解析成 canonical 键 + 归约器。**写入侧的唯一入口。**"""
    canonical_entity, entity_alias = resolve_alias(conn, "entity", entity)
    canonical_attribute, attribute_alias = resolve_alias(conn, "attribute", attribute)
    spec = tdb.get_attribute(conn, canonical_entity, canonical_attribute)
    type_ = spec.type if spec is not None else None
    if force_reducer is not None:
        explicit = force_reducer
    else:
        explicit = spec.reducer if spec is not None else None
    if explicit is not None and explicit not in REDUCERS:
        explicit = None
    reducer = explicit or REDUCER_BY_TYPE.get(type_ or "", "none")
    if reducer not in REDUCERS:
        reducer = "none"
    return ResolvedKey(
        entity=canonical_entity,
        attribute=canonical_attribute,
        type=type_,
        reducer=reducer,
        registered=spec is not None,
        reason="" if reducer != "none" else "no_reducer",
        entity_alias=entity_alias,
        attribute_alias=attribute_alias,
    )


def resolve_dotted(conn, dotted: str, *, force_reducer: str | None = None) -> ResolvedKey:
    entity, attribute = split_key(dotted)
    return resolve_key(conn, entity, attribute, force_reducer=force_reducer)


def register_attribute(
    conn,
    entity: str,
    attribute: str,
    *,
    type: str | None = None,
    reducer: str | None = None,
    clock: Clock,
    force: bool = False,
    actor: str = "cli",
) -> RegisterResult:
    """注册（或改）一个键的 type / reducer。**同一个键换算子也要显式 `force`。**

    `reducer` 必须是 §15.3 表里的六个之一（`none` 可以显式写）；`type` 是**开放集**：
    表外的 type 与"没有 type"同路（`no_reducer`），因为设计稿没定那些类型的算子。
    """
    if reducer is not None and reducer not in REDUCERS:
        raise ValueError(f"没有登记过这个归约器：{reducer!r}（§15.3 只有 {REDUCERS}）")
    canonical_entity, _ = resolve_alias(conn, "entity", entity)
    canonical_attribute, _ = resolve_alias(conn, "attribute", attribute)
    wanted = AttributeSpec(
        entity=canonical_entity,
        attribute=canonical_attribute,
        type=type,
        reducer=reducer,
    )
    now = clock.now()
    target = f"{_KEY_AUDIT_PREFIX}:{wanted.entity}.{wanted.attribute}"
    with tdb.transaction(conn):
        existing = tdb.get_attribute(conn, wanted.entity, wanted.attribute)
        if existing is None:
            tdb.upsert_attribute(conn, wanted.entity, wanted.attribute, type, reducer)
            tdb.audit(
                conn,
                at=now,
                actor=actor,
                action="attr_add",
                target=target,
                after=_spec_text(wanted),
                reason="注册表新增：该键从此按登记的 type/reducer 归约",
            )
            return RegisterResult(True, wanted, reason="新增")
        if (existing.type, existing.reducer) == (type, reducer):
            return RegisterResult(True, existing, reason="已登记且一致（幂等）")
        if not force:
            tdb.audit(
                conn,
                at=now,
                actor=actor,
                action="attr_conflict",
                target=target,
                before=_spec_text(existing),
                after=_spec_text(wanted),
                reason="同一个键想换成另一个 type/reducer：拒绝合并，旧登记保留、"
                "两条都留在 audit 里；要改必须显式 force",
            )
            return RegisterResult(
                False,
                existing,
                conflict=True,
                existing=existing,
                reason="注册表冲突：拒绝合并（不许静默挑一个）",
            )
        tdb.upsert_attribute(conn, wanted.entity, wanted.attribute, type, reducer)
        tdb.audit(
            conn,
            at=now,
            actor=actor,
            action="attr_change",
            target=target,
            before=_spec_text(existing),
            after=_spec_text(wanted),
            reason="显式 force 改登记：旧值留在 audit 里",
        )
        return RegisterResult(
            True, wanted, forced=True, existing=existing, reason="force 改写"
        )


def add_alias(
    conn,
    alias: str,
    canonical: str,
    *,
    kind: str,
    clock: Clock,
    lang: str = "",
    scope: str = "workspace",
    source: str = "user",
    actor: str = "cli",
    force: bool = False,
) -> AliasResult:
    """加一条别名映射。**冲突就拒绝合并**：两条都留（原行不动 + audit 记着被拒的那个）。

    `canonical` 可以是裸段（`wallet` / `balance`），也可以照 CLI 的写法给全键
    （`wallet.balance`）——那时取与 `kind` 对应的那一段。
    """
    kind = _check_kind(kind)
    alias_norm = normalize_segment(alias)
    canonical_norm = _canonical_segment(kind, canonical)
    now = clock.now()
    target = f"{kind}_alias:{alias_norm}"
    with tdb.transaction(conn):
        existing = tdb.get_alias(conn, kind, alias_norm)
        if existing is None:
            tdb.insert_alias(
                conn,
                kind,
                alias=alias_norm,
                canonical=canonical_norm,
                lang=lang,
                scope=scope,
                created_at=now,
                source=source,
            )
            tdb.audit(
                conn,
                at=now,
                actor=actor,
                action="alias_add",
                target=target,
                after=canonical_norm,
                reason=f"来源={source}（P1 只收用户显式与人确认过的候选，§15.2）",
            )
            return AliasResult(True, kind, alias_norm, canonical_norm, reason="新增")
        if existing["canonical"] == canonical_norm:
            return AliasResult(
                True,
                kind,
                alias_norm,
                canonical_norm,
                existing=existing["canonical"],
                reason="已存在且指向同一个 canonical（幂等）",
            )
        # 冲突：拒绝合并。原行**一个字节都不动**，被拒的那个 canonical 留在 audit 的
        # after 栏里——这就是"两条都留"（§15.2 第 3 行）。
        if not force:
            tdb.audit(
                conn,
                at=now,
                actor=actor,
                action="alias_conflict",
                target=target,
                before=existing["canonical"],
                after=canonical_norm,
                reason="同一个 alias 想指另一个 canonical：拒绝合并，两条都留，"
                "新写入标 needs_review；要改必须 alias move --force",
            )
            return AliasResult(
                False,
                kind,
                alias_norm,
                existing["canonical"],
                existing=existing["canonical"],
                conflict=True,
                status="needs_review",
                reason=f"别名冲突：{alias_norm!r} 已指 {existing['canonical']!r}，"
                f"拒绝改指 {canonical_norm!r}（不许静默挑一个）",
            )
        tdb.update_alias(
            conn,
            kind,
            alias=alias_norm,
            canonical=canonical_norm,
            source=f"{source}:force",
        )
        tdb.audit(
            conn,
            at=now,
            actor=actor,
            action="alias_move",
            target=target,
            before=existing["canonical"],
            after=canonical_norm,
            reason="显式 force 改指：旧 canonical 留在 audit 里",
        )
        return AliasResult(
            True,
            kind,
            alias_norm,
            canonical_norm,
            existing=existing["canonical"],
            forced=True,
            reason="force 改指",
        )


def move_alias(
    conn,
    alias: str,
    canonical: str,
    *,
    kind: str,
    clock: Clock,
    force: bool = False,
    actor: str = "cli",
    source: str = "user",
) -> AliasResult:
    """`alias move`：**不给 `force` 就走"拒绝合并"那条路**，写 `alias_conflict`；
    给了 `force` 才改，并且**必须写 audit**（§15.2 第 3 行）。"""
    return add_alias(
        conn,
        alias,
        canonical,
        kind=kind,
        clock=clock,
        source=source,
        actor=actor,
        force=force,
    )


def _check_kind(kind: str) -> str:
    if kind not in ("entity", "attribute"):
        raise ValueError(f"别名只有两个命名空间：entity / attribute，不是 {kind!r}")
    return kind


def _canonical_segment(kind: str, canonical: str) -> str:
    """canonical 允许给裸段或全键；给全键时取与命名空间对应的那一段。

    （CLI 的 `alias add 余额 wallet.balance` 就是全键写法；实体表存 `wallet`、
    属性表存 `balance`——两张表是两个命名空间，§15.2。）
    """
    text = (canonical or "").strip()
    if "." not in text:
        return normalize_segment(text)
    entity, attribute = split_key(text)
    return entity if kind == "entity" else attribute


def _spec_text(spec: AttributeSpec) -> str:
    return f"type={spec.type or ''} reducer={spec.reducer or ''}"
