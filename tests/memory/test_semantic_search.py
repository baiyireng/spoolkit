"""T5：`semantic` 检索的 **逐字召回 + `bm25()` 排序**（设计稿 §14.8 第②步）。

T4 把召回口径换成双字 shingle，实测判它是**负结果**：`clean` 类的跨词边界查询
（`清理旧文件` → 逐字召回 5 条、双字只召回 1 条）P@5/R@5 都掉。T5 的裁决是
「**召回回到逐字**，双字只做重排/打分信号，**在逐字召回的候选集上做 `bm25()`
排序**」。本文件把 T5 的三件事钉住：

1. **`bm25()` 的符号与方向先实测、再改排序**（计划 T5 风险 ①：猜错会让整个排序
   反向，而用例可能看不出来）。SQLite 的 `bm25()` **原值越小（越负）越相关**，
   `ORDER BY bm25(semantic_fts)` 升序就是「最相关在前」；`bm25_score` 是它的
   正号版本（§6.1 的 `rel = 1 - exp(-bm25)` 配的是后者）。
2. **排序真的由 `bm25()` 决定**：写入序下 `依赖管理` 的 top1 是 `清理历史数据
   并备份`（id 更小），`bm25()` 下是同含「管」「理」「依」「赖」的那一条。
3. **每行带 `bm25_raw`**（§6.3：`why` 要把原值与归一化分量两个都打），而
   `bm25_raw` **不是表里的列**（schema 一个字节没动）；走 LIKE 的回退路径没有
   FTS 值可打，如实为 `None`。

仪器层面的读数与验收（P@5 / R@5 / top1）在
`tests/memory/test_relevance_harness.py`；双字切分与打分信号在
`tests/memory/test_bigram.py`。
"""

from __future__ import annotations

from pathlib import Path

import spoolkit.memory.semantic as semantic_module
from spoolkit.memory.semantic import get_semantic, insert_semantic, search_semantic
from spoolkit.memory.store import (
    _match_query,
    bm25_score,
    init_memory_schema,
    normalize_for_fts,
    normalize_for_fts_bigram,
)
from spoolkit.store.db import open_db


def _conn(tmp_path: Path):
    """一个建好 v4 结构的空库（`open_db` 已把 `row_factory` 设成 `sqlite3.Row`）。"""
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


# ---- 1. `bm25()` 的符号与方向（先实测钉住，再拿它排序）----------------------


def _seed_dependency_corpus(conn):
    """T5 的最小语料：一条弱命中、一条强命中、三条填充。

    插入顺序是刻意的：**弱的在前**（id 更小），所以"强的排第一"只可能是排序的
    功劳，不是写入顺序的巧合。

    填充条目不是凑数：FTS5 的 IDF 是 `log((N-n+0.5)/(n+0.5))`，**只有两条**时
    `n=1` 的词 IDF 恰好是 0、`n=2` 的是负值，两种贡献互相抵消，读数退化成
    `1e-06` 量级的噪声差异。补到 5 条，`依`/`赖`/`管` 的 IDF 才为正，
    "命中更多 → 原值更小"才是**有量级**的结论（实测 `-3.22` vs `-0.33`）。
    """
    weak = insert_semantic(conn, "fact", "清理历史数据并备份", content_hash="t5-b")
    strong = insert_semantic(conn, "fact", "本项目统一用 uv 管理依赖", content_hash="t5-a")
    for index, filler in enumerate(
        ("安装体积要可控", "requirements 要锁版本", "小窗口下不要全量注入")
    ):
        insert_semantic(conn, "fact", filler, content_hash=f"t5-fill{index}")
    return strong, weak


