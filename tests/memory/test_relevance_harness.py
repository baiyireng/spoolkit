"""T4：M2' 夹具固化 + 三臂仪器（`scripts/memory_relevance.py`）。

**这个文件存在的理由**：设计稿 §14.3.6 说夹具与仪器"在仓里"，实测**不在**——
`.tmp/embed-bench/bench_common.py:15-61` 才是源，而 `.tmp/` 被 `.gitignore:22`
忽略，随时会没。T4 把语料/查询逐字固化进 `benchmarks/memory/*.jsonl`，并在这里
钉三件事：

1. **逐字相同**：仓内夹具（16 条语料 / 13 条查询）与源
   `.tmp/embed-bench/bench_common.py` 的 `CORPUS` / `QUERIES` **逐字段相同**。
   源不在仓里时**skip 并说明**（不许假绿，也不许把"源没了"当成"抄对了"）。
2. **仪器可信**：`--arm store-char`（= 逐字口径，现状基线）读出的 P@5 / R@5 /
   top1 与 `benchmarks/memory/baseline_fts5_store.json`（逐字抄自
   `.tmp/embed-bench/results_fts5_store.json` 的 summary）**逐位相同**。
3. **仪器可证伪**：把双字口径改回逐字，`--arm bigram` 的读数会与
   `--arm store-char` **完全相同**（结论行会报"未跑出差异"）。

判据本身（§14.8 第①步 / 计划 T4）：`bigram` 臂的 P@5 与 R@5 都不低于
`store-char` 臂。**读数多少由 `scripts/memory_relevance.py` 打印，本文件不替它
下结论**——只钉"测的是哪两件事、锚在哪"。⚠️ 16 条语料太小（分组查询 P@5 上限
0.8），**绝对值不可外推**（§14.3.7 第 3 条）。
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

    "不同"是这条用例的红线：把双字口径改回逐字，两臂读数会完全一样，
    这里立刻红——判据因此是**可证伪**的，不是"看起来有读数就行"。
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
    """结论行的可证伪形态：读数与基线逐位相同时必须报"未跑出差异"。"""
    harness = _load_harness()
    same = harness.acceptance_line(dict(BASELINE_SUMMARY), BASELINE_SUMMARY)
    assert "未跑出差异" in same

    better = dict(BASELINE_SUMMARY, precision_at_5=0.5, recall_at_5=0.7)
    line = harness.acceptance_line(better, BASELINE_SUMMARY)
    assert "P@5 ≥ 基线" in line and "R@5 ≥ 基线" in line
    worse = dict(BASELINE_SUMMARY, precision_at_5=0.1, recall_at_5=0.1)
    assert "P@5 < 基线" in harness.acceptance_line(worse, BASELINE_SUMMARY)


# ---- 4. 另外两臂：本任务不出读数，但桩必须在 -------------------------------


@pytest.mark.parametrize("arm,keyword", [("bigram+bm25", "T5"), ("tokenized", "依赖")])
def test_未启用的臂如实报skipped(arm: str, keyword: str) -> None:
    """`bigram+bm25` 是 T5；`tokenized`（方案 B）P1 不引依赖——都不许假装有读数。"""
    harness = _load_harness()
    result = harness.run_arm(arm)
    assert keyword in result["skipped"]
    assert result["summary"]["n_queries"] == 0
