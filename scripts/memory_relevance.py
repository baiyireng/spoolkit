"""M2' 三臂对照仪器：中文分词口径的 P@5 / R@5 / top1（设计稿 §14.8 / 计划 T4）。

**这份仪器存在的理由**是设计稿 §14.3.6 的原话「脚本与语料都在仓里，不在内存里」
——而实测它**不在仓里**：原仪器 `.tmp/embed-bench/run_fts5.py` 与夹具
`.tmp/embed-bench/bench_common.py` 都在 `.gitignore:22` 忽略的 `.tmp/` 下，
随时会没。T4 把语料与查询**逐字固化**进 `benchmarks/memory/*.jsonl`，这份脚本
就是围着那两份夹具的仪器。

三臂（**本任务只出前两臂**）：

| 臂 | 索引与查询口径 | 排序 | 本任务的状态 |
|---|---|---|---|
| `store-char` | 逐字拆（`store.normalize_for_fts` + `store._match_query`）= **现状基线** | `ORDER BY id`（= `_rank` 在同 use_count / 同 created_at 下的退化形态） | 复现基线读数 |
| `bigram` | 双字 shingle（`store.bigram_tokens`，T4 落地） | `ORDER BY id`（**只改分词、不动排序**） | 出读数 |
| `bigram+bm25` | 双字 shingle | `ORDER BY bm25(semantic_fts)` | **T5 才有读数**，本臂报"未启用" |
| `tokenized` | 方案 B（引分词器） | — | P1 不引依赖，**桩**：打印 skipped |

三条纪律：

1. **指标口径与 `.tmp/embed-bench` 那套逐字一致**（`bench_common.metrics` /
   `summarize`）：`p@5 = 命中数/5`、`r@5 = 命中数/相关数`、`top1 = 1 if 首位命中`，
   最后对 13 条查询取算术平均。本文件把这套指标**独立重写一遍**（不 import
   `.tmp`，那是随时会没的目录），并用 `tests/memory/test_relevance_harness.py`
   把"逐位相同"钉在基线上。
2. **`store-char` 与 `bigram` 走同一份夹具、同一套代码**，唯一差别是分词口径
   ——所以两份读数的差就是分词口径的差（§14.8 第 2 条"颠倒就说不清这点增益
   本来该由谁拿到"）。
3. **排序在本任务里一个字节都不许动**：两个臂都 `ORDER BY id`。`bm25()` 是 T5。

⚠️ **绝对数字不可外推**（§14.3.7 第 3 条 / 计划 T4 要点）：16 条语料太小，
分组查询的 P@5 上限只有 0.8。本仪器只承诺"**相对**排序不低于基线"，
**不许**把这里的读数写成"分词口径已达标"。

用法（**零新依赖**，标准库 + 现有依赖）：

    .venv\\Scripts\\python.exe scripts\\memory_relevance.py --arm store-char
    .venv\\Scripts\\python.exe scripts\\memory_relevance.py --arm bigram
    .venv\\Scripts\\python.exe scripts\\memory_relevance.py --arm all
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# 允许 `python scripts/memory_relevance.py` 直接跑（不起包安装）：把仓根与 src 挂上。
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _path in (str(_REPO / "src"), str(_REPO / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from spoolkit.memory.semantic import insert_semantic, search_semantic  # noqa: E402
from spoolkit.memory.store import (  # noqa: E402
    _match_query,
    init_memory_schema,
    normalize_for_fts,
)

FIXTURE_DIR = _REPO / "benchmarks" / "memory"
CORPUS_PATH = FIXTURE_DIR / "corpus.jsonl"
QUERIES_PATH = FIXTURE_DIR / "queries.jsonl"
BASELINE_PATH = FIXTURE_DIR / "baseline_fts5_store.json"

# 指标口径的常数（与 bench_common.K 一致）：P@5 / R@5 / top1。
K = 5

# 一次检索最多取几条。`.tmp/embed-bench/run_fts5.py` 取**全部**命中行，再由
# `metrics()` 截前 5 条打分；本仪器走 `search_semantic` 的 `limit`（默认
# `recall_limit=5`），两种口径出同一份 `top_k_ids`——差别只在"取回了几行"，
# 不在"算进指标的哪几条"。
HARNESS_LIMIT = K

ARMS = ("store-char", "bigram", "bigram+bm25", "tokenized")


# ---- 夹具（逐字固化进仓，见 tests/memory/test_relevance_harness.py）--------


def load_corpus(path: Path | None = None) -> list[dict]:
    """读 16 条语料：`{"id", "group", "text"}`，按 id 升序。"""
    target = path or CORPUS_PATH
    rows = [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return sorted(rows, key=lambda row: row["id"])


def load_queries(path: Path | None = None) -> list[dict]:
    """读 13 条查询：`{"query", "kind", "relevant", "note"}`，保持文件顺序。"""
    target = path or QUERIES_PATH
    return [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---- 指标（口径照 bench_common.metrics / summarize；独立重写一遍）----------


def metrics(ranked_ids: list[int], relevant: set[int], k: int = K) -> dict:
    """单条查询的 precision@k / recall@k / top1（与 `bench_common.metrics` 同式）。"""
    top = ranked_ids[:k]
    hits = [i for i in top if i in relevant]
    return {
        "p_at_k": len(hits) / k,
        "r_at_k": len(hits) / len(relevant) if relevant else 0.0,
        "top1": 1 if (ranked_ids and ranked_ids[0] in relevant) else 0,
        "top_k_ids": top,
        "top1_id": ranked_ids[0] if ranked_ids else None,
    }


def summarize(per_query: dict[str, dict]) -> dict:
    """对全部查询取算术平均（与 `bench_common.summarize` 同式）。"""
    n = len(per_query)
    if not n:
        return {"n_queries": 0, "precision_at_5": 0.0, "recall_at_5": 0.0, "top1_accuracy": 0.0}
    return {
        "n_queries": n,
        "precision_at_5": sum(v["p_at_k"] for v in per_query.values()) / n,
        "recall_at_5": sum(v["r_at_k"] for v in per_query.values()) / n,
        "top1_accuracy": sum(v["top1"] for v in per_query.values()) / n,
    }


def _score(ranked: dict[str, list[int]], queries: list[dict]) -> dict[str, dict]:
    """把每条查询的排好序 id 列表打成 `{query: metrics}`。"""
    per_query: dict[str, dict] = {}
    for item in queries:
        query = item["query"]
        m = metrics(ranked[query], set(item["relevant"]))
        m["note"] = item["note"]
        m["kind"] = item["kind"]
        m["ranked"] = ranked[query]
        per_query[query] = m
    return per_query


# ---- 两条臂 ----------------------------------------------------------------


def _char_index_value(text: str) -> str:
    """逐字口径的索引文本（= `store.normalize_for_fts` 的现成口径）。"""
    return normalize_for_fts(text)


def _char_match_value(query: str) -> str:
    """逐字口径的 MATCH 串（= `store._match_query` 的现成口径）。"""
    return _match_query(query)


class _CharTokenizer:
    """把 `semantic` 的检索口径临时钉在**逐字**上（`store-char` 臂的开关）。

    `spoolkit.memory.semantic` 把 `store` 的两个口径函数按名字 import 进了本模块
    的命名空间，所以换掉这两个名字就等于换掉整条检索路径的口径——主表写入、
    FTS 写入、MATCH 串生成**全部**走同一份代码。`store-char` 臂因此**不是**一份
    平行实现，而是同一件事的两种口径；这一点很重要：两臂读数的差就**只**能归因
    到分词口径（§14.8 第 2 条）。
    """

    def __init__(self, module) -> None:
        self._module = module

    def __enter__(self):
        self._saved = (
            self._module.normalize_for_fts_bigram,
            self._module.semantic_match_query,
        )
        self._module.normalize_for_fts_bigram = _char_index_value
        self._module.semantic_match_query = _char_match_value
        return self

    def __exit__(self, *exc_info) -> None:
        (
            self._module.normalize_for_fts_bigram,
            self._module.semantic_match_query,
        ) = self._saved
        return None


def run_store_char(corpus: list[dict], queries: list[dict]) -> dict[str, list[int]]:
    """`store-char` 臂：**逐字**索引 + **逐字 OR** 查询（现状基线口径）。

    与 `run_bigram` 走**同一条生产代码路径**（`init_memory_schema` +
    `insert_semantic` + `search_semantic`），唯一差别是那两个口径函数被
    `_CharTokenizer` 换成逐字版。排序两边都是 `ORDER BY s.id`——与基线的
    `_rank` 在"同 use_count / 同 created_at"下退化成同一个顺序（T4 不动排序，
    `bm25()` 是 T5）。
    """
    import spoolkit.memory.semantic as semantic_module

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_memory_schema(conn)
    with _CharTokenizer(semantic_module):
        for row in corpus:
            insert_semantic(conn, "fact", row["text"], content_hash=f"bench-{row['id']}")
        ranked: dict[str, list[int]] = {}
        for item in queries:
            hits = search_semantic(conn, item["query"], limit=HARNESS_LIMIT)
            ranked[item["query"]] = [row.id for row in hits]
    conn.close()
    return ranked


def run_bigram(corpus: list[dict], queries: list[dict]) -> dict[str, list[int]]:
    """`bigram` 臂：双字 shingle（**走仓里的生产代码路径**）。

    写入走 `semantic.insert_semantic`（它调 `_semantic_fts_values` → 双字）、
    查询走 `semantic.search_semantic`（它调 `store.semantic_match_query` → 双字，
    长度 < 2 的查询转原文子串回退），建表走 `store.init_memory_schema`。
    仪器因此**不是**在测一份平行实现，而是在测仓库里真正会跑的那条路。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_memory_schema(conn)
    for row in corpus:
        insert_semantic(
            conn,
            "fact",
            row["text"],
            content_hash=f"bench-{row['id']}",
            overrides={"semantic_hash_chars": 16},
        )
    ranked: dict[str, list[int]] = {}
    for item in queries:
        hits = search_semantic(conn, item["query"], limit=HARNESS_LIMIT)
        ranked[item["query"]] = [row.id for row in hits]
    conn.close()
    return ranked


