"""T6：五维可解释排序 + 两阶段「候选 → 重排」（设计稿 §6.1–§6.3 / §14.4 规矩 3）。

`rank.py` 是**纯函数**：不读时钟、不碰数据库、不渲染进 prompt。本文件把 T6 的
六条验收（计划 T6「验收标准」1–6）逐条钉住，并额外钉住三件容易被"顺手改掉"的事：

1. **`Σ weights[k]·parts[k] == score`** 用 `abs=1e-9, rel=0` 的绝对容差验，
   而且 `weights` 是**带符号**的（`penalty` 为负，因为 §6.1 里它是减项）；
2. **`penalty` 是数据驱动的 0**：P1 没有负反馈数据源（`applied_count` 恒为 0），
   所以它贡献 0 且 reason 写明"无负反馈数据"；把数据源打开就按 `applied_count /
   (use_count + 1)` 扣分——**不是写死的 0**；
3. **两阶段的每一步都不静默丢**：`chosen + dropped` 恰好等于输入，且每条
   `dropped` 都有 `reason` 与可读的 `detail`。

夹具全部用固定 `NOW`（`rank` 自己不读时钟，用例也不该依赖真实时间）。
"""

from __future__ import annotations

import math
import time as time_module
from pathlib import Path

import pytest

import spoolkit.memory.rank as rank_module
from spoolkit.memory.rank import (
    DROPPED_REASONS,
    IMPORTANCE_VERIFIED_BONUS,
    PART_ORDER,
    REASONS_WITHOUT_SOURCE_IN_P1,
    Candidate,
    rank,
)
from spoolkit.memory.semantic import insert_semantic, search_semantic, touch_semantic
from spoolkit.memory.store import init_memory_schema
from spoolkit.store.db import open_db

NOW = 1_700_000_000.0  # 固定的"现在"（epoch 秒）
DAY = 86400.0
TIGHT = {"recall_rerank_k": 3, "recall_limit": 3}  # 三缺省的"全都要看"覆盖


def _cand(
    ref: str,
    *,
    raw: float | None = -1.0,
    age_days: float = 0.0,
    use_count: int = 0,
    created_at: float | None = None,
    **kwargs,
) -> Candidate:
    """按"距今几天"造一条候选；`created_at` 显式给时以它为准（边界用例用）。"""
    return Candidate(
        ref=ref,
        text=kwargs.pop("text", ref),
        bm25_raw=raw,
        use_count=use_count,
        created_at=NOW - age_days * DAY if created_at is None else created_at,
        **kwargs,
    )


def _sum_check(item, result) -> None:
    """`Σ weights[k]·parts[k] == score`：容差口径 = 绝对 1e-9、相对 0。"""
    total = math.fsum(item.weights[part] * item.parts[part] for part in PART_ORDER)
    assert total == pytest.approx(item.score, abs=1e-9, rel=0)
    total_from_result = math.fsum(
        result.weights[part] * item.parts[part] for part in PART_ORDER
    )
    assert total_from_result == pytest.approx(item.score, abs=1e-9, rel=0)


def _all_scored(result) -> list:
    """进过五维重排的每一条（chosen + 被 below_k 淘汰的）。"""
    return list(result.chosen) + [item for item in result.dropped if item.score is not None]


# ---- 验收 2：`parts` 能加回总分 -------------------------------------------------


def test_五维分解加回总分_绝对容差1e9() -> None:
    """每个分量与总分都能单独取出，且五维加权和**逐位加回**总分。

    浮点容差口径是**绝对 1e-9、相对 0**（`rel=0` 是刻意的：默认的 `rel=1e-6`
    在 0.8 量级上等于 8e-7 的宽容度，那就钉不住"加回总分"这件事了）。
    """
    candidates = [
        _cand("a", raw=-3.0, age_days=2, use_count=2, kind="preference"),
        _cand("b", raw=-1.0, age_days=100, kind="fact"),
        _cand("c", raw=-2.0, age_days=5, use_count=1, kind="lesson"),
        _cand("d", raw=None, age_days=1, kind="skill"),
    ]
    result = rank(candidates, now=NOW, overrides={"recall_rerank_k": 4, "recall_limit": 4})
    print("parts 原始输出:", result.chosen[0].parts, "score:", result.chosen[0].score)
    print("reasons:", result.chosen[0].reasons)

    assert len(_all_scored(result)) == 4
    for item in _all_scored(result):
        assert set(item.parts) == set(PART_ORDER)
        assert set(item.reasons) == set(PART_ORDER)
        assert all(reason.strip() for reason in item.reasons.values())
        assert all(value >= 0.0 for value in item.parts.values())
        _sum_check(item, result)


