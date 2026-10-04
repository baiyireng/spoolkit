"""记忆会话。

把记忆的读写收敛成一个对象，主循环只需要调用它，不需要知道
热记忆文件、冷记忆库和容量规则的存在。

**T11（设计稿 §6.7）：可变数量类问题的检索单元是「事件链」**。「现在还剩多少」
这类问题的答案**不在任何一条记录里**——它是若干条事件的函数。取"最相似的一条"
取回来的多半是「还剩 80」那句，而它已经过期（数字对得上、时间不对）；把它切成
互不相关的 N 个片段也一样错（模型只能在「100 / −20 / +50」之间猜）。

所以本模块把同一个 `entity.attribute` 的**有序事件链**做成**一个整体单元**
（`unit='event_chain(N)'`，N = 链上事件数）送进候选池：

- **一个候选，不是 N 个**（§6.7 的流程图）：链上事件**不会**各自成为一个候选；
- **整链进 / 整链出**：链被选中就整条渲染进上下文（受 `event_chain_render_max_chars`
  约束，超了**明确截断并标注被略去的 `event:id`**，不静默丢事件）；被淘汰就整条消失；
- **长链不加分**：链长不进任何分量——`rel` 取链内最高（**不是 Σ**，见
  `rank._chain_rel_part`），`imp` 取链内最高 × 时序完整度，`rec` 用**链内最新事件**，
  链上没有 `use_count`/负反馈数据源所以 `use`/`penalty` 如实为 0；
- **链的 `rel` 有真数据源**（T11 补缺轮）：`memory_event_fts`（`store.py` 的 v5 步）
  给出每条事件的 `bm25()` 原值，本模块在 `_build_chain` 里按**问句**取好、一条条填进
  `chain_members[].bm25_raw`。所以链在生产路径上真的参与相关性排序（T11 交回来时
  链的 `rel` 恒为 0——那是缺口，不是口径）；
- **链含已被取代的旧行**（§15.4：取代不是删除）：`memory_state` 的全部历史行与全部
  `memory_event`（含 `status='retracted'`）都在链上，`rank` 也因此**不过滤**链单元的
  `superseded`；
- **冲突不静默选一个**（§15.6）：`status='conflict'` 的链渲染带「未定」+ 互斥
  `event:id`，**不注入单个数**。

链的构造、选键与渲染都收在本模块（`build_event_chains` / `render_chain`）；
`rank.py` 只负责"链单元的五个分量怎么算"。事件链把它从"一次取一条、取几轮凑"
变成"一次取一个结论"——**减少的是轮次而不是并发**（§6.7 末段）。
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from spoolkit import limits
from spoolkit.agent.state import TaskState
from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.memory.archive import ArchiveResult, archive_task
from spoolkit.memory.hot import hot_budget, read_hot, render_hot, trim_to_budget
from spoolkit.memory.rank import (
    Candidate,
    ChainMember,
    RankedResult,
    chain_unit_label,
    rank,
)
from spoolkit.memory.store import (
    Memory,
    event_bm25_raws,
    normalize_for_fts,
    search_episodes,
    search_memories,
    touch_memory,
)
from spoolkit.memory.timeline.clock import Clock, SystemClock, local_dt
from spoolkit.memory.timeline.db import (
    derived_ids,
    fetch_current_state,
    fetch_events,
    fetch_state_rows,
)
from spoolkit.memory.timeline.models import Event
from spoolkit.memory.timeline.reduce import reduce_events
from spoolkit.memory.timeline.registry import resolve_key
from spoolkit.memory.transcript import MessageHit, prompt_stamp, search_messages

# ---- §6.7 的机械判据：什么算"可变数量类问题" ---------------------------------

# ① 疑问词（§6.7 逐字给的五个 + 口径上同类的几个：`多少`/`几`/`还剩`/`现在`/`目前`，
#    再加上"剩下 / 一共 / 总共"——它们问的是同一个东西：一个会随事件变的量）。
INTERROGATIVE_MARKERS: tuple[str, ...] = (
    "多少",
    "几",
    "还剩",
    "剩下",
    "现在",
    "目前",
    "一共",
    "总共",
)

# ③ 时间副词（§6.7 逐字给的 `到今天`/`目前`，加上同类的几个）。
TIME_ADVERB_MARKERS: tuple[str, ...] = (
    "到今天",
    "到现在",
    "到目前为止",
    "目前",
    "截止",
)

# 事件侧"逐字召回"的命中门槛：至少命中的**不同** token 数。
# 为什么要 ≥2：逐字口径下「现在还剩多少」被拆成 `现 在 还 剩 多 少`，单个「在」
# 几乎每句话里都有——只命中一个字就建链会把链单元灌满候选池。两个字符的命中
# （例如事件「还剩 80」命中 `还`+`剩`）才有区分度。
# **这是候选选择判据，不是打分**：打分只有 `rank.py` 的五维一条路。
# **值本身不在本模块**（T11 补缺轮）：它是可覆盖旋钮
# `event_chain_min_matched_tokens`（登记在 `limits.py`，依据写在那一项的 note 里），
# 这里读默认值只为给读代码的人一个入口；真正生效的值由
# `_keys_recalled_by_events` 每次 `limits.resolve` 出来（所以 `.agent/limits.json`
# 能改它）。T11 之前它是写死的 `2`——阈值不许写死，所以补登。
MIN_MATCHED_TOKENS = int(limits.resolve("event_chain_min_matched_tokens")[0])


def _hits_any(query: str, markers: Sequence[str]) -> bool:
    """问句里有没有这几个字面量之一（§6.7 判据的 ① / ③ 都是这个动作）。"""
    return any(marker in query for marker in markers)


def is_variable_quantity(query: str, keys: Sequence[tuple[str, str]]) -> bool:
    """§6.7 的机械判据：① 疑问词命中 **且**（② 命中一个已注册键 **或** ③ 时间副词命中）。

    `keys` 是**这一问召回到了的**已注册键（`select_chains` 的产物）：
    "问句命中一个已注册的 `entity.attribute` 键"在实现上就是这一步——问句的逐字
    召回把哪个键的**事件**带回来了（或问句直接说出了键/它的别名）。
    """
    if not _hits_any(query, INTERROGATIVE_MARKERS):
        return False
    return bool(keys) or _hits_any(query, TIME_ADVERB_MARKERS)


@dataclass(frozen=True)
class ChainSelection:
    """§6.7 判据的读数：哪些键要造链、判据本身成立不成立。

    留这个对象是为了让"为什么没有链单元"可回答（不是静默地什么都不做）。
    """

    keys: tuple[tuple[str, str], ...]
    variable_quantity: bool

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(f"event_chain:{entity}.{attribute}" for entity, attribute in self.keys)

    def note(self) -> str:
        if not self.variable_quantity:
            return (
                "不是可变数量类问题（§6.7 的机械判据没同时命中：疑问词 且 键/时间副词）"
                "：不造链单元，按单条召回"
            )
        if not self.keys:
            return "判据命中（疑问词 + 时间副词）但没有已注册键被召回：不造链单元"
        return "可变数量类问题：链单元 " + ", ".join(self.refs)


def select_chains(
    conn: sqlite3.Connection,
    query: str,
    *,
    overrides: Mapping[str, float] | None = None,
) -> ChainSelection:
    """选出要造链的**已注册键**，并给出判据读数（设计稿 §6.7 / §15.2）。

    两步，都只看"能不能把键和问句对上"，**不打分**：

    1. 问句**直接说出**了键：`entity`（或它的别名）与 `attribute`（或它的别名）
       都出现在问句里，或者问句里就有 `entity.attribute` 这个点号写法。
       命中就直接用这一批——问句点名了键，就不该再把别的键塞进候选。
    2. 否则按**逐字召回**看哪个键的**事件**被问句带回来了（`memory_event` 上的
       逐字匹配，与 `search_episodes` 在事件表上用 LIKE 是同一处置：表小、
       中文子串匹配天然可用）。命中门槛是可覆盖旋钮
       `event_chain_min_matched_tokens`（`overrides` 透传给 `limits.resolve`）。
    """
    keys = _registered_keys(conn)
    if not keys:
        return ChainSelection((), False)
    # 别名表读一次就够（每个键都去查一遍会把同一张表读 N 次）。
    aliases = _aliases(conn)
    named = tuple(key for key in keys if _key_named(conn, key, query, aliases))
    selected = named or _keys_recalled_by_events(conn, query, keys, overrides)
    return ChainSelection(selected, is_variable_quantity(query, selected))


def build_event_chains(
    conn: sqlite3.Connection,
    query: str,
    *,
    overrides: Mapping[str, float] | None = None,
) -> tuple["EventChain", ...]:
    """把 `select_chains` 选中的键各造成**一个**链单元（§6.7：1 个候选，不是 N 个）。"""
    selection = select_chains(conn, query, overrides=overrides)
    if not selection.variable_quantity:
        return ()
    return tuple(
        _build_chain(conn, entity, attribute, query=query, overrides=overrides)
        for entity, attribute in selection.keys
    )


@dataclass(frozen=True)
class EventChain:
    """一个 `(entity, attribute)` 的**有序事件链单元**（§6.7：1 个候选，不是 N 个）。

    - `events` 按 `at_abs` 升序，`at_abs is None`（时间解析失败）的排在已解析事件
      **之后**（§15.5 末段）；含 `status='retracted'` 的软撤销事件（§15.4：事件永不删除）。
    - `state_rows` 是**全部** state 行（含已被取代的历史行，按 `valid_from` 升序）；
      `current_state` 是 `valid_to IS NULL` 的那一行（部分唯一索引保证至多一条）。
    - `complete` / `incomplete_reason` 是**时序完整度**：`derived_from` 是否覆盖写链、
      有没有当前值、状态是否未定（§6.7 表 / §15.6）。缺基准时**扣分并标记**，
      绝不假装完整。
    - `bm25_raws` 是**事件侧的 `bm25()` 原值**（`event_id → raw`，只含命中问句的
      事件；T11 补缺轮）：`chain_members[].bm25_raw` 与 `Candidate.bm25_raw`
      都由它来。没命中的事件**没有原值**（缺席，不是 0），聚合时只对有的取最高。
    - `render` 是**整链**的渲染文本（已按 `event_chain_render_max_chars` 截断并标注）。
    """

    entity: str
    attribute: str
    events: tuple[Event, ...]
    state_rows: tuple[sqlite3.Row, ...]
    current_state: sqlite3.Row | None
    importance: float
    complete: bool
    incomplete_reason: str
    conflict_event_ids: tuple[int, ...]
    render: str
    truncated: bool
    bm25_raws: Mapping[int, float] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.entity}.{self.attribute}"

    @property
    def ref(self) -> str:
        return f"event_chain:{self.key}"

    @property
    def size(self) -> int:
        """链上事件数 = `event_chain(N)` 里的 N。"""
        return len(self.events)

    @property
    def event_ids(self) -> tuple[int, ...]:
        return tuple(event.id for event in self.events)

    @property
    def latest_at(self) -> float:
        """链内最新时刻（事件 `at_abs` 与 state 行 `valid_from` 的最大值）。

        它给 `rank` 的 `rec` 用：新近度看的是"这个键最近被更新过没有"，
        **不是链上攒了多少条**（长度不许变成加分）。
        """
        stamps = [float(event.at_abs) for event in self.events if event.at_abs]
        stamps.extend(float(row["valid_from"]) for row in self.state_rows)
        return max(stamps) if stamps else 0.0

    def to_candidate(self) -> Candidate:
        """变成 `rank` 认的候选：**一个**链单元，链上事件清单挂在 `chain_members` 上。

        四处刻意的取值：

        - `chain_members[].bm25_raw` = 该事件在**本次问句**下的 `bm25()` 原值
          （来自 `memory_event_fts`，见 `bm25_raws`）；没命中的那条如实为 `None`。
          `rank._chain_rel_part` 只对**有原值**的成员取最高——链的 `rel` 因此
          在生产路径上不再是 0（T11 补缺轮之前恒为 0，那是缺口）；
        - `bm25_raw` = **链内最高**那条的原值（`None` = 一条都没命中）。它同时是
          第①阶段序（`rank.order_by_bm25`）的输入：链现在真的按相关度参与候选池排序，
          而不是永远排在"没有原值"的那一档；
        - `verified=False`：链单元**不领** `IMPORTANCE_VERIFIED_BONUS`。链越长越容易
          "取代过别人"，那 `+0.1` 就是变相的长度加分（§6.7：长链不加分）；
        - `created_at=latest_at` + `recency_note`：`rec` 用链内**最新事件**，并把
          这件事写进 reason（不是 `created_at` 这种看不出是链的默认说法）。
        """
        members = tuple(
            ChainMember(
                event_id=event.id,
                bm25_raw=self.bm25_raws.get(event.id),
                at_abs=event.at_abs,
            )
            for event in self.events
        )
        best_raw = min(self.bm25_raws.values()) if self.bm25_raws else None
        return Candidate(
            ref=self.ref,
            # 进上下文的就是这段渲染文本：**整链**（超长时带截断标注）。
            text=self.render,
            kind="event_chain",
            unit=chain_unit_label(self.size),
            bm25_raw=best_raw,
            importance=self.importance,
            verified=False,
            created_at=self.latest_at,
            last_used_at=None,
            status=(
                self.current_state["status"]
                if self.current_state is not None
                else "needs_review"
            ),
            superseded_by=None,
            dedupe_key=f"event_chain:{self.key}",
            chain_members=members,
            complete=self.complete,
            incomplete_reason=self.incomplete_reason,
            recency_note="链内最新事件 at_abs",
        )

    def explain(self) -> str:
        """链单元的为什么：**由哪几条事件组成**（`event:id` 列表）+ 完整度与冲突。"""
        listed = " ".join(f"event:{event_id}" for event_id in self.event_ids)
        head = (
            f"unit={chain_unit_label(self.size)} 键 {self.key} 由 {self.size} 条事件组成"
            f"（整链进/出，一个单元不是 {self.size} 个候选）：{listed}"
        )
        if not self.complete:
            head += f"；时序完整度不足：{self.incomplete_reason}"
        if self.conflict_event_ids:
            head += "；冲突未定，互斥事件 " + ", ".join(
                f"event:{event_id}" for event_id in self.conflict_event_ids
            )
        if self.truncated:
            head += "；渲染已按 event_chain_render_max_chars 截断（被略去的事件在上面标着）"
        return head


@dataclass(frozen=True)
class RecallOutcome:
    """一次 `recall` 的**完整读数**（T11；T13 的三个出口读同一份数据）。

    `ranked` 是两阶段结果（`chosen` / `dropped` / `parts` / `reasons` / `explain()`），
    `rendered` 是 ref → 进上下文的文本（单条一行；链单元是**整链**），
    `selection` 是 §6.7 判据的读数，`chains` 是造出来的链单元（含事件清单）。
    """

    query: str
    now: float
    selection: ChainSelection
    chains: tuple[EventChain, ...]
    ranked: RankedResult
    rendered: Mapping[str, str]

    @property
    def variable_quantity(self) -> bool:
        return self.selection.variable_quantity

    @property
    def text(self) -> str:
        """进上下文的最终文本：按重排序取 `chosen`（链单元整条在里面）。"""
        return "\n".join(
            self.rendered[ref] for ref in self.ranked.chosen_refs if ref in self.rendered
        )

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        """候选池全量（选中的 + 被淘汰的）——"候选里是 1 个 `event_chain(3)`"就看它。"""
        return tuple(item.candidate for item in self.ranked.chosen) + tuple(
            item.candidate for item in self.ranked.dropped
        )

    def chain_for(self, ref: str) -> EventChain | None:
        for chain in self.chains:
            if chain.ref == ref:
                return chain
        return None


class MemorySession:
    """一次会话的记忆读写入口。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        hot_path: Path,
        counter: TokenCounter,
        context_window: int,
        session_id: str = "default",
        overrides: dict | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._conn = conn
        self._hot_path = hot_path
        self._counter = counter
        self._window = context_window
        self.session_id = session_id
        # 热记忆的占比是能力标定值：装配层和裁剪层必须从同一个出处取。
        self._overrides = overrides or {}
        # 排序要"现在"（`rec` 的 age_days）。会话自己不取真实时间：用注入的时钟
        # （生产是 `SystemClock`，用例是 `FakeClock`）——`rank` 因此可复现。
        self._clock: Clock = clock or SystemClock()

    def hot_text(self) -> str:
        """返回本轮应当注入的热记忆文本，已按容量裁剪。"""
        sections = read_hot(self._hot_path)
        if not sections:
            return ""
        kept, _ = trim_to_budget(
            sections,
            self._counter,
            hot_budget(self._window, overrides=self._overrides),
            overrides=self._overrides,
        )
        if not kept:
            return ""
        return render_hot(kept).strip()

    def recall(
        self,
        query: str,
        limit: int | None = None,
        *,
        now: float | None = None,
    ) -> str:
        """按关键词召回（单条记忆/情节 + 事件链单元），附来源便于追溯。

        `limit` 是"回几条"的上界（工具侧参数）；`None` = 用 `recall_limit` 这个
        旋钮的配置值。返回的是**进上下文的那几条的渲染文本**：链单元在里面时，
        整条链（含每条的 `at_abs` / `op` / `value` / `raw_text` / `speaker`）一起出现。
        """
        return self.recall_result(query, limit=limit, now=now).text

    def recall_result(
        self,
        query: str,
        *,
        limit: int | None = None,
        now: float | None = None,
    ) -> RecallOutcome:
        """`recall` 的完整读数：候选池（含链单元）、五维分、被淘汰的、链的明细。

        候选池 = ①`memory` 逐字召回 ②`episode` LIKE 召回 ③**事件链单元**（§6.7），
        一起过 `rank` 的两阶段（候选 → 五维重排）。链单元是**一个**候选；
        `event_chain_max_units` 在外面由 `rank` 施加（超出的链整条不进）。
        """
        at = self._clock.now() if now is None else float(now)
        effective = self._effective_limit(limit)
        overrides = dict(self._overrides)
        if limit is not None:
            # 调用方显式要的条数就是这一次的 `recall_limit`（K 会被它夹住）。
            overrides["recall_limit"] = int(limit)

        pairs: list[tuple[Candidate, str]] = list(self._single_candidates(query, effective))
        selection = select_chains(self._conn, query, overrides=self._overrides)
        chains = (
            tuple(
                _build_chain(
                    self._conn,
                    entity,
                    attribute,
                    query=query,
                    overrides=self._overrides,
                )
                for entity, attribute in selection.keys
            )
            if selection.variable_quantity
            else ()
        )
        for chain in chains:
            pairs.append((chain.to_candidate(), chain.render))

        ranked = rank([candidate for candidate, _ in pairs], now=at, overrides=overrides)
        return RecallOutcome(
            query=query,
            now=at,
            selection=selection,
            chains=chains,
            ranked=ranked,
            rendered={candidate.ref: text for candidate, text in pairs},
        )

    def _effective_limit(self, limit: int | None) -> int:
        if limit is not None:
            return max(0, int(limit))
        return max(0, int(limits.resolve("recall_limit", self._overrides)[0]))

    def _single_candidates(
        self, query: str, limit: int
    ) -> list[tuple[Candidate, str]]:
        """①② 两条单条通道：各自的渲染文本与候选一起交出去（渲染与打分同源）。"""
        pairs: list[tuple[Candidate, str]] = []
        for item in search_memories(self._conn, query)[:limit]:
            # 记一次使用：没有这一步，打分里的「使用频率」永远是初始值，
            # 那条维度等于不存在。
            touch_memory(self._conn, item.id)
            pairs.append(
                (
                    _memory_candidate(self._conn, item),
                    f"[{item.kind}] {item.text}（来源 {item.source or '未标注'}）",
                )
            )

        # 事件表以前只写不读，等于几百条历史躺在库里没人用。
        # 它回答的是「上次这类事做到哪、结果如何」，和记忆是互补的。
        for position, row in enumerate(search_episodes(self._conn, query, limit=limit)):
            pairs.append(
                (
                    _episode_candidate(row, position=position),
                    f"[事件] {row['task']} —— {row['summary']}（结果 {row['outcome']}）",
                )
            )
        return pairs

    def search_history(
        self,
        query: str,
        *,
        limit: int,
        snippet_chars: int,
        session: str = "",
        since: float | None = None,
        until: float | None = None,
    ) -> list[MessageHit]:
        """按关键词检索聊天往来（历史消息）。**作用域由调用方解好再传进来**。

        `session=""` 在这一层的含义是「不过滤会话」（库里能看到的都查）——
        它是一个**显式**的"全部"，不是默认值：默认值由 `resolve_scope` 决定，
        而那个默认是**当前会话**（工具与命令行都走它）。跨会话必须显式要，
        这是用户点名的要求；工作区边界依旧不动（历史按工作区分库存）。
        """
        return search_messages(
            self._conn,
            query,
            limit=limit,
            snippet_chars=snippet_chars,
            session=session,
            since=since,
            until=until,
        )

    def archive(
        self,
        state: TaskState,
        outcome: str = "success",
        promote: Sequence[tuple[str, str]] = (),
        final: str = "",
    ) -> ArchiveResult:
        """归档一次已结束的任务。"""
        return archive_task(
            self._conn,
            self._hot_path,
            state,
            session_id=self.session_id,
            counter=self._counter,
            context_window=self._window,
            outcome=outcome,
            promote=promote,
            overrides=self._overrides,
            final=final,
        )