def test_bm25符号与方向_原值越小越相关(tmp_path: Path) -> None:
    """**这是 T5 的硬前置**：同一个查询下，命中更多的行 `bm25()` 原值更小（更负）。

    语料见 `_seed_dependency_corpus`：`本项目统一用 uv 管理依赖` 是 `依` `赖`
    `管` `理` 四个逐字命中，`清理历史数据并备份` 只有 `理`。断言四件事：原值都是
    负数、命中多的原值更小、`ORDER BY bm25(...)` 升序把命中多的排前、
    `bm25_score` 把方向翻正。把符号搞反（`DESC` 或取 `-bm25`）→ 这条必红。
    """
    conn = _conn(tmp_path)
    strong, weak = _seed_dependency_corpus(conn)

    match = _match_query("依赖管理")
    assert match == "依 OR 赖 OR 管 OR 理"  # 逐字 OR（T5 的召回口径）
    rows = conn.execute(
        "SELECT s.id, bm25(semantic_fts) AS raw FROM semantic_fts f"
        " JOIN semantic s ON s.id = f.rowid WHERE semantic_fts MATCH ?"
        " ORDER BY bm25(semantic_fts)",
        (match,),
    ).fetchall()
    raws = {row["id"]: row["raw"] for row in rows}
    print("bm25 原值（越小越相关）:", raws)  # 实测依据：把原始数打出来

    assert set(raws) == {strong.id, weak.id}
    assert all(raw < 0 for raw in raws.values())  # SQLite 的 bm25() 给的是负数
    assert raws[strong.id] < raws[weak.id]  # 4 个命中 < 1 个命中
    assert [row["id"] for row in rows][0] == strong.id  # 升序 = 最相关在前

    # `bm25_score` 把方向翻正（§6.1 的 `rel = 1 - exp(-bm25)` 配的是它）
    assert bm25_score(raws[strong.id]) > bm25_score(raws[weak.id]) > 0
    # 生产检索与上面那条 SQL 同一个方向
    hits = search_semantic(conn, "依赖管理", limit=5)
    assert [item.id for item in hits] == [strong.id, weak.id]
    conn.close()


# ---- 2. 召回口径是逐字（T4 的双字替召回已按裁决回退）-----------------------


def test_召回口径是逐字_不是双字(tmp_path: Path) -> None:
    """`semantic_fts` 里落的是**逐字拆**；跨词边界的查询因此仍然抓得到。"""
    conn = _conn(tmp_path)
    item = insert_semantic(conn, "fact", "归档旧的任务记录", content_hash="t5-c")

    indexed = conn.execute(
        "SELECT text FROM semantic_fts WHERE rowid = ?", (item.id,)
    ).fetchone()["text"]
    assert indexed == normalize_for_fts("归档旧的任务记录")
    assert indexed != normalize_for_fts_bigram("归档旧的任务记录")  # 双字没在召回路径上

    assert _match_query("清理旧文件") == "清 OR 理 OR 旧 OR 文 OR 件"
    # `旧` 命中 `归档旧的任务记录`——双字口径下 `旧文` 这个 token 不存在，
    # 这条条目会被整个丢掉（T4 的负结果就是这个形状）。
    assert [row.id for row in search_semantic(conn, "清理旧文件", limit=5)] == [item.id]
    conn.close()


def test_排序由bm25决定_而不是写入顺序(tmp_path: Path) -> None:
    """可分辨的锚：写入序把 id 小的顶上来，`bm25()` 把更相关的顶上来。

    这两条在**同一次检索**里可分辨：`清理历史数据并备份` 写在前（只有 `理`），
    `本项目统一用 uv 管理依赖` 写在后（`依` `赖` `管` `理` 全中）。写入序会给
    前者，`bm25()` 必须给后者——把 `ORDER BY bm25(...)` 删掉（退回写入序）本条红。
    """
    conn = _conn(tmp_path)
    strong, weak = _seed_dependency_corpus(conn)
    assert weak.id < strong.id  # 写入序下弱的在前——所以"排对了"只可能是 bm25 的功劳

    hits = search_semantic(conn, "依赖管理", limit=5)
    assert [item.id for item in hits] == [strong.id, weak.id]

    # 每行都带原值，且两条相关度不同 → 两个不同的原值（§6.3 的原值出口）。
    assert all(item.bm25_raw is not None for item in hits)
    assert hits[0].bm25_raw != hits[1].bm25_raw
    assert hits[0].bm25_raw < hits[1].bm25_raw  # 原值越小越相关
    conn.close()