# ---- 验收 1：五个维度真的在起作用 -----------------------------------------------


def test_权重可分辨_相关度与使用频次对调改变顺序() -> None:
    """同一批候选：默认权重看相关度，把权重换成使用频次之后顺序反过来。

    第三段是"所有权重归零 → 顺序 = 输入顺序（稳定排序）"：输入按第①阶段口径
    （`bm25` 升序 = 最相关在前）给，所以归零之后取到的就是输入序，而不是某个
    偶然顺序（tie-break 是 `Scored.order`）。
    """
    candidates = [
        _cand("best-rel", raw=-8.0, age_days=1, kind="fact"),
        _cand("mid-rel", raw=-4.0, age_days=1, kind="fact"),
        _cand("most-used", raw=-0.5, age_days=1, use_count=5, kind="fact"),
    ]
    default = rank(candidates, now=NOW, overrides=dict(TIGHT))
    assert default.chosen_refs == ("best-rel", "mid-rel", "most-used")

    by_usage = rank(
        candidates,
        now=NOW,
        overrides={"recall_rerank_k": 3, "recall_limit": 3, "rank_w_relevance": 0, "rank_w_usage": 1},
    )
    assert by_usage.chosen_refs == ("most-used", "best-rel", "mid-rel")
    assert by_usage.chosen[0].parts["use"] == 1.0  # 用得最多的那条恰好是 1.0

    zeroed = rank(
        candidates,
        now=NOW,
        overrides={
            "recall_rerank_k": 3,
            "recall_limit": 3,
            "rank_w_relevance": 0,
            "rank_w_recency": 0,
            "rank_w_importance": 0,
            "rank_w_usage": 0,
            "rank_w_penalty": 0,
        },
    )
    assert zeroed.chosen_refs == ("best-rel", "mid-rel", "most-used")  # = 输入顺序
    assert zeroed.chosen_refs == tuple(item.ref for item in candidates)


def test_一次使用不吃掉全部权重() -> None:
    """`use` 是 `log1p` 归一化，不是 `use_count * 0.6` 的线性乘（旧口径）。

    场景：一条**用了 5 次、但 200 天前**的旧条目 vs 一条**新写、没用过**的条目。
    新口径下旧条目的 `use` 拿到满值 1.0，可它的加权贡献被 `rank_w_usage=0.10`
    封在 0.10 之内，压不过新近度（0.20）那一维，所以**新条目排在前面**。

    这条用例同时钉住"归一化有上界"：任何一条的 `parts["use"] <= 1.0`。
    把实现改回 `use_count * 0.6`（旧口径）→ 旧条目的 `use` 变成 3.0、贡献 0.30，
    它反超新条目 → 本条红（两个断言都会红）。
    """
    fresh = _cand("fresh", raw=-1.0, age_days=1, use_count=0, kind="fact")
    old_but_often_used = _cand("old", raw=-1.0, age_days=200, use_count=5, kind="fact")
    one_use = _cand("one-use", raw=-1.0, age_days=100, use_count=1, kind="fact")

    result = rank(
        [fresh, old_but_often_used, one_use],
        now=NOW,
        overrides={"recall_rerank_k": 3, "recall_limit": 3},
    )
    assert result.max_use == 5
    assert result.chosen_refs[0] == "fresh"
    by_ref = {item.candidate.ref: item for item in result.chosen}
    assert by_ref["fresh"].score > by_ref["old"].score
    assert by_ref["fresh"].score > by_ref["one-use"].score
    for item in _all_scored(result):
        assert item.parts["use"] <= 1.0
        assert item.weights["use"] * item.parts["use"] <= item.weights["use"]


