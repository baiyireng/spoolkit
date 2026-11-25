"""T11 补缺轮：事件链在生产路径上**真的**参与相关性排序（设计稿 §6.7 / §6.1 / §6.3）。

**补的是什么缺口**（T11 交回来时如实报的那一条）：链单元的口径（一个整体单元、
`rel` 取链内最高、不求和）早就钉住了，但 `memory_event` 没有 FTS 索引——事件侧
取不到 `bm25()` 原值，于是**生产路径上链的 `rel` 恒为 0**，链只剩新近度与重要度
在排序。v5 给 `memory_event` 建了 `memory_event_fts`，`session._build_chain` 按
问句把每条事件的 `bm25()` 原值取回来填进 `chain_members[].bm25_raw`。

这里钉四件事，**全部走生产路径**（真插事件 → 真 `recall_result` → 真五维重排，
不是手工构造 `Candidate`）：

1. **事件可检索**：用 `raw_text` 里的词（经生产口径的逐字分词）能查到那条事件；
2. **链的 `rel` > 0 且数是真的**：链里填的 `bm25()` 原值与
   `store.event_bm25_raws(conn, query, event_ids)` 直接查出来的**逐位相同**
   （"不许编数"的可证伪形态：手改一个数就红），并且 `rel` 等于按链内最高原值
   走 §6.1 公式算出来的值；`Scored.explain()` 里能看到这些原值；
3. **链的反应随问句变**：换个问句 → 链内命中的事件不同 → 原值与 `rel` 都变
   （不是"随便给个正数就完事"）；
4. **两个旋钮真的能改结果**（T11 的另一条缺口）：
   `event_chain_incomplete_factor`（时序完整度系数，默认 0.5）与
   `event_chain_min_matched_tokens`（事件侧逐字召回门槛，默认 2）。

没命中问句的事件**没有原值**（缺席，不是 0）：用例连这件事一起钉——链上的
`event:id` 会以"没有 bm25 原值"出现在 reason 里，而不是被填一个 0 混过去。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from spoolkit import limits
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory import store
from spoolkit.memory.rank import CHAIN_INCOMPLETE_KNOB
from spoolkit.memory.session import MemorySession, select_chains
from spoolkit.memory.timeline.clock import FakeClock, epoch_at
from spoolkit.memory.timeline.db import open_timeline
from spoolkit.memory.timeline.reduce import append_event, draft_from_text
from spoolkit.memory.timeline.registry import add_alias, register_attribute

NOW = epoch_at(2026, 10, 3, 12, 0)
CLOCK = FakeClock(NOW, tz_offset_min=480)

# 生产口径的问句：疑问词 + 时间副词（§6.7 判据 ① + ③），事件文本里有对应的字样。
QUERY = "现在还剩多少"
QUERY_EARNED = "我赚了多少"
QUERY_OTHER = "今天花了多少"


def _session(conn, hot: Path, **overrides) -> MemorySession:
    return MemorySession(
        conn=conn,
        hot_path=hot,
        counter=OfflineTokenCounter(),
        context_window=8192,
        clock=CLOCK,
        overrides=dict(overrides),
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
    confidence: float,
) -> int:
    """走 T8 的时间解析 + T9 的归约 + v5 的索引同步，落一条事件。返回 event id。"""
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
            speaker="user",
            confidence=confidence,
            source_ref=f"session:s1#turn:{index}",
            round_key=f"s1#{entity}.{attribute}#{index}",
        ),
        clock=CLOCK,
    )
    return result.event_id


def _balance(conn) -> tuple[int, int, int]:
    """`wallet.balance`：3 条事件（T11 用例同形的余额链）。返回三个 event id。"""
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    add_alias(conn, "余额", "balance", kind="attribute", clock=CLOCK)
    return (
        _add(
            conn, entity="wallet", attribute="balance", op="set", value="100",
            raw_text="现在身上有 100 块", at_text="2026-10-02 09:12", index=1,
            confidence=0.9,
        ),
        _add(
            conn, entity="wallet", attribute="balance", op="delta", value="-20",
            raw_text="今天花了 20", at_text="2026-10-02 09:13", index=2, confidence=0.7,
        ),
        _add(
            conn, entity="wallet", attribute="balance", op="delta", value="50",
            raw_text="我赚了 50", at_text="2026-10-03 10:11", index=3, confidence=0.8,
        ),
    )


def _db(tmp_path: Path):
    return open_timeline(tmp_path / "memory.db")


# ---- 1. 事件可检索 -------------------------------------------------------------


def test_事件可检索_用raw_text里的词就能查到该事件(tmp_path: Path) -> None:
    """v5 的索引真的建起来了：`raw_text` 里的词按生产口径查得到那条事件。

    走 `store._match_query`（`memory_fts` / `semantic_fts` / 事件侧**同一份**
    逐字分词），不另写一套——两套口径才是真坑（直接拿中文原句喂 FTS5 是查不到的，
    因为索引侧已经把中文字逐字拆开了）。
    """
    conn = _db(tmp_path)
    first, second, third = _balance(conn)
    conn.commit()

    def match(query: str) -> tuple[int, ...]:
        return tuple(
            int(row[0])
            for row in conn.execute(
                "SELECT rowid FROM memory_event_fts WHERE memory_event_fts MATCH ?"
                " ORDER BY rowid",
                (store._match_query(query),),
            )
        )

    assert match("赚") == (third,)
    assert match("花") == (second,)
    assert match("身上") == (first,)
    assert match(QUERY) == (first,)  # 「现在」命中第一条事件的 raw_text
    # 索引侧是逐字拆过的（与 `memory_fts` 同一口径）。
    row = conn.execute(
        "SELECT raw_text FROM memory_event_fts WHERE rowid = ?", (first,)
    ).fetchone()
    assert row[0] == store.normalize_for_fts("现在身上有 100 块")
    # 与三条事件**没有一个字**重合的词查不到（OR 口径下"部分命中"才会回结果，
    # 所以这里选的词是逐字都不在事件文本里的）。
    assert match("悟空") == ()
    conn.close()


# ---- 2. 生产路径上链的 rel > 0，且数是 bm25() 给的真值 -------------------------


def test_生产路径_链的rel大于0且explanation里有事件侧bm25原值(tmp_path: Path) -> None:
    """真插事件 → 真检索 → 真重排：链单元的 `rel` **不再**是 0。

    "不许编数"的可证伪形态（两条独立读数必须对上）：

    - ① 链候选 `chain_members[].bm25_raw` == `store.event_bm25_raws(conn, query,
      event_ids)`（**直接从 `memory_event_fts` 查出来的**原值）；
    - ② 链的 `rel` == 按链内最高原值走 §6.1 公式算出来的那个数（`w_bm25` 从
      `limits` 取默认 1.0）。

    手改任何一个数（或在 `session` 里写一个常数原值）→ 本条红。
    """
    conn = _db(tmp_path)
    ids = _balance(conn)
    conn.commit()
    outcome = _session(conn, tmp_path / "memory.md").recall_result(QUERY)

    assert outcome.variable_quantity is True
    assert outcome.selection.refs == ("event_chain:wallet.balance",)
    item = outcome.ranked.chosen[0]
    assert item.candidate.is_chain and item.candidate.unit == "event_chain(3)"

    # ① 原值来自事件侧 FTS，逐位相同（链上填的就是 bm25() 给的）。
    raws = store.event_bm25_raws(conn, QUERY, ids)
    assert raws, "事件侧一条都没命中：v5 的索引没接上"
    assert all(raw < 0 for raw in raws.values()), raws  # 原值越小越相关（T5 钉的符号）
    filled = {m.event_id: m.bm25_raw for m in item.candidate.chain_members}
    assert {k: v for k, v in filled.items() if v is not None} == raws
    # 没命中的事件**没有原值**（缺席，不是 0）——链的 bm25_raw 就是命中最高的那条。
    assert filled[ids[0]] == raws[ids[0]]
    assert filled[ids[1]] is None and filled[ids[2]] is None
    assert item.candidate.bm25_raw == min(raws.values())

    # ② rel 走 §6.1 的公式，输入是"链内最高"那条的原值，**不是 Σ**。
    w_bm25 = limits.resolve("w_bm25")[0]
    expected = 1.0 - math.exp(-w_bm25 * (-min(raws.values())))
    assert item.parts["rel"] == pytest.approx(expected, abs=1e-12, rel=0)
    assert item.parts["rel"] > 0.0

    # 依据要说得出原值、说得出被取中的是哪条事件、说得出没命中的那两条。
    # （"Σ 与 max 必须区分"的更硬证据在 `tests/memory/test_event_chain.py` 的
    #   `test_长链不加分_rank层`：那里链内几条都有原值，Σ 与 max 一眼可分。）
    reason = item.reasons["rel"]
    for event_id, raw in raws.items():
        assert f"event:{event_id}={raw!r}" in reason
    assert "链内最高" in reason and "不求和" in reason and "长度不进公式" in reason
    assert "没有** bm25 原值" in reason or "没有 bm25 原值" in reason
    for missing in (ids[1], ids[2]):
        assert f"event:{missing}" in reason
    # explanation（`Scored.explain()`）里也看得到这些原值（§6.3：原值与归一化都打）。
    line = item.explain()
    assert "相关度依据" in line and f"event:{ids[0]}={raws[ids[0]]!r}" in line
    print("链候选的 bm25 原值:", filled)
    print("rel =", item.parts["rel"])
    print("explanation:", line)


def test_链的rel随问句变_命中哪条事件就取哪条的原值(tmp_path: Path) -> None:
    """换个问句 → 链内命中的事件不同 → 原值与 `rel` 都变（不是"永远同一个正数"）。

    三问分别只命中第 1 / 第 2 / 第 3 条事件（或第 2+3 条），所以三个读数两两不同。
    """
    conn = _db(tmp_path)
    ids = _balance(conn)
    conn.commit()
    session = _session(conn, tmp_path / "memory.md")

    seen: dict[str, float] = {}
    for query in (QUERY, QUERY_OTHER, QUERY_EARNED):
        outcome = session.recall_result(query)
        item = outcome.ranked.chosen[0]
        assert item.candidate.is_chain
        assert item.parts["rel"] > 0.0, query
        seen[query] = item.parts["rel"]
        raws = store.event_bm25_raws(conn, query, ids)
        assert item.candidate.bm25_raw == min(raws.values())
        print(query, "→ 原值", raws, "rel", item.parts["rel"])

    # 「现在还剩多少」与「我赚了多少」命中的是不同事件 → 读数不同。
    assert seen[QUERY] != seen[QUERY_EARNED]
    assert seen[QUERY_OTHER] != seen[QUERY_EARNED]
    # 三条事件的 raw_text 两两不同，所以三个 rel 不会撞在一起。
    assert len(set(seen.values())) == 3
    conn.close()


def test_选链判据仍然只看逐字召回_不因为有了FTS而变成打分(tmp_path: Path) -> None:
    """T11 的分工不许被这一轮改掉：**选链是判据、打分是五维**。

    `select_chains` 仍走"疑问词 +（键命中 或 时间副词）+ 逐字召回门槛"，
    与 `rel` 的计算完全无关；不是可变数量类问题就一定不造链（哪怕事件能命中）。
    """
    conn = _db(tmp_path)
    _balance(conn)
    conn.commit()

    assert select_chains(conn, QUERY).variable_quantity is True
    # 问句把事件词带回来了，但没有疑问词 → 不造链（判据没说"能命中就建链"）。
    assert select_chains(conn, "身上有 100 块").variable_quantity is False
    outcome = _session(conn, tmp_path / "memory.md").recall_result("身上有 100 块")
    assert outcome.chains == ()
    conn.close()


# ---- 3. 两个旋钮真的能改结果 ---------------------------------------------------


def test_旋钮_时序完整度系数可覆盖且改了结果就变(tmp_path: Path) -> None:
    """`event_chain_incomplete_factor`（默认 0.5）：链单元 `imp` 直接乘它。

    依据：设计稿 §6.7 表 / §15.6 只说"缺基准要扣分、不假装完整"，**没给数**——
    0.5 是 T11 的裁决，裁决也要能按工作区覆盖（本仓规矩：阈值不许写死）。
    现场用同键同刻两条互斥 `set`：归约器判 `conflict` → 链的时序完整度不足。
    """
    conn = _db(tmp_path)
    register_attribute(conn, "wallet", "balance", type="monetary", clock=CLOCK)
    add_alias(conn, "余额", "balance", kind="attribute", clock=CLOCK)
    _add(conn, entity="wallet", attribute="balance", op="set", value="100",
         raw_text="现在还剩 100", at_text="2026-10-02 09:12", index=1, confidence=0.9)
    _add(conn, entity="wallet", attribute="balance", op="set", value="80",
         raw_text="现在还剩 80", at_text="2026-10-02 09:12", index=2, confidence=0.9)
    conn.commit()

    assert limits.knob(CHAIN_INCOMPLETE_KNOB).default == 0.5
    assert limits.knob(CHAIN_INCOMPLETE_KNOB).overridable is True

    default_item = _session(conn, tmp_path / "memory.md").recall_result(QUERY).ranked.chosen[0]
    assert default_item.candidate.complete is False
    assert default_item.parts["imp"] == pytest.approx(0.9 * 0.5, abs=1e-12, rel=0)
    assert "时序完整度 0.5" in default_item.reasons["imp"]
    assert f"旋钮 {CHAIN_INCOMPLETE_KNOB}" in default_item.reasons["imp"]

    # 改小 → 扣得更狠；改到 1.0 → 一点都不扣。**同一个库、同一个问句**。
    tight = _session(conn, tmp_path / "memory.md", **{CHAIN_INCOMPLETE_KNOB: 0.1})
    tight_item = tight.recall_result(QUERY).ranked.chosen[0]
    assert tight_item.parts["imp"] == pytest.approx(0.9 * 0.1, abs=1e-12, rel=0)
    assert tight_item.score < default_item.score

    loose = _session(conn, tmp_path / "memory.md", **{CHAIN_INCOMPLETE_KNOB: 1.0})
    loose_item = loose.recall_result(QUERY).ranked.chosen[0]
    assert loose_item.parts["imp"] == pytest.approx(0.9, abs=1e-12, rel=0)
    assert loose_item.score > default_item.score
    # 覆盖来源要说得出来（`limits.resolve` 的第二个返回值）。
    assert tight_item.reasons["imp"].endswith("来源 配置）")
    conn.close()


def test_旋钮_事件侧逐字召回门槛可覆盖且改了结果就变(tmp_path: Path) -> None:
    """`event_chain_min_matched_tokens`（默认 2）：门槛一抬，键就进不了候选。

    依据：设计稿 §6.7 判据② 只要求"问句的逐字召回把键的事件带回来"，**没给**
    "几个字才算带回来"——2 是 T11 的实现裁决（1 会把链灌满候选池）。
    现场：「现在还剩多少」对第一条事件命中 `现`+`在` 两个字，所以默认 2 建链；
    抬到 3 就一个字，判据不成立 → **链一条都不造**（结果随之改变）。
    """
    conn = _db(tmp_path)
    _balance(conn)
    conn.commit()

    assert limits.knob("event_chain_min_matched_tokens").default == 2

    default_outcome = _session(conn, tmp_path / "memory.md").recall_result(QUERY)
    assert default_outcome.selection.refs == ("event_chain:wallet.balance",)
    assert [c.unit for c in default_outcome.candidates] == ["event_chain(3)"]

    strict = _session(
        conn, tmp_path / "memory.md", event_chain_min_matched_tokens=3
    ).recall_result(QUERY)
    assert strict.selection.keys == ()
    assert strict.selection.variable_quantity is False
    assert strict.chains == () and strict.candidates == () and strict.text == ""
    assert "不造链单元" in strict.selection.note()

    # 门槛回到 1 也仍然建链（这一档不是"只有 2 才对"，而是旋钮在起作用）。
    loose = _session(
        conn, tmp_path / "memory.md", event_chain_min_matched_tokens=1
    ).recall_result(QUERY)
    assert loose.selection.refs == ("event_chain:wallet.balance",)
    # `select_chains` 也吃同一份 overrides（判据与造链用同一个门槛，不会两套）。
    assert select_chains(
        conn, QUERY, overrides={"event_chain_min_matched_tokens": 3}
    ).keys == ()
    conn.close()


def test_两个旋钮都在登记表里且可覆盖(tmp_path: Path) -> None:
    """登记表是"现在生效的是多少、从哪来"的唯一出处（两个数都不许写死在消费处）。"""
    for name, default in (
        (CHAIN_INCOMPLETE_KNOB, 0.5),
        ("event_chain_min_matched_tokens", 2),
    ):
        entry = limits.knob(name)
        assert entry is not None and entry.kind == limits.CAPABILITY
        assert entry.overridable is True and entry.default == default
        assert entry.note.strip()
        assert limits.resolve(name, {name: default + 1}) == (default + 1, "配置")
        assert limits.resolve(name) == (default, "默认")
    # 代码里也不许再出现这两个裸常数：消费处只认旋钮名。
    root = Path(__file__).resolve().parents[2]
    session_src = (root / "src/spoolkit/memory/session.py").read_text(encoding="utf-8")
    rank_src = (root / "src/spoolkit/memory/rank.py").read_text(encoding="utf-8")
    assert "MIN_MATCHED_TOKENS = 2" not in session_src
    assert "CHAIN_INCOMPLETE_FACTOR = 0.5" not in rank_src
    assert 'limits.resolve("event_chain_min_matched_tokens"' in session_src
    assert "limits.resolve(CHAIN_INCOMPLETE_KNOB" in rank_src
