"""T4/T5：M2' 夹具固化 + 各臂仪器（`scripts/memory_relevance.py`）。

**这个文件存在的理由**：设计稿 §14.3.6 说夹具与仪器"在仓里"，实测**不在**——
`.tmp/embed-bench/bench_common.py:15-61` 才是源，而 `.tmp/` 被 `.gitignore:22`
忽略，随时会没。T4 把语料/查询逐字固化进 `benchmarks/memory/*.jsonl`，并在这里
钉四件事：

1. **逐字相同**：仓内夹具（16 条语料 / 13 条查询）与源
   `.tmp/embed-bench/bench_common.py` 的 `CORPUS` / `QUERIES` **逐字段相同**。
   源不在仓里时**skip 并说明**（不许假绿，也不许把"源没了"当成"抄对了"）。
2. **仪器可信**：`--arm store-char`（= 逐字召回 + 写入序，T4 的基线口径）读出的
   P@5 / R@5 / top1 与 `benchmarks/memory/baseline_fts5_store.json`（逐字抄自
   `.tmp/embed-bench/results_fts5_store.json` 的 summary）**逐位相同**。
3. **仪器可证伪**：把口径改回逐字，`--arm bigram` 的读数会与 `--arm store-char`
   **完全不同**；把重排信号改成常数，`char+bigram-rerank` 会退化回基线的三项读数
   （结论行报"未跑出差异"）。
4. **T5 的验收有读数**：`--arm char+bm25`（逐字召回 + `bm25()` 排序 = P1 定案
   口径）的 P@5 / R@5 不低于基线，**且 top1 不下降**（实测 `0.7692307692307693`
   → `1.0`）。

**读数多少由 `scripts/memory_relevance.py` 打印，本文件只钉"测的是哪几件事、
锚在哪"**。⚠️ 16 条语料太小（分组查询 P@5 上限 0.8），**绝对值不可外推**
（§14.3.7 第 3 条）；而且这 13 条查询**同时**是调参对象与判据对象——所以 T5 的
定案只看"不低于基线 ＋ top1 不降"，**没有**按读数去挑权重。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = REPO_ROOT / "scripts" / "memory_relevance.py"
FIXTURE_DIR = REPO_ROOT / "benchmarks" / "memory"
CORPUS_PATH = FIXTURE_DIR / "corpus.jsonl"
QUERIES_PATH = FIXTURE_DIR / "queries.jsonl"
BASELINE_PATH = FIXTURE_DIR / "baseline_fts5_store.json"

# 基线读数（**逐字**抄自 `.tmp/embed-bench/results_fts5_store.json` 的 summary）。
# 写死在这里是刻意的：就算 `.tmp/` 没了，这条"复现基线"的判据仍然可跑。
BASELINE_SUMMARY = {
    "n_queries": 13,
    "precision_at_5": 0.3384615384615385,
    "recall_at_5": 0.5961538461538461,
    "top1_accuracy": 0.7692307692307693,
}

# 源夹具（不在仓里，随时会没）。只在测试里读，不让仓内代码依赖 `.tmp/`。
SOURCE_BENCH = REPO_ROOT / ".tmp" / "embed-bench" / "bench_common.py"


def _load_harness():
    """按路径加载 `scripts/memory_relevance.py`（不起子进程、不装包）。"""
    spec = importlib.util.spec_from_file_location("memory_relevance", HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["memory_relevance"] = module
    spec.loader.exec_module(module)
    return module


def _load_source_bench():
    """读 `.tmp/embed-bench/bench_common.py` 的 `CORPUS` / `QUERIES`；不在返回 None。"""
    if not SOURCE_BENCH.exists():
        return None
    spec = importlib.util.spec_from_file_location("bench_common_source", SOURCE_BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- 1. 夹具固化（条数 + 逐字相同）-----------------------------------------


def test_仓内夹具的条数与分组() -> None:
    """16 条语料（G1–G4 各 4 条）+ 13 条查询，编号 1..16 / 顺序照源。"""
    rows = [
        json.loads(line)
        for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["id"] for row in rows] == list(range(1, 17))
    assert [row["group"] for row in rows] == ["G1"] * 4 + ["G2"] * 4 + ["G3"] * 4 + ["G4"] * 4

    queries = [
        json.loads(line)
        for line in QUERIES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(queries) == 13
    # 13 条里有 10 条"核心"查询 + 3 条命名对照查询（源文件的注释就这么分）。
    assert sum(1 for q in queries if q["note"].startswith("14.1.3 core")) == 10
    assert [q["query"] for q in queries][-3:] == [
        "清理旧文件",
        "窗口快满了怎么办",
        "包管理",
    ]


def test_仓内夹具与源逐字相同() -> None:
    """**这是"夹具抄对了"的证据**：逐字段比对仓内 jsonl 与
    `.tmp/embed-bench/bench_common.py` 的 `CORPUS` / `QUERIES`。

    源不在仓里时 **skip 并说明**：那时只能说"源已不在，无法再比对"，**不许**
    把 skip 写成"逐字相同"（那正是"假绿"）。
    """
    source = _load_source_bench()
    if source is None:
        pytest.skip(
            f"源夹具已不在：{SOURCE_BENCH}（`.tmp/` 被 .gitignore 忽略，本来就会没）。"
            "仓内夹具是唯一副本，本条无法再比对逐字相同——不是通过，是测不了。"
        )

    rows = [
        json.loads(line)
        for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == len(source.CORPUS)
    for row, (group, text) in zip(rows, source.CORPUS, strict=True):
        assert row["group"] == group
        assert row["text"] == text

    queries = [
        json.loads(line)
        for line in QUERIES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(queries) == len(source.QUERIES)
    for row, (query, kind, relevant, note) in zip(queries, source.QUERIES, strict=True):
        assert row["query"] == query
        assert row["kind"] == kind
        assert row["relevant"] == sorted(relevant)
        assert row["note"] == note

    # 逐字相同还包含"分组派生关系"：`GROUP_OF_ID` 与仓内 `group` 列一致。
    assert source.GROUP_OF_ID == {row["id"]: row["group"] for row in rows}
    assert source.RELEVANT == {row["query"]: set(row["relevant"]) for row in queries}


def test_基线锚文件的读数与逐字誊抄一致() -> None:
    """回归锚进仓之后，锚里的数字必须与"逐字抄自源"的那组常数一致。"""
    anchor = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert anchor["summary"] == BASELINE_SUMMARY
    assert anchor["source"] == ".tmp/embed-bench/results_fts5_store.json"


# ---- 2. 指标口径（与 `.tmp/embed-bench` 那套同式）--------------------------


def test_指标口径与bench_common同式() -> None:
    """`metrics` / `summarize` 是**独立重写**的一份：用手算的例子钉住公式。"""
    harness = _load_harness()
    # 相关 4 条、前 5 里命中 2 条、首位不相关。
    m = harness.metrics([9, 1, 2, 3, 5], {1, 2, 3, 4})
    assert m["p_at_k"] == 3 / 5
    assert m["r_at_k"] == 3 / 4
    assert m["top1"] == 0
    assert m["top_k_ids"] == [9, 1, 2, 3, 5]
    # 空结果：三项都是 0（不是异常）。
    empty = harness.metrics([], {1, 2})
    assert (empty["p_at_k"], empty["r_at_k"], empty["top1"]) == (0.0, 0.0, 0)
    assert empty["top1_id"] is None

    summary = harness.summarize({"a": m, "b": empty})
    assert summary["n_queries"] == 2
    assert summary["precision_at_5"] == (3 / 5 + 0.0) / 2
    assert summary["recall_at_5"] == (3 / 4 + 0.0) / 2
    assert summary["top1_accuracy"] == 0.0


# ---- 3. 仪器：store-char 复现基线（逐位相同）-------------------------------


def test_store_char臂复现基线读数逐位相同() -> None:
    """**T4 验收 4**：夹具抄对了的证明——`store-char` 读数与基线逐位相同。

    基线来自 `.tmp/embed-bench/results_fts5_store.json` 的 summary
    （`0.3384615384615385 / 0.5961538461538461 / 0.7692307692307693`）。
    """
    harness = _load_harness()
    result = harness.run_arm("store-char")
    summary = result["summary"]
    assert summary["n_queries"] == 13
    assert summary == BASELINE_SUMMARY
    # 逐位相同：既要 `==`，也要同类型（浮点字面量逐位相等）。
    assert repr(summary["precision_at_5"]) == repr(BASELINE_SUMMARY["precision_at_5"])
    assert repr(summary["recall_at_5"]) == repr(BASELINE_SUMMARY["recall_at_5"])
    assert repr(summary["top1_accuracy"]) == repr(BASELINE_SUMMARY["top1_accuracy"])


def test_bigram臂跑得出读数且与store_char可比() -> None:
    """**T4 验收 5/6**：`bigram` 臂给出读数、且与 `store-char` **不同**。

    "不同"是这条用例的红线：把召回口径也换成双字（两边同口径），两臂读数会完全
    一样，这里立刻红——判据因此是**可证伪**的，不是"看起来有读数就行"。
    T5 的手工变异 3（`store.normalize_for_fts` 改成双字）踩的就是这一条。
    """
    harness = _load_harness()
    char = harness.run_arm("store-char")["summary"]
    bi = harness.run_arm("bigram")["summary"]
    assert bi["n_queries"] == char["n_queries"] == 13
    assert (bi["precision_at_5"], bi["recall_at_5"], bi["top1_accuracy"]) != (
        char["precision_at_5"],
        char["recall_at_5"],
        char["top1_accuracy"],
    )


def test_判据行_两臂相同时报未跑出差异() -> None:
    """结论行的可证伪形态：**三项**读数与基线逐位相同时必须报"未跑出差异"。

    为什么是三项而不是两项：T5 的 `char+bm25` 恰好 **P@5/R@5 与基线相同、top1
    更高**——只看 P@5/R@5 会把定案臂误报成"未跑出差异"。下面的 `char_bm25`
    那一条就是防这个的。
    """
    harness = _load_harness()
    assert "未跑出差异" in harness.acceptance_line(dict(BASELINE_SUMMARY), BASELINE_SUMMARY)

    better = dict(BASELINE_SUMMARY, precision_at_5=0.5, recall_at_5=0.7)
    line = harness.acceptance_line(better, BASELINE_SUMMARY)
    assert "P@5 ≥ 基线" in line and "R@5 ≥ 基线" in line and "满足验收" in line
    worse = dict(BASELINE_SUMMARY, precision_at_5=0.1, recall_at_5=0.1)
    assert "P@5 < 基线" in harness.acceptance_line(worse, BASELINE_SUMMARY)
    # P@5/R@5 都持平、只有 top1 高：既不是"未跑出差异"，也不是"不满足验收"。
    top1_only = dict(BASELINE_SUMMARY, top1_accuracy=1.0)
    line = harness.acceptance_line(top1_only, BASELINE_SUMMARY)
    assert "未跑出差异" not in line and "满足验收" in line


# ---- 4. T5：`char+bm25` 是定案口径，验收在这里有读数 -----------------------


def test_char_bm25臂满足验收() -> None:
    """**T5 的验收（§14.8 口径）**：P@5 与 R@5 都不低于基线，且 top1 不下降。

    实测：`P@5=0.3384615384615385`（= 基线）、`R@5=0.5961538461538461`（= 基线）、
    `top1=1.0`（基线 `0.7692307692307693`）——三条查询的 top1 从 0 翻到 1：
    `依赖管理` `[3,5,6]`→`[5,6,3]`、`包管理` `[3,5,6]`→`[5,6,3]`、
    `全文检索` `[9,11,13,14,15]`→`[13,14,11,9,15]`；`存档` 从 `[1,2]`→`[2,1]`
    （两条都相关，top1 不变）。P@5/R@5 逐位持平是因为**截断集合没变**：逐字候选
    集就那么大，重排没有把相关的条目挤出前 5。
    """
    harness = _load_harness()
    s = harness.run_arm("char+bm25")["summary"]
    assert s["precision_at_5"] >= BASELINE_SUMMARY["precision_at_5"]
    assert s["recall_at_5"] >= BASELINE_SUMMARY["recall_at_5"]
    assert s["top1_accuracy"] > BASELINE_SUMMARY["top1_accuracy"]  # 0.7692… → 1.0
    # 夹具上的实测形状（不是判据，是"只动排序"的证据）：两项召回读数逐位持平。
    assert s["precision_at_5"] == BASELINE_SUMMARY["precision_at_5"]
    assert s["recall_at_5"] == BASELINE_SUMMARY["recall_at_5"]
    assert "满足验收" in harness.acceptance_line(s, BASELINE_SUMMARY)

    # 改善的**具体条目**（不是汇总数字）：三条查询的 top1 从 0 翻到 1，逐条读数
    # 与写入序对照着钉住——这三条就是"绝对值不可外推、但相对改善可复现"的证据。
    per_query = harness.run_arm("char+bm25")["per_query"]
    baseline_per_query = harness.run_arm("store-char")["per_query"]
    assert per_query["依赖管理"]["ranked"] == [5, 6, 3]
    assert per_query["包管理"]["ranked"] == [5, 6, 3]
    assert per_query["全文检索"]["ranked"] == [13, 14, 11, 9, 15]
    for query in ("依赖管理", "包管理", "全文检索"):
        assert baseline_per_query[query]["top1"] == 0
        assert per_query[query]["top1"] == 1


def test_重排臂与char_bm25三项读数逐位相同() -> None:
    """双字信号在本夹具上**没有**改变任何读数——这是"值不值得"的证据。

    `char+bigram-rerank`（覆盖率重排）与 `char+bm25+bigram`（bm25 主序 + 覆盖率
    次序）的三项读数与 `char+bm25` 逐位相同：16 条语料上双字作为**打分信号**既
    没有多召回一条、也没有把 top1 顶掉一条。所以生产排序**不采纳**它（把它留在
    仪器里，等 T6 的五维重排决定它的权重与位置）。
    """
    harness = _load_harness()
    base = harness.run_arm("char+bm25")["summary"]
    for arm in ("char+bigram-rerank", "char+bm25+bigram"):
        assert harness.run_arm(arm)["summary"] == base, arm


def test_重排信号改成常数_退化回基线并报未跑出差异(monkeypatch) -> None:
    """**可证伪**：把双字信号换成常数，重排臂退化回写入序 = 基线的三项读数。

    这条是 T5 变异的常驻版本（手工变异：把 `store.bigram_coverage` 改成
    `return 0.0`）。它同时说明上面那条"逐位相同"不是巧合——信号一动，读数就动。
    """
    harness = _load_harness()
    monkeypatch.setattr(harness, "bigram_coverage", lambda query, text: 0.0)
    s = harness.run_arm("char+bigram-rerank")["summary"]
    assert s == BASELINE_SUMMARY  # 并列时按 id = T4 的写入序口径
    assert "未跑出差异" in harness.acceptance_line(s, BASELINE_SUMMARY)


# ---- 5. 方案 B 的桩：P1 不引依赖 -------------------------------------------


@pytest.mark.parametrize("arm,keyword", [("tokenized", "依赖")])
def test_未启用的臂如实报skipped(arm: str, keyword: str) -> None:
    """`tokenized`（方案 B：引分词器）P1 不引依赖——不许假装有读数。"""
    harness = _load_harness()
    result = harness.run_arm(arm)
    assert keyword in result["skipped"]
    assert result["summary"]["n_queries"] == 0


def test_未知的臂直接报错() -> None:
    """臂名拼错时报错，而不是悄悄给一条空读数。"""
    harness = _load_harness()
    with pytest.raises(SystemExit):
        harness.run_arm("bigram+bm25")  # T5 起这个名字不再是一条臂（口径换回逐字了）