# ---- 单条候选：`memory` / `episode` ------------------------------------------


def _memory_candidate(conn: sqlite3.Connection, item: Memory) -> Candidate:
    """一条 `memory` 行 → 候选。

    `Memory` 这个 dataclass 不带时间戳与 `success_count`，而 `rec`（新近度）与
    "通过验证"的 `+0.1` 要用它们，所以按 `id` 补一次**只读**列查询。
    `bm25_raw=None`：`search_memories` 走 `memory_fts` 只给"匹配上了"，
    没有 `bm25()` 原值可打 → `rel` 如实为 0（与 T6 对 LIKE 回退的处置同一口径）。
    """
    row = conn.execute(
        "SELECT created_at, last_used_at, applied_count, success_count FROM memory"
        " WHERE id = ?",
        (item.id,),
    ).fetchone()
    return Candidate(
        ref=f"memory:{item.id}",
        text=item.text,
        kind=item.kind,
        bm25_raw=None,
        importance=None,
        use_count=item.use_count,
        applied_count=0 if row is None else int(row["applied_count"] or 0),
        created_at=0.0 if row is None or row["created_at"] is None else float(row["created_at"]),
        last_used_at=(
            None if row is None or row["last_used_at"] is None else float(row["last_used_at"])
        ),
        verified=bool(row is not None and (row["success_count"] or 0) > 0),
    )