# ---- 结论行（可证伪：两臂读数相同 → 报"未跑出差异"）-----------------------


def acceptance_line(summary: dict, baseline: dict | None) -> str:
    """一行结论：P@5 / R@5 是否都不低于基线（§14.8 第①步的接受判据）。

    基线与本臂读数**逐位相同**时明确报「未跑出差异」——T4 验收 6 要的正是这条
    （把双字改回逐字，两臂读数会一模一样，判据必须能看出来）。
    """
    if baseline is None:
        return "结论：无基线可比（benchmarks/memory/baseline_fts5_store.json 缺失）"
    base_p = baseline["precision_at_5"]
    base_r = baseline["recall_at_5"]
    same = summary["precision_at_5"] == base_p and summary["recall_at_5"] == base_r
    if same:
        return (
            "结论：**未跑出差异**——P@5/R@5 与 store-char 逐位相同，"
            "分词口径没有对读数产生任何影响（这应当判红）"
        )
    return (
        f"结论：P@5 {'≥' if summary['precision_at_5'] >= base_p else '<'} 基线 "
        f"({summary['precision_at_5']!r} vs {base_p!r})；"
        f"R@5 {'≥' if summary['recall_at_5'] >= base_r else '<'} 基线 "
        f"({summary['recall_at_5']!r} vs {base_r!r})"
    )


