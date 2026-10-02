"""五维可解释排序 + 两阶段「候选 → 重排」（设计稿 §6.1–§6.3 / §6.6 / §6.7 / §14.4 规矩 3；计划 T6）。

本模块是**纯函数**：不读时钟（`now` 由调用方给，生产用 T8 的可注入时钟）、
不碰数据库、不写任何表、不渲染进 prompt（候选池只在内存里过一遍，§6.2 表：
「重排候选池（不进上下文）… **0 token**」）。

**公式（§6.1 逐字）**：

    score = w_rel·rel + w_rec·rec + w_imp·imp + w_use·use − w_pen·penalty

五个权重与半衰期**全部从 `limits` 取**（T1 已登记），本模块**一个都不写死**；
`RankedResult.weights` 是**带符号**的系数（`penalty` 那一项是负号，因为它在公式里
是减项），所以 `Σ weights[k]·parts[k] == score` 对每一条都成立（容差 1e-9）。
每个分量都带一条 **reason**（原始值、怎么算的、哪个数据源缺席），`parts` + `reasons`
就是 T13 的 `why` 的地基（§6.3 要求把 `bm25` 原值与归一化分量**两个都打**）。

**各维的口径（都不留暗常数）**：

| 分量 | 怎么算 | 口径说明 |
|---|---|---|
| `rel` | `1 − exp(−w_bm25·bm25_score)` | `bm25_score = -bm25()`（`store.bm25_score`，T5 实测钉住符号：原值越小越相关）。**没有自带归一化**——原值与方向都由 T5 定死。`bm25_score` 为负（该词 IDF 为负）时 `1−exp(...)` 为负 → **夹到 0**（reason 里写明）。`bm25_raw is None`（走 LIKE 回退、没碰 FTS）→ `rel` **如实为 0**，不编一个值 |
| `rec` | `exp(−age_days / half_life_days)` | `age_days` 基于 `last_used_at or created_at`，**用哪一天写在 reason 里**。没有时间戳（都是 0/None）→ `rec` 如实为 0；时间戳晚于 `now` → `age_days` 按 0 夹取（`rec` 不会超过 1） |
| `imp` | 判定值（`Candidate.importance`）**否则** kind 基线，再加「通过验证 / 成功取代过别人」的 `+0.1` | 基线：`fact`/`decision` 0.6、`preference` 0.7、`lesson`/`skill` 0.5；未登记的 kind 取中性 0.5（reason 里点名）。**用户改过的**在 reason 里标 `(user)`（§6.3：不许数字裸奔） |
| `use` | `log1p(use_count) / log1p(max_use)`，`max_use` = **进重排的候选集内**最大 `use_count` | 归一化分母取"这一批候选里用得最多的那条"，所以 `max_use` 那条恰好是 1.0。`max_use == 0`（全都没用过）→ 分母为 0 → 全体 `use` 如实为 0，**不做除法**。`use_count` 为负按 0 夹取 |
| `penalty` | `applied_count / (use_count + 1)`，**仅当负反馈数据源存在** | **P1 没有负反馈数据源**（没有写入者，`applied_count` 恒为 0，采信它就是拿一个恒 0 的列冒充"反馈"）→ 由数据驱动地贡献 **0**，reason 里写明「无负反馈数据」。**不静默塞一个猜的值**：数据源一出现（`NEGATIVE_FEEDBACK_SOURCE`），公式立刻按 §6.1 生效 |

**两阶段（§14.4 规矩 3）**：

1. 第①阶段（**候选池**）：逐字 FTS5 + `bm25()` 排序（`semantic.search_semantic`
   已在 SQL 里做完，`ORDER BY bm25(semantic_fts)` 升序）。`rank()` 收到的是这一阶段的
   产物；它自己**只做一次稳定复排**（`order_by_bm25`：相关度降序、无 `bm25_raw` 的排最后），
   以保证"调用方给乱序也得到同一个结果"，**不引入第二套归一化**；
2. 合并去重：`dedupe_key or content_hash or text.strip()`，**去重先于截断 N**
   （重复条目不该占候选池预算），保留更相关的那条；
3. 取 **N = `recall_candidates_n`**（默认 50）进重排，超过 N 的每一条都带
   `over_budget` 的 reason，**不静默丢**；
4. 五维重排 → 取 **K = `recall_rerank_k`**（默认 5）。**硬约束 `K ≤ recall_limit`**：
   越界时**夹取**到 `recall_limit` 并在 `RankedResult.k_note` 写明（选择夹取而不是报错，
   理由写在 `_resolve_settings` 的注释里：`recall` 在会话热路径上，配置写错不该把
   主循环炸掉；但夹取必须**可见**）。重排**只能减少**进上下文的条数、不能增加。

**丢掉的候选**：`Dropped.reason` ∈ `DROPPED_REASONS`（§6.3 的枚举 +
用户点名的 `deduped`）。`demoted` / `conflict` 在 P1 **没有数据源**（见
`REASONS_WITHOUT_SOURCE_IN_P1`），本模块**不编造**它们。

**本任务不实现**（别在这里找）：事件链单元的链内逻辑（T11；本模块只带一个中性的
`Candidate.unit` 字段）、token 预算与让位（T12）、`memory_decision` 落库与 `why`
渲染（T13）、`recall` / 会话的接线（T11/T13）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from spoolkit import limits
from spoolkit.memory.store import bm25_score

# ---- 常量：五个分量、五个权重旋钮、它们的名字只在这里出现一次 ----------------

# 分量的顺序 = §6.1 公式里的出现顺序。`parts` / `reasons` / `weights` 三个字典
# 共用这一份顺序，所以"加回总分"的循环不需要另写一套映射。
PART_ORDER: tuple[str, ...] = ("rel", "rec", "imp", "use", "penalty")

# 分量 → `limits` 里的旋钮名（T1 已登记；一个都不写死在这里的数值上）。
WEIGHT_KNOBS: dict[str, str] = {
    "rel": "rank_w_relevance",
    "rec": "rank_w_recency",
    "imp": "rank_w_importance",
    "use": "rank_w_usage",
    "penalty": "rank_w_penalty",
}

# 分量的中文名（只用于 `explain()` 的渲染；T13 的三个出口用同一份文字）。
PART_LABELS: dict[str, str] = {
    "rel": "相关",
    "rec": "新近",
    "imp": "重要",
    "use": "频次",
    "penalty": "反馈",
}

# `imp` 的 kind 基线（§6.1 表）。**这是"写入时判定"的初值表**：T6 自己不判定，
# 只做"没有判定值就退回基线"。任一取值都要能被 reason 说出来。
IMPORTANCE_BASELINE: dict[str, float] = {
    "fact": 0.6,
    "decision": 0.6,
    "preference": 0.7,
    "lesson": 0.5,
    "skill": 0.5,
}
# 未登记的 kind（例如 T11 的链单元）取中性值，并在 reason 里点名"未登记"。
IMPORTANCE_BASELINE_FALLBACK = 0.5
# §6.1 的 "+0.1"（口径裁决 8 的解释：**成功取代过别人**或**通过验证**的 +0.1）。
IMPORTANCE_VERIFIED_BONUS = 0.1

# `Dropped.reason` 的合法取值。前五个是 §6.3 逐字给的枚举，`deduped` 是本计划
# 的合并去重（用户在 T6 指令里点名要它），**加取值而不是含糊其辞**。
DROPPED_REASONS: tuple[str, ...] = (
    "below_k",  # 进了五维重排，但排在 K 名之外
    "over_budget",  # 在候选池 N 之外，没进重排（条数预算；T12 的 token 预算共用这个取值）
    "deduped",  # 与另一条候选的去重键相同（保留更相关的那条）
    "superseded",  # 行自己声明已被取代（`status='superseded'` 或 `superseded_by` 有值）
    "demoted",  # P1 无数据源，见下
    "conflict",  # P1 无数据源，见下
)
# 这两个取值在 P1 **没有任何数据源**：`demoted`（降级）与 `conflict`（冲突未决）
# 要等 T9 的归约器/冲突六类与 T11 的链单元完整度。**本模块不编造它们**——
# 与 `penalty` 同一条纪律：没有数据就说没有，不猜一个值填上。
REASONS_WITHOUT_SOURCE_IN_P1: tuple[str, ...] = ("demoted", "conflict")

# `penalty` 的数据源开关。P1 **没有**负反馈数据的写入者（`semantic.applied_count`
# 没有被任何写入口更新过，恒为 0），所以默认 `None` = 「这条数据不存在」，
# `penalty` 由数据驱动地贡献 0（reason 写明）。等 T7/T13 真的开始记
# 「推给了但没被采纳」，把它设成一个可读的来源名，公式立刻按 §6.1 生效。
# 用例把它临时置成非 None 来证明"这不是写死的 0"。
NEGATIVE_FEEDBACK_SOURCE: str | None = None

# 负反馈缺席时 `parts["penalty"]` 的 reason 逐字（用例按它断言，别改字面量而不改用例）。
NO_NEGATIVE_FEEDBACK = (
    "无负反馈数据（P1 没有 applied_count 的写入者，该列恒为 0，不采信）：penalty 如实为 0"
)


@dataclass(frozen=True)
class Candidate:
    """一条参与排序的候选（单条记忆，或 T11 的一个事件链单元）。

    `ref` 是稳定的外部引用（`semantic:<id>` 之类），`why` 与 `dropped` 都用它
    指认"说的是哪一条"——不靠文本，也不靠列表下标。

    `unit` 在 P1 只有 `"single"` 一种；T11 会传 `"event_chain"`，并按 §6.7 把
    **链内最高 `bm25`** 作为 `bm25_raw` 传进来（链是一个单元，不能把 N 条的分数
    加起来，那是 T11 的规矩）。本模块**不实现**链内逻辑，只认这一个字段：
    链单元**不过滤** `superseded`（链天然含被取代的旧 state 行，§15.4）。

    `dedupe_key` 为空时按 `content_hash`、再退到 `text.strip()` 推算去重键
    （见 `dedupe_signature`）；`dedupe_similarity` 那个近似阈值**不属于 T6**
    （它归 §7 的去重/巩固，这里只做同键精确合并）。
    """

    ref: str
    text: str = ""
    kind: str = "fact"
    unit: str = "single"
    bm25_raw: float | None = None
    importance: float | None = None
    importance_user_set: bool = False
    verified: bool = False
    superseded_other: bool = False
    use_count: int = 0
    applied_count: int = 0
    created_at: float = 0.0
    last_used_at: float | None = None
    status: str = "active"
    superseded_by: int | None = None
    content_hash: str = ""
    dedupe_key: str = ""

    @property
    def dedupe_signature(self) -> str:
        """合并去重的键：显式 `dedupe_key` → `content_hash` → `text.strip()`。

        三者都为空时返回 `""`，调用方（`rank`）把空键当作**唯一**处理——
        否则一批空文本的候选会被合并成一条，那才是真的静默丢东西。
        """
        return self.dedupe_key or self.content_hash or self.text.strip()

    @property
    def superseded(self) -> bool:
        """这条候选是否**自己声明**已被取代（旧行不删，但别再当答案）。"""
        return self.status == "superseded" or self.superseded_by is not None

    @classmethod
    def from_semantic(cls, row) -> "Candidate":
        """把一条 `semantic.Semantic`（通常是 `search_semantic` 的返回值）转成候选。

        三处映射要说清（免得读的人以为丢字段了）：

        - `bm25_raw` 原样带过来；走 LIKE 回退的那一行它本来就是 `None`；
        - `verified` 取 `success_count > 0`（"通过验证"在 T6 的读法 = 用成功过），
          这是**本实现的解释**，写入侧语义由 T7/T9 保证；
        - `superseded_other`（"成功取代过别人"）**取不到**：它要反查"有哪条指向我"，
          不是本行的字段。P1 没有这条数据源，所以如实留 `False`——那 `+0.1`
          这一半今天不会生效，**不假装它生效了**（与 `penalty` 同一条纪律）。
        """
        return cls(
            ref=f"semantic:{row.id}",
            text=row.text,
            kind=row.kind,
            bm25_raw=row.bm25_raw,
            importance=row.importance,
            use_count=row.use_count,
            applied_count=row.applied_count,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            status=row.status,
            superseded_by=row.superseded_by,
            content_hash=row.content_hash,
            verified=row.success_count > 0,
        )


@dataclass(frozen=True)
class Scored:
    """一条算过五维分的候选：`parts` + `reasons` + `weights` + 总分，一个都不少。

    `weights` 与 `parts` 放在同一条上（而不是只放在 `RankedResult` 上），
    是为了让"这一条的分是这几个数乘出来的"**自证**：`Σ weights[k]·parts[k] == score`
    拿单条就能验，不用回去找结果对象。

    `order` 是它在**第①阶段序**里的位置，既是稳定排序的 tie-break，
    也是"总分相同就按相关度/输入序"这条口径的落点。
    """

    candidate: Candidate
    parts: dict[str, float]
    reasons: dict[str, str]
    weights: dict[str, float]
    score: float
    order: int

    def explain(self) -> str:
        """§6.1 的那个分解行（T13 的三个出口渲染同一份数据）。

        打的是**加权后的贡献**（`w·part`），不是裸分量——读者问的是"这条为什么
        排在这"，加权贡献才直接对得上总分。`penalty` 是减项，渲染成 `− 反馈 x`。
        """
        contributions = [
            (part, self.weights[part] * self.parts[part]) for part in PART_ORDER
        ]
        head = " + ".join(
            f"{PART_LABELS[part]} {value:.2f}"
            for part, value in contributions
            if part != "penalty"
        )
        tail = f"− {PART_LABELS['penalty']} {abs(dict(contributions)['penalty']):.2f}"
        return f"分 {self.score:.2f} = {head} {tail}"


@dataclass(frozen=True)
class Dropped:
    """一条没进上下文的候选 + **它为什么没进**（§6.3：不许静默丢）。

    进了五维重排之后才被淘汰的（`below_k`）带 `score` / `parts`；在候选池阶段
    就被淘汰的（`over_budget` / `deduped` / `superseded`）没算过分，如实留
    `score=None` / `parts={}`——**不编一个 0 分的分数**。
    """

    candidate: Candidate
    reason: str
    detail: str
    score: float | None = None
    parts: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RankedResult:
    """两阶段的结果：进上下文的 + 没进的（都带理由）+ 权重与旋钮来源。"""

    chosen: tuple[Scored, ...]
    dropped: tuple[Dropped, ...]
    weights: dict[str, float]
    sources: dict[str, str]
    pool_size: int
    reranked: int
    max_use: int
    k_requested: int
    k: int
    limit: int
    k_note: str

    @property
    def chosen_refs(self) -> tuple[str, ...]:
        return tuple(item.candidate.ref for item in self.chosen)

    @property
    def dropped_reasons(self) -> dict[str, int]:
        """reason → 条数。理由的分布要能一眼看出来，不然"没静默丢"只是句口号。"""
        counts: dict[str, int] = {}
        for item in self.dropped:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


@dataclass(frozen=True)
class _Settings:
    """一次调用的全部旋钮（值 + 来源），全部来自 `limits.resolve`。"""

    weights: dict[str, float]  # 带符号：penalty 为负
    w_bm25: float
    half_life_days: float
    candidates_n: int
    rerank_k: int
    limit: int
    sources: dict[str, str]


def order_by_bm25(candidates: Iterable[Candidate]) -> list[Candidate]:
    """第①阶段序的纯函数版：**相关度降序、稳定、无原值的排最后**。

    为什么要有它（而不是只信调用方给的顺序）：`rank()` 要能对"乱序的候选池"给出
    同一个结果，否则 N 截断切掉哪几条就取决于调用方偶然的拼接顺序。口径与
    `semantic.search_semantic` 的 `ORDER BY bm25(semantic_fts)` 升序**同一份**
    （`bm25_score = -bm25()`，见 `store.bm25_score`）；这里**不引入第二套归一化**。

    `bm25_raw is None`（走 LIKE 回退、没碰 FTS）的候选**排在所有有原值的之后**，
    彼此保持输入顺序——它没有相关度可言，不该靠"猜一个 0"插到前面。它进重排后
    `rel` 分量如实为 0（见 `_rel_part`）。
    """
    ranked: list[tuple[int, int, float, Candidate]] = []
    for index, candidate in enumerate(candidates):
        if candidate.bm25_raw is None:
            ranked.append((1, index, 0.0, candidate))
        else:
            ranked.append((0, index, -bm25_score(candidate.bm25_raw), candidate))
    ranked.sort(key=lambda item: (item[0], item[2], item[1]))
    return [item[3] for item in ranked]


def rank(
    candidates: Sequence[Candidate],
    *,
    now: float,
    overrides: Mapping[str, float] | None = None,
) -> RankedResult:
    """两阶段排序：候选池去重取 N → 五维重排取 K（**硬约束 K ≤ `recall_limit`**）。

    `now` 是**调用方给的**时间（epoch 秒）——本函数不读时钟，所以排序在用例里
    可复现，也接得上 T8 的可注入时钟。`overrides` 是 `.agent/limits.json` 那一份
    覆盖表，直接透传给 `limits.resolve`。
    """
    settings = _resolve_settings(overrides)

    pool = list(candidates)
    dropped: list[Dropped] = []

    # 第①阶段序 + ②合并去重 + ③截断候选池。三件都在**内存里**做，不进 prompt。
    ordered = order_by_bm25(pool)
    live: list[Candidate] = []
    seen: dict[str, Candidate] = {}
    for candidate in ordered:
        if candidate.unit == "single" and candidate.superseded:
            dropped.append(
                Dropped(
                    candidate=candidate,
                    reason="superseded",
                    detail=(
                        "行自己声明已被取代"
                        f"（status={candidate.status!r}, superseded_by={candidate.superseded_by!r}）："
                        "旧行不删（§15.4），但不再作为单条答案进上下文"
                    ),
                )
            )
            continue
        signature = candidate.dedupe_signature
        if signature and signature in seen:
            kept = seen[signature]
            dropped.append(
                Dropped(
                    candidate=candidate,
                    reason="deduped",
                    detail=(
                        f"与 {kept.ref} 的去重键相同（{signature!r}）：保留更相关的那条"
                        "（第①阶段序在前者胜）"
                    ),
                )
            )
            continue
        if signature:
            seen[signature] = candidate
        live.append(candidate)

    rerank_pool = live[: settings.candidates_n]
    # 用 enumerate 而不是 `live.index(...)`：两个字段完全相同的候选在 `index` 下
    # 会指到同一个下标（说的位置就不是自己的了）。
    for position, candidate in enumerate(
        live[settings.candidates_n :], start=settings.candidates_n + 1
    ):
        dropped.append(
            Dropped(
                candidate=candidate,
                reason="over_budget",
                detail=(
                    f"在第①阶段序里排在第 {position} 位，超出候选池 "
                    f"recall_candidates_n={settings.candidates_n}：没进五维重排"
                ),
            )
        )

    # ④ 五维打分。`max_use` 的口径 = **这一批进重排的候选**里的最大值（写进 reason）。
    max_use = max((_clean_use_count(item) for item in rerank_pool), default=0)
    scored: list[Scored] = []
    for order, candidate in enumerate(rerank_pool):
        parts, reasons = _parts(candidate, now=now, settings=settings, max_use=max_use)
        score = math.fsum(settings.weights[part] * parts[part] for part in PART_ORDER)
        scored.append(
            Scored(
                candidate=candidate,
                parts=parts,
                reasons=reasons,
                weights=dict(settings.weights),
                score=score,
                order=order,
            )
        )
    # 稳定排序：总分相同 → 回到第①阶段序（`order`），也就是更相关/更靠前的先取。
    scored.sort(key=lambda item: (-item.score, item.order))

    chosen = tuple(scored[: settings.rerank_k])
    for rank_index, item in enumerate(scored[settings.rerank_k :], start=settings.rerank_k + 1):
        dropped.append(
            Dropped(
                candidate=item.candidate,
                reason="below_k",
                detail=(
                    f"五维分 {item.score!r} 排在第 {rank_index} 名，"
                    f"K=recall_rerank_k={settings.rerank_k}"
                    f"（recall_limit={settings.limit}）：没进上下文"
                ),
                score=item.score,
                parts=dict(item.parts),
            )
        )

    requested_k = _requested_rerank_k(overrides)
    k_note = ""
    if settings.rerank_k < requested_k:
        k_note = (
            f"K={requested_k} 被 recall_limit={settings.limit} "
            f"夹取到 {settings.rerank_k}（硬约束 K ≤ recall_limit：重排只能减少"
            "进上下文的条数、不能增加）"
        )
    return RankedResult(
        chosen=chosen,
        dropped=tuple(dropped),
        weights=dict(settings.weights),
        sources=dict(settings.sources),
        pool_size=len(pool),
        reranked=len(rerank_pool),
        max_use=max_use,
        k_requested=requested_k,
        k=settings.rerank_k,
        limit=settings.limit,
        k_note=k_note,
    )


# ---- 内部：旋钮解析与五个分量 ------------------------------------------------


def _requested_rerank_k(overrides: Mapping[str, float] | None) -> int:
    """`recall_rerank_k` 的**原始**请求值（夹取前），只用于把话说清楚。"""
    return int(limits.resolve("recall_rerank_k", overrides)[0])


def _resolve_settings(overrides: Mapping[str, float] | None) -> _Settings:
    """把七个旋钮一次读齐（值 + 来源），并校验到"能算"为止。

    校验是**显式报错**的：负权重、负 `w_bm25`、`half_life_days <= 0`、
    `recall_candidates_n < 1`、`recall_rerank_k < 0`、`recall_limit < 0` 一律
    `ValueError`——配置写错要吵，不能静默算出一个看着正常的排名。

    但 **`K > recall_limit` 是夹取、不是报错**：它是最容易配出来的一种越界，
    而 `rank` 跑在 `recall` 这条会话热路径上，为一个"取多了"把主循环炸掉不划算。
    夹取**必须可见**（`RankedResult.k_note` + `k_requested` 两个字段），
    所以它不违反"不许静默"。
    """
    sources: dict[str, str] = {}
    weights: dict[str, float] = {}
    for part in PART_ORDER:
        value, source = limits.resolve(WEIGHT_KNOBS[part], overrides)
        sources[WEIGHT_KNOBS[part]] = source
        if value < 0:
            raise ValueError(
                f"{WEIGHT_KNOBS[part]} 不能为负（{value!r}）：正负号由 §6.1 的公式决定，"
                "penalty 那一项的减号已经由 weights 带上了"
            )
        weights[part] = -value if part == "penalty" else value

    w_bm25, source = limits.resolve("w_bm25", overrides)
    sources["w_bm25"] = source
    if w_bm25 < 0:
        raise ValueError(f"w_bm25 不能为负（{w_bm25!r}）：它乘在 bm25_score 上")

    half_life, source = limits.resolve("rank_recency_half_life_days", overrides)
    sources["rank_recency_half_life_days"] = source
    if half_life <= 0:
        raise ValueError(
            f"rank_recency_half_life_days 必须为正（{half_life!r}）：它是 rec 的分母"
        )

    candidates_n, source = limits.resolve("recall_candidates_n", overrides)
    sources["recall_candidates_n"] = source
    if int(candidates_n) < 1:
        raise ValueError(f"recall_candidates_n 至少为 1（{candidates_n!r}）：候选池不该为空")

    rerank_k, source = limits.resolve("recall_rerank_k", overrides)
    sources["recall_rerank_k"] = source
    if int(rerank_k) < 0:
        raise ValueError(f"recall_rerank_k 不能为负（{rerank_k!r}）")

    limit, source = limits.resolve("recall_limit", overrides)
    sources["recall_limit"] = source
    if int(limit) < 0:
        raise ValueError(f"recall_limit 不能为负（{limit!r}）")

    return _Settings(
        weights=weights,
        w_bm25=w_bm25,
        half_life_days=half_life,
        candidates_n=int(candidates_n),
        rerank_k=min(int(rerank_k), int(limit)),  # 硬约束 K ≤ recall_limit
        limit=int(limit),
        sources=sources,
    )


def _clean_use_count(candidate: Candidate) -> int:
    """`use_count` 按非负整数读（负数是脏数据，夹到 0，不参与 log1p 之外的运算）。"""
    return max(0, int(candidate.use_count))


def _rel_part(candidate: Candidate, settings: _Settings) -> tuple[float, str]:
    """`rel = 1 − exp(−w_bm25·bm25_score)`，**不在这里再做归一化**（T5 已定死符号与量纲）。"""
    raw = candidate.bm25_raw
    if raw is None:
        return 0.0, "无 bm25 原值（走 LIKE 回退路径，没碰 FTS）：rel 如实为 0"
    score = bm25_score(raw)
    value = 1.0 - math.exp(-settings.w_bm25 * score)
    if value < 0.0:
        # bm25_score 为负 = 该词的 IDF 为负（词太常见）。§6.1 的公式在这种输入下
        # 给出负的"相关度"，没有意义，夹到 0 并把原因写出来（T5 的记录里点名了这个
        # 边界：`N=2, n=1` 时 IDF 恰好 0，出现在多数条目里时为负）。
        return 0.0, (
            f"bm25 原值 {raw!r} → bm25_score {score!r} 为负（该词的 IDF 为负），"
            f"1−exp(−{settings.w_bm25}·{score!r}) 为负 → 夹取到 0"
        )
    return value, (
        f"bm25 原值 {raw!r} → bm25_score {score!r} → "
        f"1−exp(−w_bm25({settings.w_bm25})·{score!r}) = {value!r}"
    )


def _rec_part(candidate: Candidate, now: float, settings: _Settings) -> tuple[float, str]:
    """`rec = exp(−age_days / half_life_days)`；**用哪一天**必须写在 reason 里。"""
    if candidate.last_used_at:
        reference, which = float(candidate.last_used_at), "last_used_at"
    else:
        reference, which = float(candidate.created_at), "created_at"
    if reference <= 0:
        return 0.0, (
            "没有 last_used_at / created_at（无时间戳可算 age_days）：rec 如实为 0"
        )
    age_days = (now - reference) / 86400.0
    clamp_note = ""
    if age_days < 0:
        clamp_note = f"（{which} 晚于 now，age_days 由 {age_days!r} 夹取到 0：rec 不会超过 1）"
        age_days = 0.0
    value = math.exp(-age_days / settings.half_life_days)
    return value, (
        f"{which} 距今 age_days={age_days!r} 天 / 半衰期 "
        f"rank_recency_half_life_days={settings.half_life_days} → "
        f"exp(−age/half) = {value!r}{clamp_note}"
    )


def _imp_part(candidate: Candidate) -> tuple[float, str]:
    """kind 基线（或判定值）+「通过验证 / 成功取代过别人」的 +0.1；用户改过的标 `(user)`。"""
    if candidate.importance is None:
        base = IMPORTANCE_BASELINE.get(candidate.kind)
        if base is None:
            base = IMPORTANCE_BASELINE_FALLBACK
            origin = f"kind={candidate.kind!r} 未登记 → 中性基线 {base}"
        else:
            origin = f"kind={candidate.kind!r} 基线 {base}"
    else:
        base = float(candidate.importance)
        origin = f"判定值 importance={base!r}"
    if candidate.importance_user_set:
        origin += "(user)"

    bonus = 0.0
    if candidate.verified or candidate.superseded_other:
        bonus = IMPORTANCE_VERIFIED_BONUS
        why = []
        if candidate.verified:
            why.append("通过验证")
        if candidate.superseded_other:
            why.append("成功取代过别人")
        origin += f" + {IMPORTANCE_VERIFIED_BONUS}（{'/'.join(why)}）"
    return base + bonus, f"重要度 {origin}"


def _use_part(candidate: Candidate, max_use: int) -> tuple[float, str]:
    """`log1p(use_count) / log1p(max_use)`；归一化分母 = 进重排的候选集内最大值。"""
    count = _clean_use_count(candidate)
    if max_use <= 0:
        return 0.0, (
            f"候选集内 max_use=0（本条 use_count={count}）：log1p 归一化的分母为 0，"
            "use 如实为 0（不做除法）"
        )
    value = math.log1p(count) / math.log1p(max_use)
    note = "" if count == candidate.use_count else f"（use_count={candidate.use_count!r} 按 0 夹取）"
    return value, (
        f"log1p({count})/log1p(max_use={max_use}) = {value!r}"
        f"（归一化口径：分母取本批进重排候选的 use_count 最大值，最大者恰好 1.0）{note}"
    )


def _penalty_part(candidate: Candidate) -> tuple[float, str]:
    """`applied_count / (use_count + 1)`，**只在负反馈数据源存在时**计算。"""
    if NEGATIVE_FEEDBACK_SOURCE is None:
        return 0.0, NO_NEGATIVE_FEEDBACK
    count = _clean_use_count(candidate)
    value = candidate.applied_count / (count + 1)
    return value, (
        f"负反馈（数据源 {NEGATIVE_FEEDBACK_SOURCE}）：applied_count="
        f"{candidate.applied_count} / (use_count={count} + 1) = {value!r}"
    )


def _parts(
    candidate: Candidate, *, now: float, settings: _Settings, max_use: int
) -> tuple[dict[str, float], dict[str, str]]:
    """五个分量一起算：返回 `parts` 与一一对应的 `reasons`（键集合完全相同）。"""
    pairs = {
        "rel": _rel_part(candidate, settings),
        "rec": _rec_part(candidate, now, settings),
        "imp": _imp_part(candidate),
        "use": _use_part(candidate, max_use),
        "penalty": _penalty_part(candidate),
    }
    return (
        {part: pairs[part][0] for part in PART_ORDER},
        {part: pairs[part][1] for part in PART_ORDER},
    )