def _episode_candidate(row: sqlite3.Row, *, position: int) -> Candidate:
    """一条 `episode` 行 → 候选。

    `search_episodes` **不返回 `id`**（它的 SELECT 只有 task/summary/outcome/created_at），
    而 ref 要稳定可追溯，所以这里用「任务名 + 归档时刻 + 本次序号」拼一个可读的 ref。
    **不改 `search_episodes` 的检索口径**（那是一次 LIKE 判据，重复一份会让两处漂移）。
    """
    created_at = float(row["created_at"] or 0.0)
    return Candidate(
        ref=f"episode:{row['task']}@{created_at:.0f}#{position}",
        text=f"{row['task']} —— {row['summary']}（结果 {row['outcome']}）",
        kind="episode",
        bm25_raw=None,
        importance=None,
        created_at=created_at,
    )


# ---- 链的构造与渲染 ----------------------------------------------------------


def _registered_keys(conn: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    """注册表里的全部 `(entity, attribute)`（§15.2：只有注册过的键能当链的口子）。"""
    return tuple(
        (row["entity"], row["attribute"])
        for row in conn.execute(
            "SELECT entity, attribute FROM entity_attribute ORDER BY entity, attribute"
        )
    )


def _aliases(conn: sqlite3.Connection) -> dict[str, dict[str, set[str]]]:
    """canonical → 别名集合，分 entity / attribute 两组（§15.2 的门牌号翻译）。"""
    out: dict[str, dict[str, set[str]]] = {"entity": {}, "attribute": {}}
    for kind, table in (("entity", "entity_alias"), ("attribute", "attribute_alias")):
        for row in conn.execute(f"SELECT alias, canonical FROM {table}"):
            out[kind].setdefault(row["canonical"], set()).add(row["alias"])
    return out


def _key_named(
    conn: sqlite3.Connection,
    key: tuple[str, str],
    query: str,
    aliases: dict[str, dict[str, set[str]]] | None = None,
) -> bool:
    """问句里**直接说出**了这个键吗（键名、它的别名，或点号写法）？"""
    entity, attribute = key
    needle = query.casefold()
    table = _aliases(conn) if aliases is None else aliases
    entity_names = {entity} | table["entity"].get(entity, set())
    attribute_names = {attribute} | table["attribute"].get(attribute, set())
    hit_entity = any(name and name.casefold() in needle for name in entity_names)
    hit_attribute = any(name and name.casefold() in needle for name in attribute_names)
    dotted = f"{entity}.{attribute}".casefold() in needle
    return (hit_entity and hit_attribute) or dotted


def _query_tokens(query: str) -> tuple[str, ...]:
    """查询的**逐字** token（与 `memory_fts` / `semantic_fts` 的索引侧同一口径）。"""
    return tuple(token for token in normalize_for_fts(query).split() if token)


def _keys_recalled_by_events(
    conn: sqlite3.Connection,
    query: str,
    keys: Sequence[tuple[str, str]],
    overrides: Mapping[str, float] | None = None,
) -> tuple[tuple[str, str], ...]:
    """哪些键的**事件**被问句逐字召回了（候选选择判据，见 `MIN_MATCHED_TOKENS`）。

    只看 `status='active'` 的事件：被软撤销的那条不该把一个键拉进候选
    （但它仍然在链上——链一旦建起来就含全部事件，§15.4）。

    命中门槛每次从 `limits` 取（旋钮 `event_chain_min_matched_tokens`），
    所以 `.agent/limits.json` 能改"链要多难才建得起来"；T11 之前这个数写死在本模块。
    """
    tokens = _query_tokens(query)
    if not tokens:
        return ()
    threshold = int(
        limits.resolve("event_chain_min_matched_tokens", overrides)[0]
    )
    selected: list[tuple[str, str]] = []
    for entity, attribute in keys:
        rows = conn.execute(
            "SELECT raw_text, value, entity, attribute FROM memory_event"
            " WHERE entity = ? AND attribute = ? AND status = 'active'",
            (entity, attribute),
        )
        for row in rows:
            text = " ".join(
                (row["raw_text"], row["value"], row["entity"], row["attribute"])
            ).casefold()
            if len({token for token in tokens if token in text}) >= threshold:
                selected.append((entity, attribute))
                break
    return tuple(selected)


def _event_order(event: Event) -> tuple[int, float, int]:
    """链上事件的序：`at_abs` 升序；时间未知的排在**已解析事件之后**（§15.5 末段）。

    `time_precision='unknown'` 的事件不是"最早"也不是"最晚"——它**不知道**，
    所以既不塞到链首也不塞到链尾冒充端点，而是统一排在后面并带 `?` 标记。
    """
    if event.at_abs is None:
        return (1, 0.0, event.id)
    return (0, float(event.at_abs), event.id)


def _build_chain(
    conn: sqlite3.Connection,
    entity: str,
    attribute: str,
    *,
    query: str = "",
    overrides: Mapping[str, float] | None = None,
) -> EventChain:
    """造一个链单元：取全事件 + 全 state 行 + 当前行，判完整度、算重要度、渲染。

    顺手做**事件侧的相关度取值**（T11 补缺轮）：把 `query` 交给
    `store.event_bm25_raws`，一次拿回链上**命中问句**那些事件的 `bm25()` 原值，
    填进 `EventChain.bm25_raws`（`rank` 的链内取最高就靠它）。
    `query` 为空串（例如只想拿链做别的用途）时 `memory_event_fts` 也匹配不出东西，
    `bm25_raws` 因此为空、链的 `rel` 如实为 0——**不编一个数**。
    """
    events = tuple(sorted(fetch_events(conn, entity, attribute), key=_event_order))
    state_rows = fetch_state_rows(conn, entity, attribute)
    current = fetch_current_state(conn, entity, attribute)
    reducer = (
        current["reducer"]
        if current is not None
        else resolve_key(conn, entity, attribute).reducer
    )
    reduction = reduce_events(events, reducer)
    conflict_ids = tuple(int(event_id) for event_id in reduction.conflict_events)
    complete, incomplete_reason = _completeness(events, current)
    bm25_raws = event_bm25_raws(conn, query, tuple(event.id for event in events))

    # 链单元的"重要度" = **链内最高** confidence（`memory_event` 没有 importance 列，
    # 本实现按 confidence 的最大值读——见 `rank._chain_imp_part` 的 reason）。
    # 取最大是长度不敏感的：多一条低置信的事件不会把链的重要度抬上去。
    confidences = [float(event.confidence) for event in events]
    confidences.extend(float(row["confidence"]) for row in state_rows)
    importance = max(confidences) if confidences else 0.5

    max_chars = int(limits.resolve("event_chain_render_max_chars", overrides)[0])
    render, truncated = render_chain(
        entity=entity,
        attribute=attribute,
        events=events,
        state_rows=state_rows,
        current=current,
        conflict_event_ids=conflict_ids,
        incomplete_reason=incomplete_reason,
        max_chars=max_chars,
    )
    return EventChain(
        entity=entity,
        attribute=attribute,
        events=events,
        state_rows=tuple(state_rows),
        current_state=current,
        importance=importance,
        complete=complete,
        incomplete_reason=incomplete_reason,
        conflict_event_ids=conflict_ids,
        render=render,
        truncated=truncated,
        bm25_raws=bm25_raws,
    )


def _completeness(
    events: Sequence[Event], current: sqlite3.Row | None
) -> tuple[bool, str]:
    """**时序完整度**：链的当前值是不是真由这条链推出来的（§6.7 表 / §15.6）。

    四条一起看，缺一条就**扣分并给出原因**（`rank` 用 `complete=False` 把链单元的
    重要度乘旋钮 `event_chain_incomplete_factor`）：① 链上有事件；② 有当前 state 行；
    ③ 当前行 `status='active'` 且 `value` 非空（缺基准时 `value` 是空的，
    **不给猜的数**）；④ `derived_from` 覆盖写链（有事件没进证据链 = 归约没跑全）。
    """
    if not events:
        return False, "链上一条事件都没有"
    if current is None:
        return False, "没有当前 state 行（valid_to IS NULL）：推不出当前值"
    if current["status"] != "active":
        return False, (
            f"状态未定：status={current['status']!r}（reason={current['reason']!r}），"
            "不假装完整"
        )
    if not (current["value"] or ""):
        return False, (
            f"当前值留空（reason={current['reason']!r}）：缺基准值，不给猜的数"
        )
    covered = set(derived_ids(current))
    missing = [event.id for event in events if event.id not in covered]
    if missing:
        gap = ", ".join(f"event:{event_id}" for event_id in missing)
        return False, f"`derived_from` 没覆盖写链：{gap} 不在证据链里（归约没跑全）"
    return True, ""


def render_chain(
    *,
    entity: str,
    attribute: str,
    events: Sequence[Event],
    state_rows: Sequence[sqlite3.Row],
    current: sqlite3.Row | None,
    conflict_event_ids: Sequence[int],
    incomplete_reason: str,
    max_chars: int,
) -> tuple[str, bool]:
    """把整条链渲染成**一段**文本；超 `max_chars` 就截断并**标注被略去的 `event:id`**。

    为什么截断必须标注（而不是切掉了事）：链单元的价值是"一次取一个结论"，
    读者要能看出"这条链上还有 N 条事件没显示、它们是 `event:6 … event:12`"——
    静默截断会让人以为链只有这么长，那比不显示更糟（§15.4：事件永不删除，
    它的痕迹也不该在渲染层消失）。

    返回 `(文本, 是否截断)`。
    """
    size = len(events)
    header: list[str] = [
        f"[事件链] {entity}.{attribute}（{size} 条事件，unit={chain_unit_label(size)}）"
    ]
    if current is None:
        header.append("当前值：没有 state 行（推不出，不猜一个数）")
    else:
        value = current["value"] if (current["value"] or "") else "?"
        unit = current["unit"] or ""
        label = {"conflict": "**未定**", "needs_review": "**待复核**"}.get(
            current["status"], ""
        )
        body = (
            f"当前值 {value}{(' ' + unit) if unit else ''}"
            f"{(' ' + label) if label else ''}"
            f"（status={current['status']}, reducer={current['reducer']}）"
        )
        if conflict_event_ids:
            body += (
                "：互斥事件 "
                + " ".join(f"event:{event_id}" for event_id in conflict_event_ids)
                + "（两条都留，不注入单个数）"
            )
        elif current["status"] == "needs_review" and current["reason"]:
            body += f"，reason={current['reason']!r}"
        header.append(body)
    if len(state_rows) > 1:
        # 链**含已被取代的旧 state 行**（§15.4：取代不是删除，旧行只被关闭）：
        # 把它们摊在一行里，读者一眼能看到"这个值是怎么一路变过来的"。行数多时
        # 取首两条 + 末条（全摊开会把预算吃光，那是渲染层不该做的事）。
        values = [
            (
                f"{row['value']}{(' ' + row['unit']) if row['unit'] else ''}"
                if (row["value"] or "")
                else "?"
            )
            for row in state_rows
        ]
        if len(values) > 3:
            shown = f"{values[0]} → {values[1]} → … → {values[-1]}"
        else:
            shown = " → ".join(values)
        header.append(
            f"取代链（{len(state_rows)} 行 state，含已被取代的旧行）："
            f"{shown}（末行 = 当前）"
        )
    if incomplete_reason:
        header.append(f"完整度：{incomplete_reason}")

    lines = [_event_line(event) for event in events]
    full = "\n".join(header + lines)
    if len(full) <= max_chars:
        return full, False

    # 截断：**整行删**（不硬切半句），并把被删掉的 `event:id` 全列在标记里。
    ids = ", ".join(f"event:{event.id}" for event in events)
    marker_all = (
        f"…（**截断**：链上共 {size} 条事件，此处略去 {size} 条：{ids}）"
    )
    for head_len in range(len(header), 0, -1):
        # 行数不够时先让出的是**头部的解释行**（完整度），键与当前值留到最后；
        # 但"链上是谁"（`event:id` 列表）在任何形态里都不许消失。
        head = header[:head_len]
        for keep in range(len(lines) - 1, -1, -1):
            omitted = [event.id for event in events[keep:]]
            listed = " ".join(f"event:{event_id}" for event_id in omitted)
            marker = (
                f"…（**截断**：链上共 {size} 条事件，此处略去 {len(omitted)} 条：{listed}）"
                if keep
                else marker_all
            )
            text = "\n".join(head + lines[:keep] + [marker])
            if len(text) <= max_chars:
                return text, True

    # 极端配置（上界比"键一行 + 事件清单"还小）：至少"截断了 + 链上是谁"不能丢。
    if len(marker_all) <= max_chars:
        return marker_all, True
    return f"…（**截断**：链上共 {size} 条事件，渲染上界 {max_chars} 字太小）", True


def _event_line(event: Event) -> str:
    """链上一行：`at_abs` / `op` / `value` / `raw_text` / `speaker`（§6.7 的渲染要点）。

    `time_precision='unknown'`（`at_abs is None`）的行带 `?` 标记——它排在已解析
    事件之后（`_event_order`），且**不许**被当成链首或链尾的端点。

    **时间那一段分两种来源**，因为它们漂不漂不是一回事：

    - `at_source='turn'`：这个时刻是**这一轮的真实钟点**（`session.started_at`
      + 本轮 turn），同一份内容换个时刻重跑就不同——它会进模型上下文，所以
      按运行起点归一化（`prompt_stamp`）。一次 t=0 的复跑里，这一类绝对时刻
      正是被贪心解码放大成两条轨迹的东西（见 `memory/transcript.py` 那一段）。
    - 其余（`explicit` / `relative`）：时刻是从**消息正文里读出来的内容**，
      同一句话到哪天重跑都是同一个渲染——那就不该动它，改了反而是丢信息。
    """
    unknown = event.at_abs is None or event.time_precision == "unknown"
    if event.at_abs is None:
        stamp = "?（时间未知）"
    elif event.at_source == "turn":
        stamp = prompt_stamp(float(event.at_abs))
    else:
        stamp = local_dt(event.at_abs, event.tz_offset_min).strftime("%Y-%m-%d %H:%M")
    if event.at_abs is not None and event.time_precision == "unknown":
        stamp = f"?{stamp}"
    unit = f" {event.unit}" if event.unit else ""
    speaker = event.speaker or "未标注"
    return (
        f"  event:{event.id} {stamp} {event.op} {event.value}{unit}"
        f"「{event.raw_text}」{speaker}"
    )