def load_baseline(path: Path | None = None) -> dict | None:
    """读回归锚；文件不在就返回 `None`（仪器如实说"无可比"。不编读数）。"""
    target = path or BASELINE_PATH
    if not target.exists():
        return None
    data = json.loads(target.read_text(encoding="utf-8"))
    return data.get("summary", data)


# ---- 入口 ------------------------------------------------------------------


def run_arm(arm: str, *, corpus_path: Path | None = None, queries_path: Path | None = None) -> dict:
    """跑一条臂，返回 `{"arm", "per_query", "summary", "skipped"}`。

    这是给 pytest 直接调的函数入口（`tests/memory/test_relevance_harness.py`
    用它复现基线，不必起子进程）。
    """
    corpus = load_corpus(corpus_path)
    queries = load_queries(queries_path)
    if arm == "store-char":
        ranked = run_store_char(corpus, queries)
    elif arm == "bigram":
        ranked = run_bigram(corpus, queries)
    elif arm == "bigram+bm25":
        # T5 才启用：`bm25()` 排序是 T5 的唯一改动面，本任务不许碰 ORDER BY。
        return {
            "arm": arm,
            "per_query": {},
            "summary": summarize({}),
            "skipped": "skipped: T5 才有读数（bm25 排序属 T5，本任务不许改 ORDER BY）",
        }
    elif arm == "tokenized":
        # 方案 B：另建"已分词检索列"、引分词器。P1 不引依赖（§13.13 第 1 条）。
        return {
            "arm": arm,
            "per_query": {},
            "summary": summarize({}),
            "skipped": "skipped: P1 不引依赖（§13.13 第 1 条）",
        }
    else:
        raise SystemExit(f"未知的臂 {arm!r}：可选 {', '.join(ARMS)}")
    per_query = _score(ranked, queries)
    return {"arm": arm, "per_query": per_query, "summary": summarize(per_query), "skipped": ""}