# ---- 验收 3：两阶段（N 生效 + 每条被丢的都有 reason）---------------------------


def test_两阶段_候选池只留最相关的N条_其余带reason() -> None:
    """60 条候选、**乱序**传进来、N=50、K=5：只有最相关的 50 条进重排。

    乱序是刻意的：`over_budget` 的那 10 条必须正好是**相关度最低**的 10 条，
    才能证明第①阶段（bm25 升序）在截断之前真的排过一遍，而不是"切输入的前 50 条"。
    同时钉住**问责**：`chosen + dropped` 恰好是输入那 60 条，一条不多一条不少。
    """
    candidates = [
        _cand(f"c{index:02d}", raw=-(60 - index) / 10.0) for index in range(60)
    ]
    shuffled = list(reversed(candidates))  # c59（最不相关）在前
    result = rank(
        shuffled,
        now=NOW,
        overrides={"recall_candidates_n": 50, "recall_rerank_k": 5, "recall_limit": 5},
    )

    assert result.pool_size == 60
    assert result.reranked == 50
    assert len(result.chosen) == 5
    assert len(result.dropped) == 55
    assert result.dropped_reasons == {"below_k": 45, "over_budget": 10}

    over_budget = {item.candidate.ref for item in result.dropped if item.reason == "over_budget"}
    assert over_budget == {f"c{index:02d}" for index in range(50, 60)}
    assert set(result.chosen_refs) <= {f"c{index:02d}" for index in range(50)}

    for item in result.dropped:
        assert item.reason in DROPPED_REASONS
        assert item.detail.strip()
    for item in result.dropped:
        if item.reason != "below_k":
            # 候选池阶段就淘汰的没算过分：如实留空，不编一个 0 分的分数
            assert item.score is None and item.parts == {}

    accounted = [item.candidate.ref for item in result.chosen] + [
        item.candidate.ref for item in result.dropped
    ]
    assert sorted(accounted) == sorted(item.ref for item in candidates)


# ---- 验收 4：硬约束 `K ≤ recall_limit` -----------------------------------------


def test_rerank_k_硬约束_越界夹取且可见() -> None:
    """`recall_rerank_k=10` 而 `recall_limit=5` → **夹到 5**，并把这件事说出来。

    钉住的行为是**夹取**（不是报错）：硬约束写在 `limits` 的 note 里（"重排只能
    减少进上下文的条数、不能增加"），`recall` 跑在会话热路径上，配置写错不该把
    主循环炸掉——但夹取必须可见，所以 `k_requested` / `k` / `k_note` 三个字段
    一起把它说清楚。去掉夹取 → `chosen` 变 10 条 → 本条红。
    """
    candidates = [_cand(f"c{index}", raw=-(10 - index) / 10.0) for index in range(10)]
    clamped = rank(
        candidates,
        now=NOW,
        overrides={"recall_rerank_k": 10, "recall_limit": 5},
    )
    assert clamped.k_requested == 10
    assert clamped.limit == 5
    assert clamped.k == 5
    assert len(clamped.chosen) == 5
    assert len(clamped.chosen) <= clamped.limit
    assert "夹取" in clamped.k_note and "recall_limit=5" in clamped.k_note

    within = rank(candidates, now=NOW, overrides={"recall_rerank_k": 3, "recall_limit": 5})
    assert within.k == 3 and within.k_note == "" and len(within.chosen) == 3

    # `recall_limit=0` 是合法配置（"一条都不进上下文"）：K 夹到 0，五维分照算、
    # 每条都带 below_k 的理由，没有一条被静默丢掉。
    none = rank(candidates, now=NOW, overrides={"recall_rerank_k": 5, "recall_limit": 0})
    assert none.k == 0 and none.chosen == ()
    assert none.dropped_reasons == {"below_k": 10}
    assert len(none.dropped) == len(candidates)


# ---- 验收 5：旋钮可覆盖（半衰期） ----------------------------------------------


