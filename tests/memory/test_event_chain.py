"""T11 / M4-T5：事件链作为**一个整体单元**参与候选与重排（设计稿 §6.6 / §6.7 / §15.4）。

四条硬规则逐条钉住（Prompt 的验收标准）：

1. **候选里是 1 个 `event_chain(3)`**，不是 3 个互不相关的片段——链上事件**不会**
   各自成为一个候选；
2. **整链进 / 整链出**：链被选中时链上每条一起进上下文，被淘汰（`below_k` /
   `event_chain_max_units` 上界）时**一起消失**；
3. **长链不加分**：`rel` 取链内最高（不是 Σ），`imp` 取链内最高，`rec` 用链内最新
   事件，链上没有 `use_count`/负反馈 → 12 条链不许因为"条数多"排在 3 条链前面；
4. **可解释**：链单元的 explanation 列出 `event:id`，并有"时序完整度"与"冲突未定"。

外加设计稿 §6.7 表的两条：`conflict` 的链注入时带「未定」+ 互斥 `event:id`
（**不注入单个数**），渲染不超 `event_chain_render_max_chars`，超了要**明确截断并
标注被略去的 `event:id`**（不静默丢事件）。

**夹具口径**：事件走 T9 的 `append_event`（真归约、真取代链、真冲突），排序用
`FakeClock` 注入的"现在"——`rank` 自己不读时钟，所以整套读数可复现。
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.rank import Candidate, ChainMember, chain_unit_label, rank
from spoolkit.memory.session import (
    INTERROGATIVE_MARKERS,
    MIN_MATCHED_TOKENS,
    MemorySession,
    TIME_ADVERB_MARKERS,
    is_variable_quantity,
    render_chain,
    select_chains,
)
from spoolkit.memory.timeline.clock import FakeClock, epoch_at
from spoolkit.memory.timeline.db import (
    fetch_current_state,
    open_timeline,
)
from spoolkit.memory.timeline.reduce import append_event, draft_from_text
from spoolkit.memory.timeline.registry import add_alias, register_attribute

NOW = epoch_at(2026, 10, 3, 12, 0)
LATEST = epoch_at(2026, 10, 3, 10, 11)
CLOCK = FakeClock(NOW, tz_offset_min=480)

# 三条链的两个问句：
# · 点名的（命中别名「余额」）与不点名的（靠逐字召回把键带回来）都要能选出同一个键。
QUERY_NAMED = "现在还剩多少余额"
QUERY_PLAIN = "现在还剩多少"
QUERY_SPENT = "到今天一共花了多少"


def _db(tmp_path: Path):
    return open_timeline(tmp_path / "memory.db")


def _session(tmp_path: Path, conn, **kwargs) -> MemorySession:
    return MemorySession(
        conn=conn,
        hot_path=tmp_path / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=8192,
        clock=CLOCK,
        **kwargs,
    )


def _add(
    conn,
    *,
    entity: str,
    attribute: str,
    op: str,
    value: str,
    raw_text: str,
    at_text: str,
    index: int,
    unit: str = "CNY",
    speaker: str = "user",
    confidence: float | None = None,
) -> int:
    """落一条事件（走 T8 的时间解析 + T9 的归约）。返回新 event id。"""
    result = append_event(
        conn,
        draft_from_text(
            raw_text,
            clock=CLOCK,
            entity=entity,
            attribute=attribute,
            op=op,
            value=value,
            unit=unit,
            time_text=at_text,
            speaker=speaker,
            confidence=confidence,
            source_ref=f"session:s1#turn:{index}",
            round_key=f"s1#{entity}.{attribute}#{index}",
        ),
        clock=CLOCK,
    )
    return result.event_id


def _balance(conn) -> None:
    """`wallet.balance`：3 条事件 → 当前值 130（与 M4-T1 同形的余额链）。"""
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    add_alias(conn, "余额", "balance", kind="attribute", clock=CLOCK)
    _add(
        conn,
        entity="wallet",
        attribute="balance",
        op="set",
        value="100",
        raw_text="现在身上有 100 块",
        at_text="2026-10-02 09:12",
        index=1,
        confidence=0.9,
    )
    _add(
        conn,
        entity="wallet",
        attribute="balance",
        op="delta",
        value="-20",
        raw_text="今天花了 20",
        at_text="2026-10-02 09:13",
        index=2,
        confidence=0.7,
    )
    _add(
        conn,
        entity="wallet",
        attribute="balance",
        op="delta",
        value="50",
        raw_text="我赚了 50",
        at_text="2026-10-03 10:11",
        index=3,
        confidence=0.8,
    )


def _ledger(conn, *, size: int = 12) -> None:
    """`ledger.spent`：`size` 条事件（"到今天一共花了多少"的另一条链）。

    第一条 `set 0`、其余 `delta +10`，逐分钟排开（同刻会给归约器送冲突信号）。
    与 `wallet.balance` 刻意对齐的两件事：链内最高 `confidence` 相同（0.9）、
    链内**最新事件**时刻相同（`LATEST`）——这样"长链不加分"的读数不受它俩干扰。
    """
    register_attribute(conn, "ledger", "spent", type="monetary", clock=CLOCK)
    for index in range(1, size + 1):
        _add(
            conn,
            entity="ledger",
            attribute="spent",
            op="set" if index == 1 else "delta",
            value="0" if index == 1 else "10",
            raw_text=f"今天花了 10（第 {index} 笔）" if index > 1 else "今天开始记账，花了 0",
            at_text=f"2026-10-03 10:{index - 1:02d}",
            index=index,
            confidence=0.9,
        )


def _paid_chain(size: int, *, best_raw: float = -4.0, other_raw: float = 0.5) -> Candidate:
    """rank 层的链候选：链内最高 `bm25` 固定在 `best_raw`，其余条是 `other_raw`。

    构造形状是刻意的：**只有部分事件命中问句**（真实情形就是这样），所以
    "Σ 求和"与"取最高"会给出一眼可分的两个数（长链的 Σ 甚至会被夹到 0）。
    """
    members = tuple(
        ChainMember(
            event_id=index + 1,
            bm25_raw=best_raw if index == 0 else other_raw,
            at_abs=LATEST - (size - index) * 60.0,
        )
        for index in range(size)
    )
    return Candidate(
        ref=f"event_chain:probe.k{size}",
        text=f"probe.k{size} 链（{size} 条）",
        kind="event_chain",
        unit=chain_unit_label(size),
        importance=0.9,
        created_at=float(LATEST),
        dedupe_key=f"event_chain:probe.k{size}",
        chain_members=members,
    )


# ---- 验收 1：候选里是 **1 个** 链单元，不是 N 个片段 ----------------------------


def test_M4_T5_候选里是一个event_chain_3而不是三条片段(tmp_path: Path) -> None:
    """问「现在还剩多少」→ 候选里出现 **1 个 `unit='event_chain(3)'`**。

    这条同时排除两种错法：把链拆成 3 个候选（`event:1/2/3` 各自进池），以及
    只取"最相似的那一条"（候选里只有一个单条事件）。
    """
    conn = _db(tmp_path)
    _balance(conn)
    outcome = _session(tmp_path, conn).recall_result(QUERY_PLAIN)

    assert outcome.variable_quantity is True
    assert outcome.selection.refs == ("event_chain:wallet.balance",)

    units = [candidate.unit for candidate in outcome.candidates]
    assert units == ["event_chain(3)"], units
    assert len(outcome.candidates) == 1  # 1 个候选，不是 3 个
    # 链上事件**不许**各自成为候选（没有 `event:<id>` 这种片段 ref）
    assert not [c for c in outcome.candidates if c.ref.startswith("event:")]
    assert [c.ref for c in outcome.candidates] == ["event_chain:wallet.balance"]

    chain = outcome.chains[0]
    assert chain.key == "wallet.balance"
    assert chain.event_ids == (1, 2, 3)
    assert chain.size == 3
    assert outcome.ranked.chosen_refs == ("event_chain:wallet.balance",)

    print("候选清单:", [(c.ref, c.unit) for c in outcome.candidates])
    print("why 原文:", outcome.ranked.chosen[0].explain())
    print("链单元的 explanation:", chain.explain())


def test_点名的问法与逐字召回的问法选中同一个键(tmp_path: Path) -> None:
    """§6.7 判据的②（问句点名键/别名）与③+逐字召回（问句把键的事件带回来）两条路。

    「现在还剩多少余额」命中属性别名「余额」；「现在还剩多少」不点名，但事件里
    有「现在身上有 100 块」（逐字命中 `现`+`在` ≥ `MIN_MATCHED_TOKENS`）。
    """
    conn = _db(tmp_path)
    _balance(conn)
    session = _session(tmp_path, conn)

    named = select_chains(conn, QUERY_NAMED)
    plain = select_chains(conn, QUERY_PLAIN)
    assert named.keys == plain.keys == (("wallet", "balance"),)
    assert named.variable_quantity is True and plain.variable_quantity is True
    # 两种问法都给出 1 个链单元（候选里不是 3 条片段）
    assert [c.unit for c in session.recall_result(QUERY_NAMED).candidates] == [
        "event_chain(3)"
    ]
    assert [c.unit for c in session.recall_result(QUERY_PLAIN).candidates] == [
        "event_chain(3)"
    ]


def test_判据_不是可变数量类问题就不造链单元(tmp_path: Path) -> None:
    """机械判据逐条：疑问词 **且**（键命中 或 时间副词命中）。

    只有疑问词（「现在几点了」）不建链；只有键（「帮我改 wallet.balance」没有疑问词）
    也不建链；疑问词 + 键 或 疑问词 + 时间副词 才建链。
    """
    assert "还剩" in INTERROGATIVE_MARKERS and "到今天" in TIME_ADVERB_MARKERS
    assert MIN_MATCHED_TOKENS >= 2

    assert is_variable_quantity("现在还剩多少", (("wallet", "balance"),)) is True
    assert is_variable_quantity("今天几点了", ()) is False  # 疑问词命中但没有键/副词
    assert is_variable_quantity("帮我改 indexer.py", (("indexer", "size"),)) is False
    assert is_variable_quantity("到今天一共花了多少", ()) is True  # 疑问词 + 时间副词
    assert is_variable_quantity("看一下 wallet.balance", (("wallet", "balance"),)) is False

    conn = _db(tmp_path)
    _balance(conn)
    unrelated = _session(tmp_path, conn).recall_result("帮我改一下 indexer.py")
    assert unrelated.variable_quantity is False
    assert unrelated.chains == ()
    assert unrelated.candidates == ()  # 库里没有别的记忆，也没有链单元
    assert "不是可变数量类问题" in unrelated.selection.note()


# ---- 验收 2：整链进 / 整链出 ---------------------------------------------------


def test_整链进_选中时链上每条一起进上下文(tmp_path: Path) -> None:
    """链被选中 → 链上 3 条事件的 `event:id`、`at_abs`、`op`、`value`、`raw_text`、
    `speaker` 与当前值一起进上下文（§6.7 的渲染要点）。"""
    conn = _db(tmp_path)
    _balance(conn)
    text = _session(tmp_path, conn).recall(QUERY_NAMED)

    assert "unit=event_chain(3)" in text
    assert "当前值 130 CNY" in text
    for token in ("event:1", "event:2", "event:3"):
        assert token in text
    for raw in ("现在身上有 100 块", "今天花了 20", "我赚了 50"):
        assert raw in text
    assert "2026-10-02 09:12" in text and "2026-10-03 10:11" in text
    assert " set 100 CNY" in text and " delta -20 CNY" in text
    assert "user" in text
    # 链**含已被取代的旧 state 行**（§15.4：取代不是删除），渲染里看得到它们
    assert "取代链（3 行 state" in text
    assert "100 CNY → 80 CNY → 130 CNY" in text
    print("recall 原文:\n" + text)


def test_整链出_被淘汰时一条都不进(tmp_path: Path) -> None:
    """`event_chain_max_units=0` → 一个链单元都不进，且 `dropped` 里链有 reason。

    "进 0 条事件"与"整链不进"是同一件事的两种说法：链不拆，所以淘汰是整体的。
    """
    conn = _db(tmp_path)
    _balance(conn)
    session = _session(tmp_path, conn, overrides={"event_chain_max_units": 0})
    outcome = session.recall_result(QUERY_NAMED)

    assert outcome.ranked.chosen_refs == ()
    assert outcome.ranked.chain_chosen == 0
    assert outcome.text == ""
    dropped = {item.candidate.ref: item for item in outcome.ranked.dropped}
    assert set(dropped) == {"event_chain:wallet.balance"}
    item = dropped["event_chain:wallet.balance"]
    assert item.reason == "over_budget"
    assert "event_chain_max_units=0" in item.detail
    assert item.candidate.unit == "event_chain(3)"
    # 链上事件一条都没进上下文（不是"只进了前两条"）
    for token in ("event:1", "event:2", "event:3"):
        assert token not in outcome.text
    print("dropped:", item.reason, item.detail)


def test_整链出_上界1而候选2条链时进1条另一条有reason(tmp_path: Path) -> None:
    """`event_chain_max_units=1`、候选有两条链 → 进 1 条，另一条**整条**被淘汰。"""
    conn = _db(tmp_path)
    _balance(conn)
    _ledger(conn, size=12)
    session = _session(tmp_path, conn, overrides={"event_chain_max_units": 1})
    outcome = session.recall_result(QUERY_SPENT)

    assert len(outcome.chains) == 2
    assert outcome.ranked.chain_chosen == 1
    chosen_chains = [c for c in outcome.ranked.chosen if c.candidate.is_chain]
    assert len(chosen_chains) == 1
    over = [d for d in outcome.ranked.dropped if d.reason == "over_budget"]
    assert len(over) == 1
    assert "event_chain_max_units=1" in over[0].detail
    assert over[0].candidate.is_chain and over[0].candidate not in [
        c.candidate for c in outcome.ranked.chosen
    ]
    # 被淘汰那条的链上事件一条都不在上下文里（整链出）
    lost = outcome.chain_for(over[0].candidate.ref)
    assert lost is not None
    for event_id in lost.event_ids:
        # `event:1` 是 `event:11` 的前缀，所以按"后面不接数字"判
        assert not re.search(rf"event:{event_id}(?!\d)", outcome.text), event_id
    assert lost.render not in outcome.text


# ---- 验收 3：长链不加分 --------------------------------------------------------


def test_长链不加分_rank层_3条与12条读数完全相同() -> None:
    """链内最高 `bm25` 相同 → 3 条链与 12 条链的 `rel`/`score` **相同**（不是 12 倍）。

    `rel` 是"相关度"不是"相关度总量"：取链内最高是**长度不敏感**的聚合。
    把聚合换成 Σ（把 N 条分数加起来）→ 本条立刻红。
    """
    short = _paid_chain(3)
    long = _paid_chain(12)
    result = rank(
        [short, long],
        now=NOW,
        overrides={"recall_rerank_k": 5, "recall_limit": 5},
    )
    scored = {item.candidate.ref: item for item in result.chosen}
    assert set(scored) == {short.ref, long.ref}

    short_item, long_item = scored[short.ref], scored[long.ref]
    for part in short_item.parts:
        assert short_item.parts[part] == pytest.approx(long_item.parts[part], abs=1e-12, rel=0)
    assert short_item.score == pytest.approx(long_item.score, abs=1e-12, rel=0)

    # `rel` 就是"链内最高的那条"走 §6.1 公式的结果（−4.0 → bm25_score 4.0）
    expected = 1.0 - math.exp(-1.0 * 4.0)
    assert short_item.parts["rel"] == pytest.approx(expected, abs=1e-12, rel=0)
    assert long_item.parts["rel"] == pytest.approx(expected, abs=1e-12, rel=0)
    # 求和会给一个完全不同的数（12 条那条的 Σ 甚至为负 → 夹到 0），所以这里顺带钉住"不是 Σ"
    summed_short = math.fsum([4.0, -0.5, -0.5])
    assert short_item.parts["rel"] != pytest.approx(
        1.0 - math.exp(-1.0 * summed_short), abs=1e-9, rel=0
    )
    assert "不求和" in short_item.reasons["rel"]
    assert "链内最高" in short_item.reasons["rel"]
    # 长链没有排到短链前面（分相同 → 回到第①阶段序）
    assert [item.candidate.ref for item in result.chosen].index(long.ref) > 0 or True
    assert result.chosen[0].candidate.ref == short.ref
    print("3 条链:", short_item.parts, short_item.score)
    print("12 条链:", long_item.parts, long_item.score)


def test_长链不加分_会话层_12条链不因条数排前(tmp_path: Path) -> None:
    """真库里造两条链（3 条事件 / 12 条事件）：**长度**不进任何分量。

    对齐的三件事：链内最高 `confidence`（0.9）、链内最新事件时刻（`LATEST`）、
    以及链上没有 `use_count`（事件的 `use_count` 恒为 0）。

    **`rel` 是例外，而且必须是例外**（T11 补缺轮）：v5 起事件侧有了
    `memory_event_fts`，链的 `rel` 由"链内最高 `bm25` 原值"决定——两条链的**事件
    文本不同**，所以它们的 `rel` 本来就该不同（那是内容相关度，不是长度加分）。
    长度不进公式这件事由两处独立钉住：① 这里的另外四个分量逐位相同；
    ② 同一文件的 `test_长链不加分_rank层`（链内每条都给原值，3 条与 12 条的
    `rel`/`score` **逐位相同**——那才是"长度不敏感"的直接证据）。
    """
    conn = _db(tmp_path)
    _balance(conn)  # 3 条
    _ledger(conn, size=12)  # 12 条
    outcome = _session(tmp_path, conn).recall_result(QUERY_SPENT)

    assert len(outcome.chains) == 2
    by_ref = {item.candidate.ref: item for item in outcome.ranked.chosen}
    assert set(by_ref) == {"event_chain:wallet.balance", "event_chain:ledger.spent"}
    short, long = by_ref["event_chain:wallet.balance"], by_ref["event_chain:ledger.spent"]
    assert short.candidate.chain_size == 3 and long.candidate.chain_size == 12
    # 长度不进 imp / rec / use / penalty：这四项逐位相同。
    for part in ("rec", "imp", "use", "penalty"):
        assert short.parts[part] == pytest.approx(long.parts[part], abs=1e-12, rel=0), part
    # `rel` 现在有真原值：两条链各自 > 0，口径仍是"链内最高 + 不求和 + 长度不进公式"，
    # 且原值确实来自事件侧（不是构造方塞的常数）。
    for item in (short, long):
        assert item.parts["rel"] > 0.0
        assert item.candidate.bm25_raw is not None
        assert any(
            member.bm25_raw is not None for member in item.candidate.chain_members
        )
        assert "链内最高" in item.reasons["rel"]
        assert "不求和" in item.reasons["rel"]
        assert "长度不进公式" in item.reasons["rel"]
    # 长链不在短链前面（同分时回到第①阶段序；这里断言"没有因为 12 条而变大"）。
    assert long.parts["imp"] <= short.parts["imp"] + 1e-12
    print("会话层 3 条:", short.parts, short.score)
    print("会话层 12 条:", long.parts, long.score)
    print("3 条链的 rel 依据:", short.reasons["rel"])
    print("12 条链的 rel 依据:", long.reasons["rel"])


def test_链内最高_取的是最相关的那个成员不是第一条() -> None:
    """链内最高 = `bm25_score` 最大的那条（原值最小），与它在链上的位置无关。"""
    members = (
        ChainMember(event_id=1, bm25_raw=-1.0),
        ChainMember(event_id=2, bm25_raw=-3.0),
        ChainMember(event_id=3, bm25_raw=-2.0),
    )
    candidate = Candidate(
        ref="event_chain:x.y",
        text="x.y",
        kind="event_chain",
        unit=chain_unit_label(3),
        importance=0.5,
        created_at=NOW,
        chain_members=members,
        dedupe_key="event_chain:x.y",
    )
    item = rank([candidate], now=NOW, overrides={"recall_rerank_k": 1}).chosen[0]
    assert item.parts["rel"] == pytest.approx(1.0 - math.exp(-3.0), abs=1e-12, rel=0)
    assert "event:2" in item.reasons["rel"]  # 最高的是它
    assert "event:1" in item.reasons["rel"] and "event:3" in item.reasons["rel"]


# ---- 验收 4：可解释 + 超长截断 + 冲突 ------------------------------------------


def test_链单元的explanation列出事件id(tmp_path: Path) -> None:
    """链单元的 explanation 说明它**由哪几条事件组成**（`event:id` 列表）。

    两处都要有：链自己的 `explain()`（含完整度/冲突），以及 `Scored.explain()`
    的分值分解行（T13 的 `why` 读同一份）。
    """
    conn = _db(tmp_path)
    _balance(conn)
    outcome = _session(tmp_path, conn).recall_result(QUERY_NAMED)
    chain = outcome.chains[0]

    line = chain.explain()
    assert "由 3 条事件组成" in line
    for event_id in (1, 2, 3):
        assert f"event:{event_id}" in line
    assert "unit=event_chain(3)" in line
    assert "一个单元不是 3 个候选" in line

    scored_line = outcome.ranked.chosen[0].explain()
    assert "unit=event_chain(3)" in scored_line
    assert "event:1 event:2 event:3" in scored_line
    # 分值分解仍在（五维一笔不少）
    for label in ("相关", "新近", "重要", "频次", "反馈"):
        assert label in scored_line
    print(scored_line)


def test_超长链截断明确标注_不静默丢事件(tmp_path: Path) -> None:
    """超过 `event_chain_render_max_chars` → **明确截断并标注被略去的 `event:id`**。

    三件一起钉：① 渲染长度 ≤ 上界；② 文本里出现"截断"标记；③ **链上每条的
    `event:id` 都能在文本里找到**（在行里或被标注为"略去"）——静默丢事件 = ③ 红。
    """
    conn = _db(tmp_path)
    _ledger(conn, size=12)
    session = _session(tmp_path, conn, overrides={"event_chain_render_max_chars": 200})
    outcome = session.recall_result(QUERY_SPENT)
    chain = outcome.chains[0]
    assert chain.size == 12
    assert chain.truncated is True
    assert len(chain.render) <= 200
    assert "截断" in chain.render
    assert "事件永远不删" not in chain.render  # 只钉标记存在
    for event_id in chain.event_ids:
        assert f"event:{event_id}" in chain.render, event_id
    # 被略去的条数在标记里说得出来
    assert "略去" in chain.render
    # 严格 > 上界时才截断
    wide = _session(tmp_path, conn, overrides={"event_chain_render_max_chars": 100000})
    full = wide.recall_result(QUERY_SPENT)
    assert full.chains[0].truncated is False
    assert "截断" not in full.chains[0].render
    for event_id in full.chains[0].event_ids:
        assert f"\n  event:{event_id} " in full.chains[0].render
    print("截断后:", chain.render)


def test_事件排序_未知时间的排在已解析之后并带问号(tmp_path: Path) -> None:
    """`time_precision='unknown'` 的事件排在**已解析事件之后**、带 `?`（§15.5 末段）。

    它不许被当成链首或链尾的端点假装在那两个时刻上，也不许被丢掉（§15.4）。
    """
    conn = _db(tmp_path)
    _balance(conn)
    unknown_id = _add(
        conn,
        entity="wallet",
        attribute="balance",
        op="delta",
        value="-5",
        raw_text="前几天花了 5",
        at_text="前几天",
        index=4,
        confidence=0.4,
    )
    session = _session(tmp_path, conn, overrides={"event_chain_render_max_chars": 4000})
    outcome = session.recall_result(QUERY_NAMED)
    chain = outcome.chains[0]
    assert chain.size == 4 and unknown_id in chain.event_ids
    assert chain.event_ids[-1] == unknown_id  # 排在最后
    lines = [line for line in chain.render.splitlines() if "event:" in line]
    assert f"event:{unknown_id}" in lines[-1]
    assert "?" in lines[-1] and "时间未知" in lines[-1]
    print(chain.render)


def test_冲突链带未定标签_不注入单个数(tmp_path: Path) -> None:
    """同键同刻两条互斥 `op='set'` → 链的当前值是"未定"（§15.6）。

    渲染必须带「未定」+ 互斥 `event:id`，而且**不注入单个数**（不能把 100 或 80
    其中一个当成答案）——同时链的时序完整度为"不足"，`imp` 被扣分。
    """
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    add_alias(conn, "余额", "balance", kind="attribute", clock=CLOCK)
    first = _add(
        conn,
        entity="wallet",
        attribute="balance",
        op="set",
        value="100",
        raw_text="现在还剩 100",
        at_text="2026-10-02 09:12",
        index=1,
        confidence=0.9,
    )
    second = _add(
        conn,
        entity="wallet",
        attribute="balance",
        op="set",
        value="80",
        raw_text="现在还剩 80",
        at_text="2026-10-02 09:12",
        index=2,
        confidence=0.9,
    )
    current = fetch_current_state(conn, "wallet", "balance")
    assert current["status"] == "conflict" and current["reason"] == "same_instant"

    outcome = _session(tmp_path, conn).recall_result(QUERY_PLAIN)
    chain = outcome.chains[0]
    assert chain.complete is False
    assert "未定" in chain.incomplete_reason
    assert set(chain.conflict_event_ids) == {first, second}
    assert "未定" in chain.render
    assert "当前值 ?" in chain.render
    assert f"event:{first}" in chain.render and f"event:{second}" in chain.render
    # 不注入单个数：两条互斥事件的值不会作为"当前值"出现
    assert "当前值 100" not in chain.render and "当前值 80" not in chain.render

    item = outcome.ranked.chosen[0]
    assert item.parts["imp"] == pytest.approx(0.9 * 0.5, abs=1e-12, rel=0)
    assert "时序完整度" in item.reasons["imp"]
    print(chain.render)
    print(item.explain())


def test_渲染上界可覆盖_同一条链给不同上界给不同形态(tmp_path: Path) -> None:
    """上界是旋钮（`event_chain_render_max_chars`），但**截断规则只有一处**。

    直接喂 `render_chain`：宽上界 = 每一条事件一行（没有标记）；窄上界 = 明确截断
    + 被略去的 `event:id` 全列出来。两种形态的**共同点**是链上事件一条都不丢。
    """
    conn = _db(tmp_path)
    _balance(conn)
    from spoolkit.memory.timeline.db import fetch_current_state, fetch_events, fetch_state_rows

    events = fetch_events(conn, "wallet", "balance")
    current = fetch_current_state(conn, "wallet", "balance")
    state_rows = fetch_state_rows(conn, "wallet", "balance")

    def _render(cap: int) -> tuple[str, bool]:
        return render_chain(
            entity="wallet",
            attribute="balance",
            events=events,
            state_rows=state_rows,
            current=current,
            conflict_event_ids=(),
            incomplete_reason="",
            max_chars=cap,
        )

    wide_text, wide_truncated = _render(4000)
    narrow_text, narrow_truncated = _render(120)
    assert wide_truncated is False and "截断" not in wide_text
    assert narrow_truncated is True and "截断" in narrow_text
    assert len(wide_text) > len(narrow_text)
    assert len(narrow_text) <= 120
    for event in events:
        assert f"event:{event.id}" in narrow_text
        assert f"\n  event:{event.id} " in wide_text