def _print_result(result: dict, baseline: dict | None) -> None:
    arm = result["arm"]
    print(f"== arm: {arm} ==")
    if result["skipped"]:
        print(f"  {result['skipped']}")
        return
    for query, m in result["per_query"].items():
        print(
            "  %-10s p@5=%.3f r@5=%.3f top1=%d  ranked=%s"
            % (query, m["p_at_k"], m["r_at_k"], m["top1"], m["ranked"])
        )
    s = result["summary"]
    print(
        "  SUMMARY  P@5=%r  R@5=%r  top1=%r  (n=%d)"
        % (s["precision_at_5"], s["recall_at_5"], s["top1_accuracy"], s["n_queries"])
    )
    if baseline is not None:
        print(
            "  BASELINE P@5=%r  R@5=%r  top1=%r"
            % (
                baseline["precision_at_5"],
                baseline["recall_at_5"],
                baseline["top1_accuracy"],
            )
        )
    if arm == "bigram":
        print("  " + acceptance_line(s, baseline))
    print("  注：16 条语料上的相对读数，绝对数字不可外推（§14.3.7 第 3 条）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="M2' 三臂对照仪器（P@5 / R@5 / top1；夹具在 benchmarks/memory/）"
    )
    parser.add_argument(
        "--arm",
        default="all",
        choices=("store-char", "bigram", "bigram+bm25", "tokenized", "all"),
        help="要跑的臂；默认 all（只跑有读数的那几条）",
    )
    parser.add_argument("--corpus", type=Path, default=None, help="覆盖语料 jsonl 路径")
    parser.add_argument("--queries", type=Path, default=None, help="覆盖查询 jsonl 路径")
    parser.add_argument("--json", action="store_true", help="额外打印机器可读结果")
    args = parser.parse_args(argv)

    wanted = ["store-char", "bigram"] if args.arm == "all" else [args.arm]
    if args.arm == "all":
        wanted += ["bigram+bm25", "tokenized"]
    baseline = load_baseline()
    if baseline is None:
        print("⚠️  benchmarks/memory/baseline_fts5_store.json 不在：无基线可比（不编读数）")
    payload = []
    for arm in wanted:
        result = run_arm(arm, corpus_path=args.corpus, queries_path=args.queries)
        _print_result(result, baseline)
        payload.append({k: v for k, v in result.items() if k != "per_query"} | {"per_query": result["per_query"]})
        print()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