def test_旋钮可覆盖_半衰期改变rec分量() -> None:
    """`.agent/limits.json` 覆盖 `rank_recency_half_life_days=7` → `rec` 可观测变化。

    `sources` 要能说出这个数是从哪来的（"默认" / "配置"），否则调参就是猜。
    """
    candidate = _cand("aged", raw=-1.0, age_days=30, kind="fact")
    default = rank([candidate], now=NOW, overrides={"recall_rerank_k": 1})
    assert default.chosen[0].parts["rec"] == pytest.approx(math.exp(-1.0), abs=1e-12, rel=0)
    assert default.sources["rank_recency_half_life_days"] == "默认"

    shorter = rank(
        [candidate],
        now=NOW,
        overrides={"recall_rerank_k": 1, "rank_recency_half_life_days": 7},
    )
    assert shorter.chosen[0].parts["rec"] == pytest.approx(math.exp(-30 / 7), abs=1e-12, rel=0)
    assert shorter.chosen[0].parts["rec"] < default.chosen[0].parts["rec"]
    assert shorter.sources["rank_recency_half_life_days"] == "配置"


# ---- 验收 6：边界 ---------------------------------------------------------------


def test_边界_空候选_零使用_零惩罚() -> None:
    """空候选不抛；`max_use == 0` 不除零；`use_count=0` 时 `penalty` 有定义。"""
    empty = rank([], now=NOW)
    assert empty.chosen == () and empty.dropped == ()
    assert empty.pool_size == 0 and empty.reranked == 0 and empty.max_use == 0

    unused = [_cand("u1", raw=-2.0), _cand("u2", raw=-1.0)]
    result = rank(unused, now=NOW, overrides=dict(TIGHT))
    assert result.max_use == 0
    for item in _all_scored(result):
        assert item.parts["use"] == 0.0
        assert "max_use=0" in item.reasons["use"]
        assert item.parts["penalty"] == 0.0
        assert "无负反馈数据" in item.reasons["penalty"]

    # 负数 use_count 是脏数据：按 0 夹取，且 reason 里点名夹过（不静默）
    dirty = [_cand("neg", raw=-2.0, use_count=-3), _cand("pos", raw=-1.0, use_count=2)]
    dirty_result = rank(dirty, now=NOW, overrides=dict(TIGHT))
    assert dirty_result.max_use == 2
    neg = next(item for item in _all_scored(dirty_result) if item.candidate.ref == "neg")
    assert neg.parts["use"] == 0.0
    assert "夹取" in neg.reasons["use"]

    # 没有时间戳（created_at=0 且没有 last_used_at）→ rec 如实为 0，并且说得出来
    timeless = rank([_cand("t", raw=-1.0, created_at=0.0)], now=NOW, overrides={"recall_rerank_k": 1})
    assert timeless.chosen[0].parts["rec"] == 0.0
    assert "没有 last_used_at" in timeless.chosen[0].reasons["rec"]


# ---- `penalty`：没有数据就是 0，有数据源就按公式扣 ------------------------------


def test_penalty_无数据源时是数据驱动的0_有源时按公式扣分(monkeypatch) -> None:
    """`penalty` **不是写死的 0**：数据源一开，`applied_count/(use_count+1)` 立刻生效。

    P1 没有负反馈数据的写入者（`semantic.applied_count` 恒为 0），所以默认态下
    两条只差 `applied_count` 的候选**分数完全相同**、reason 写明"无负反馈数据"。
    把 `NEGATIVE_FEEDBACK_SOURCE` 打开（模拟 T13 开始记"推给了但没被采纳"），
    同一条候选立刻被扣分，且扣的量正好是 `w_pen · applied/(use+1)`。
    """
    twin_a = _cand("a", raw=-1.0, use_count=1, applied_count=0)
    twin_b = _cand("b", raw=-1.0, use_count=1, applied_count=3)
    default = rank([twin_a, twin_b], now=NOW, overrides=dict(TIGHT))
    by_ref = {item.candidate.ref: item for item in default.chosen}
    assert by_ref["a"].parts["penalty"] == 0.0 and by_ref["b"].parts["penalty"] == 0.0
    assert by_ref["a"].score == pytest.approx(by_ref["b"].score, abs=1e-12, rel=0)
    assert "无负反馈数据" in by_ref["b"].reasons["penalty"]

    monkeypatch.setattr(rank_module, "NEGATIVE_FEEDBACK_SOURCE", "用例注入的负反馈源")
    with_source = rank([twin_a, twin_b], now=NOW, overrides=dict(TIGHT))
    scored = {item.candidate.ref: item for item in with_source.chosen}
    assert scored["a"].parts["penalty"] == 0.0
    assert scored["b"].parts["penalty"] == pytest.approx(3 / 2, abs=1e-12, rel=0)
    drop = scored["a"].score - scored["b"].score
    assert drop == pytest.approx(-scored["b"].weights["penalty"] * 3 / 2, abs=1e-9, rel=0)
    assert "用例注入的负反馈源" in scored["b"].reasons["penalty"]