def test_给足候选时_排序决定留下哪几条(tmp_path: Path, monkeypatch) -> None:
    """截断在 SQL 里做，所以排序决定**谁进召回**，不只是换个顺序。

    8 条候选、`limit=5`：只有「归档策略」四个字全中的那条最长的那条会被写入序
    截掉，`bm25()` 把它留进候选。这条同时钉住 `ORDER BY` 与 `LIMIT` 的相对位置
    （先排后截）与"给足 `limit` 就等于在全部候选上重排"这句话。
    """
    conn = _conn(tmp_path)
    for index in range(6):
        insert_semantic(conn, "fact", f"归档条目第 {index} 条", content_hash=f"t5-f{index}")
    insert_semantic(conn, "fact", "策略条目甲", content_hash="t5-fs")
    target = insert_semantic(conn, "fact", "归档策略：迁移前先备份", content_hash="t5-ft")

    hits = search_semantic(conn, "归档策略", limit=5)
    assert len(hits) == 5
    assert target.id in [item.id for item in hits]  # bm25 把它留住了

    # 对照：同一个函数、同一批候选，只把排序换回写入序（T4 的基线口径）——
    # 那条被截掉了。"排序"因此不是一个装饰。
    monkeypatch.setattr(semantic_module, "_RECALL_ORDER_BY", "s.id")
    id_order = [item.id for item in search_semantic(conn, "归档策略", limit=5)]
    monkeypatch.undo()
    assert target.id not in id_order
    conn.close()


# ---- 3. 原值出口与 schema 不动 ---------------------------------------------


def test_bm25原值不是表里的列(tmp_path: Path) -> None:
    """`bm25_raw` 是检索时算出来的，不是新列——schema 与 FTS 定义一个字节没动。

    T5 的硬约束是"不许改 schema / `_MIGRATIONS` / FTS 表定义"，所以原值只能挂在
    返回的 `Semantic` 上；`get_semantic` / `list_semantic` 给 `None`。
    """
    conn = _conn(tmp_path)
    item = insert_semantic(conn, "fact", "归档旧的任务记录", content_hash="t5-g")

    got = get_semantic(conn, item.id)
    assert got is not None and got.bm25_raw is None
    for table in ("semantic", "semantic_fts"):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert "bm25_raw" not in columns
    # FTS 表还是那三列（T5 不改 FTS 定义）。
    assert {row["name"] for row in conn.execute("PRAGMA table_info(semantic_fts)")} == {
        "text",
        "trigger",
        "evidence",
    }
    conn.close()


def test_回退路径的行没有bm25原值(tmp_path: Path, monkeypatch) -> None:
    """走 LIKE 回退时没碰 FTS，**没有 bm25 值可打**——如实为 `None`，不编一个 0。

    把 `_match_query` 换成"切不出 token"的形态，强制走
    `_semantic_single_char_fallback`（T4 起就有的安全网，T5 不删）：结果照常
    查得到，但 `bm25_raw` 是 `None`。
    """
    conn = _conn(tmp_path)
    item = insert_semantic(conn, "fact", "归档旧的任务记录", content_hash="t5-h")
    monkeypatch.setattr(semantic_module, "_match_query", lambda query: "")

    hits = search_semantic(conn, "归档旧的任务记录", limit=5)
    assert [row.id for row in hits] == [item.id]
    assert hits[0].bm25_raw is None
    conn.close()


def test_空查询不检索(tmp_path: Path) -> None:
    """与 `search_memories` 同一口径：空查询返回空，且不去碰 FTS。"""
    conn = _conn(tmp_path)
    insert_semantic(conn, "fact", "归档旧的任务记录", content_hash="t5-i")
    assert search_semantic(conn, "   ") == []
    assert search_semantic(conn, "") == []
    conn.close()