# ---- `rel`：用 T5 钉住的 `bm25_score` 走 §6.1 的归一化，不直接用原值 -------------


def test_rel_用归一化公式_不直接用原始bm25原值() -> None:
    """`rel = 1 − exp(−w_bm25·bm25_score)`，`bm25_score = -bm25()`。

    三条边界一起钉：正常值走公式；`bm25_raw is None`（LIKE 回退）如实为 0；
    `bm25_score` 为负（该词 IDF 为负）时公式给出负数 → 夹到 0 并写明原因。
    把 `rel` 改成直接用 `bm25_raw`（负值）或直接用 `bm25_score`（正值）→ 本条红。
    """
    normal = _cand("n", raw=-1.0, kind="fact")
    result = rank([normal], now=NOW, overrides={"recall_rerank_k": 1})
    assert result.chosen[0].parts["rel"] == pytest.approx(1 - math.exp(-1.0), abs=1e-12, rel=0)

    scaled = rank(
        [normal], now=NOW, overrides={"recall_rerank_k": 1, "w_bm25": 0.5}
    )
    assert scaled.chosen[0].parts["rel"] == pytest.approx(1 - math.exp(-0.5), abs=1e-12, rel=0)
    assert scaled.sources["w_bm25"] == "配置"

    no_raw = rank([_cand("fallback", raw=None)], now=NOW, overrides={"recall_rerank_k": 1})
    assert no_raw.chosen[0].parts["rel"] == 0.0
    assert "无 bm25 原值" in no_raw.chosen[0].reasons["rel"]

    # bm25() 原值为正（IDF 为负的常见词）→ bm25_score 为负 → 夹到 0
    negative = rank([_cand("neg-idf", raw=0.7)], now=NOW, overrides={"recall_rerank_k": 1})
    assert negative.chosen[0].parts["rel"] == 0.0
    assert "夹取到 0" in negative.chosen[0].reasons["rel"]


# ---- 淘汰理由：superseded / deduped，以及 P1 没有数据源的两个取值 ---------------


def test_superseded被丢_archived不丢_链单元不过滤() -> None:
    """`superseded` 有数据源（行自己声明被取代）；`archived` 没有裁决说不该召回。

    事件链单元（T11）**不过滤** `superseded`：链天然含被取代的旧 state 行
    （§15.4），把链内部的行按单条口径过滤掉会让链缺环。
    """
    candidates = [
        _cand("old-superseded", raw=-5.0, status="superseded"),
        _cand("superseded-by", raw=-4.0, superseded_by=7),
        _cand("archived", raw=-3.0, status="archived"),
        _cand("chain", raw=-2.0, unit="event_chain", status="superseded"),
        _cand("live", raw=-1.0),
    ]
    result = rank(candidates, now=NOW, overrides={"recall_rerank_k": 5, "recall_limit": 5})
    reasons = {item.candidate.ref: item.reason for item in result.dropped}
    assert reasons == {"old-superseded": "superseded", "superseded-by": "superseded"}
    assert result.dropped_reasons == {"superseded": 2}
    assert set(result.chosen_refs) == {"archived", "chain", "live"}
    for item in result.dropped:
        assert item.reason in DROPPED_REASONS

    # P1 里这两个理由**没有数据源**：如实登记，而不是编一条候选来把它用掉
    assert set(REASONS_WITHOUT_SOURCE_IN_P1) == {"demoted", "conflict"}
    assert set(REASONS_WITHOUT_SOURCE_IN_P1) < set(DROPPED_REASONS)


def test_去重先于候选池截断_保留更相关的一条() -> None:
    """合并去重**在 N 截断之前**：重复条目不该占掉候选池预算。

    A1/A2 同 `content_hash`（A1 更相关），另有 B、C、D。`recall_candidates_n=3`：

    - 去重先于截断 → 去重后 A1,B,C,D → 取 3 条 = A1,B,C（**C 进得了重排**）；
    - 若先截断再去重 → A1,A2,B → 去重后只剩 A1,B（C 被挤掉）。
    """
    candidates = [
        _cand("A1", raw=-5.0, content_hash="dup"),
        _cand("A2", raw=-4.9, content_hash="dup"),
        _cand("B", raw=-4.0),
        _cand("C", raw=-3.0),
        _cand("D", raw=-2.0),
    ]
    result = rank(
        candidates,
        now=NOW,
        overrides={"recall_candidates_n": 3, "recall_rerank_k": 5, "recall_limit": 5},
    )
    assert result.reranked == 3
    deduped = [item for item in result.dropped if item.reason == "deduped"]
    assert len(deduped) == 1
    assert deduped[0].candidate.ref == "A2"  # 保留更相关的那条
    assert "A1" in deduped[0].detail
    assert deduped[0].score is None and deduped[0].parts == {}  # 没算过就不编分数
    assert "C" in result.chosen_refs  # 去重没吃掉候选池预算
    assert {item.reason for item in result.dropped} == {"deduped", "over_budget"}

    # 去重键为空（文本、content_hash、dedupe_key 都空）的候选**不合并**：
    # 把一批空条目并成一条才是真的静默丢东西
    empty = [_cand("e1", raw=-2.0, text=""), _cand("e2", raw=-1.0, text="")]
    empty_result = rank(empty, now=NOW, overrides=dict(TIGHT))
    assert empty_result.reranked == 2 and empty_result.dropped == ()

    # 显式 `dedupe_key` 优先于 content_hash
    explicit = rank(
        [_cand("k1", raw=-3.0, dedupe_key="K"), _cand("k2", raw=-1.0, dedupe_key="K")],
        now=NOW,
        overrides=dict(TIGHT),
    )
    assert explicit.dropped_reasons == {"deduped": 1}


# ---- `imp`：基线 / 用户标记 / +0.1 ---------------------------------------------


def test_imp_基线_用户标记_验证加成() -> None:
    """kind 基线照 §6.1 表；判定值优先；用户改过的标 `(user)`；验证/取代 +0.1。

    `+0.1` 的歧义按「口径裁决 8」解释：**成功取代过别人**或**通过验证**的 +0.1，
    被取代的旧条目**不加分**（所以 `superseded_other` 与 `verified` 是两个入参）。
    """
    baselines = {
        "fact": 0.6,
        "decision": 0.6,
        "preference": 0.7,
        "lesson": 0.5,
        "skill": 0.5,
    }
    for kind, expected in baselines.items():
        item = rank(
            [_cand(kind, raw=-1.0, kind=kind)], now=NOW, overrides={"recall_rerank_k": 1}
        ).chosen[0]
        assert item.parts["imp"] == pytest.approx(expected, abs=1e-12, rel=0)

    unregistered = rank(
        [_cand("x", raw=-1.0, kind="episode")], now=NOW, overrides={"recall_rerank_k": 1}
    ).chosen[0]
    assert unregistered.parts["imp"] == pytest.approx(0.5, abs=1e-12, rel=0)
    assert "未登记" in unregistered.reasons["imp"]

    user_set = rank(
        [_cand("u", raw=-1.0, kind="fact", importance=0.9, importance_user_set=True)],
        now=NOW,
        overrides={"recall_rerank_k": 1},
    ).chosen[0]
    assert user_set.parts["imp"] == pytest.approx(0.9, abs=1e-12, rel=0)
    assert "(user)" in user_set.reasons["imp"]  # §6.3：不许数字裸奔

    verified = rank(
        [_cand("v", raw=-1.0, kind="fact", verified=True)],
        now=NOW,
        overrides={"recall_rerank_k": 1},
    ).chosen[0]
    assert verified.parts["imp"] == pytest.approx(0.6 + IMPORTANCE_VERIFIED_BONUS, abs=1e-12, rel=0)
    assert "通过验证" in verified.reasons["imp"]

    replaced_other = rank(
        [_cand("r", raw=-1.0, kind="fact", superseded_other=True)],
        now=NOW,
        overrides={"recall_rerank_k": 1},
    ).chosen[0]
    assert replaced_other.parts["imp"] == pytest.approx(0.7, abs=1e-12, rel=0)
    assert "成功取代过别人" in replaced_other.reasons["imp"]


# ---- 渲染地基（T13 用）与"不读时钟" --------------------------------------------


def test_explain_打出五维分解() -> None:
    """§6.1 的分解行：五个分量的**加权贡献** + 总分（T13 三出口渲染同一份数据）。"""
    item = rank(
        [_cand("e", raw=-3.0, age_days=2, use_count=1, kind="preference")],
        now=NOW,
        overrides={"recall_rerank_k": 1},
    ).chosen[0]
    line = item.explain()
    assert line.startswith("分 ") and " = " in line
    for label in ("相关", "新近", "重要", "频次", "反馈"):
        assert label in line
    assert "− 反馈" in line  # penalty 是减项，渲染成减号


def test_不读时钟_now由调用方给(monkeypatch) -> None:
    """`rank` 不读时钟（`now` 由调用方给，接 T8 的可注入时钟）。"""
    def _boom() -> float:
        raise AssertionError("rank 不该读时钟：now 必须由调用方给")

    monkeypatch.setattr(time_module, "time", _boom)
    result = rank([_cand("c", raw=-1.0)], now=NOW)
    assert result.chosen_refs == ("c",)


# ---- 非法旋钮：显式报错，不静默算出一个看着正常的排名 --------------------------


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"rank_recency_half_life_days": 0}, "必须为正"),
        ({"rank_w_usage": -1}, "不能为负"),
        ({"w_bm25": -1}, "不能为负"),
        ({"recall_candidates_n": 0}, "至少为 1"),
        ({"recall_rerank_k": -1}, "不能为负"),
        ({"recall_limit": -1}, "不能为负"),
    ],
)
def test_非法旋钮显式报错(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        rank([_cand("c", raw=-1.0)], now=NOW, overrides=overrides)
    assert message in str(excinfo.value)


# ---- 接真实行：`Candidate.from_semantic` ---------------------------------------


def test_from_semantic_接真实行_并把P1缺口如实留下(tmp_path: Path) -> None:
    """`search_semantic` 的行能直接变成候选；两处 P1 缺口如实留空、不假装。

    - `imp` 读的是 `semantic.importance` 列（T2 的建表默认 0.5 = "写入时未判定"）；
      **写入时按 kind 基线判定是 T7 的事**，T6 不自作主张把 0.5 改成 0.6；
    - `superseded_other`（"成功取代过别人"）要反查"谁指向我"，P1 没有这条数据源
      → 那半个 `+0.1` 今天不会生效。
    """
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    older = insert_semantic(conn, "fact", "构建命令是 pytest -q", content_hash="t6-a")
    newer = insert_semantic(conn, "preference", "归档前先备份", content_hash="t6-b")
    for _ in range(2):
        touch_semantic(conn, newer.id)

    rows = search_semantic(conn, "备份 构建", limit=5)
    assert {row.id for row in rows} == {older.id, newer.id}
    candidates = [Candidate.from_semantic(row) for row in rows]
    assert {item.ref for item in candidates} == {f"semantic:{older.id}", f"semantic:{newer.id}"}
    assert all(item.bm25_raw is not None for item in candidates)  # 原值带出来了

    result = rank(candidates, now=max(row.created_at for row in rows) + 60.0, overrides=dict(TIGHT))
    assert result.chosen_refs  # 真行能排
    older_scored = next(
        item for item in _all_scored(result) if item.candidate.ref == f"semantic:{older.id}"
    )
    assert older_scored.parts["imp"] == pytest.approx(0.5, abs=1e-12, rel=0)
    assert "(user)" not in older_scored.reasons["imp"]
    assert all(item.superseded_other is False for item in candidates)
    conn.close()
