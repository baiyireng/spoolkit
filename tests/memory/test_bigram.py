"""`semantic` 侧的双字 shingle 口径（设计稿 §14.8 方案 A / T4；T5 起只做打分信号）。

这一组用例钉住四件事：

1. **切分规则**：`归档旧的任务记录` → `归档 档旧 旧的 的任 任务 务记 记录`
   **逐字**（字面量断言，不是"大概像"）；`归档` → `归档`；字母数字段整段成 token。
2. **不许造假双字**：标点/空白是段边界，`归档，删除` 里**不生成** `档，` / `，删`
   这类跨边界 token（§14.8 取舍列点名的噪声源）。
3. **单字查询不回归**：查 `归` 仍能命中 `归档旧的任务记录`——它正是 §14.8 点名的
   「粒度」案例，归零就是回归（T5 起由**逐字召回**直接保证）。
4. **召回口径是逐字**（T5 的裁决把 T4 的"双字替掉逐字"**回退**了）：`semantic_fts`
   里落的是逐字拆，双字只做重排/打分信号（`bigram_coverage`），生产排序是
   `bm25()`（见 `tests/memory/test_semantic_search.py`）。

**函数与用例留着，不许删**：T5 的裁决是"回退召回口径"，不是"删掉双字"。谁想把
双字接回召回，先拿 `scripts/memory_relevance.py --arm bigram` 的读数说话——16 条
语料上它的 P@5/R@5（`0.23076923076923078` / `0.46153846153846156`）都低于逐字。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spoolkit.memory.semantic import insert_semantic, search_semantic
from spoolkit.memory.store import (
    _match_query,
    bigram_coverage,
    bigram_tokens,
    init_memory_schema,
    normalize_for_fts,
    normalize_for_fts_bigram,
    semantic_match_query,
)
from spoolkit.store.db import open_db


def _conn(tmp_path: Path):
    """一个建好 v4 结构的空库（`open_db` 已把 `row_factory` 设成 `sqlite3.Row`）。"""
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


# ---- 1. 切分规则（字面量钉住）-----------------------------------------------


def test_双字切分_逐字等于设计稿那一串() -> None:
    """验收 1 的**字面量**断言：token 列表与"空格连起来"两种写法都钉住。

    期望值来自设计稿 §14.8：`归档旧的任务记录` → `归档 档旧 旧的 的任 任务…`
    （计划 T4 验收 1 把它补全成 `归档 档旧 旧的 的任 任务 务记 记录`）。
    """
    assert bigram_tokens("归档旧的任务记录") == [
        "归档",
        "档旧",
        "旧的",
        "的任",
        "任务",
        "务记",
        "记录",
    ]
    assert normalize_for_fts_bigram("归档旧的任务记录") == (
        "归档 档旧 旧的 的任 任务 务记 记录"
    )


def test_双字_恰好两个字时就是一个双字() -> None:
    """验收 1 的后半：`bigram("归档") == "归档"`（不是"归档 档旧"）。"""
    assert bigram_tokens("归档") == ["归档"]
    assert normalize_for_fts_bigram("归档") == "归档"


def test_双字_标点是段边界_不生成跨边界token() -> None:
    """验收 3：`归档，删除` 里不许出现 `档，` / `，删` 这种假双字。"""
    tokens = bigram_tokens("归档，删除")
    assert tokens == ["归档", "删除"]
    assert "档，" not in tokens
    assert "，删" not in tokens
    # 空白、斜杠、连字符同理（都是段边界，不进 token）。
    assert bigram_tokens("归档 删除") == ["归档", "删除"]
    assert bigram_tokens("docs/cli.md") == ["docs", "cli", "md"]


def test_双字_字母数字段整段成token_memory侧逐字口径不变() -> None:
    """口径 4 + 一条防顺手改：`uv` 不碎成 `u`/`v`；`memory` 侧仍是逐字。"""
    assert bigram_tokens("本项目统一用 uv 管理依赖") == [
        "本项",
        "项目",
        "目统",
        "统一",
        "一用",
        "uv",
        "管理",
        "理依",
        "依赖",
    ]
    # CJK 与 ASCII 相邻处**不造**跨脚本双字（`用u` / `v管` 都是噪声）。
    assert "用u" not in bigram_tokens("用uv管理依赖")
    assert "v管" not in bigram_tokens("用uv管理依赖")
    # `memory` / `memory_fts` 侧的逐字口径一个字不动（决议 7 的只读精神）。
    assert normalize_for_fts("归档ab").split() == ["归", "档", "ab"]
    assert _match_query("归档") == "归 OR 档"


def test_双字_空文本与纯标点切不出token() -> None:
    assert bigram_tokens("") == []
    assert bigram_tokens("，，，") == []
    assert normalize_for_fts_bigram("") == ""
    assert semantic_match_query("") == ""


def test_双字_MATCH串是OR且每段带引号() -> None:
    """`_match_query` 的形态照旧（OR），只是 token 换成了双字（对照臂的查询侧）。"""
    assert semantic_match_query("清理旧文件") == '"清理" OR "理旧" OR "旧文" OR "文件"'
    assert semantic_match_query("uv") == '"uv"'


# ---- 1b. 双字覆盖率：T5 保留它的**唯一用途**（重排/打分信号）-----------------


def test_双字覆盖率_是打分信号不是召回口径() -> None:
    """覆盖率 = 查询的双字有多少在条目里出现（集合交，除以查询的双字数）。

    `清理旧文件` 那两行就是 T5 保留双字的理由与代价：它辨得出 `清理历史数据并
    备份`（1/4），但**对 `归档旧的任务记录` 给 0**——那一行是靠逐字 `旧` 才召回
    的（跨词边界）。所以它只能当**次序号/信号**，不能当召回口径。
    """
    assert bigram_coverage("归档", "归档旧的任务记录") == 1.0
    assert bigram_coverage("清理旧文件", "清理历史数据并备份") == 1 / 4
    assert bigram_coverage("清理旧文件", "归档旧的任务记录") == 0.0  # `旧文` 不存在
    assert bigram_coverage("依赖管理", "本项目统一用 uv 管理依赖") == 2 / 3
    # 分母是查询的双字数：查询切不出双字时给 0.0（不是除零）。
    assert bigram_coverage("，，，", "任何文本") == 0.0
    assert bigram_coverage("", "任何文本") == 0.0


# ---- 2. 单字查询（不回归）---------------------------------------------------


def test_单字查询_能命中_不回归(tmp_path: Path) -> None:
    """验收 2：查 `归` 命中 `归档旧的任务记录`。

    这条用例是**回归锚**：谁给单字查询加一道长度闸、或让召回在单字上返回空，
    它就红——而 `归` 正是设计稿 §14.8 点名的粒度案例，归零是回归不是"更精确"。
    T5 起命中由**逐字召回**直接给出（`_match_query("归") == '"归"'`）；
    `_semantic_single_char_fallback` 是这条之外的安全网（FTS 一个 token 都没命中
    时兜底），由 `test_semantic_search.py::test_回退路径的行没有bm25原值` 单独钉。
    """
    conn = _conn(tmp_path)
    item = insert_semantic(conn, "fact", "归档旧的任务记录", content_hash="t4-a")
    insert_semantic(conn, "fact", "把过期条目存档", content_hash="t4-b")

    assert _match_query("归") == '"归"'  # 逐字：单字查询切得出一个 token
    assert semantic_match_query("归") == '"归"'  # 双字对照臂的查询侧同样是 1 个
    assert [row.id for row in search_semantic(conn, "归", limit=5)] == [item.id]
    # 语料里的第 2 条**不含** `归`（"把过期条目存档" 一个字都不匹配），所以
    # 召回恰好是"含该字的条目"，不是把整串 LIKE 放回来（放回来的话 `存档`
    # 也会一起命中）。
    assert [row.id for row in search_semantic(conn, "归", limit=100)] == [item.id]
    conn.close()


def test_逐字召回的代价_虚字也命中_精度交给bm25补(tmp_path: Path) -> None:
    """逐字 OR 的已知代价：查询里的虚字（`的`）会把无关条目拉进候选。

    `完全无关的词` 里只有 `的` 出现在 `归档旧的任务记录` 中，逐字召回**会**命中
    它（T4 的双字口径下这里命中 0 条）。这是"召回换逐字"付出的代价，补偿手段是
    `bm25()` 排序（把真正相关的顶上去）与 T6 的五维重排——**不是**把召回再换回
    双字：那会连 `清理旧文件` → `归档旧的任务记录`（跨词边界）一起丢掉，而那条
    正是设计稿 §14.8 点名的 embed-wins 案例。
    """
    conn = _conn(tmp_path)
    noise = insert_semantic(conn, "fact", "归档旧的任务记录", content_hash="t4-c")
    hit = insert_semantic(conn, "fact", "清理历史数据并备份", content_hash="t4-d")

    assert [row.id for row in search_semantic(conn, "完全无关的词", limit=5)] == [noise.id]
    # 对照：真正相关的查询只给相关的条目。
    assert [row.id for row in search_semantic(conn, "清理", limit=5)] == [hit.id]
    conn.close()


# ---- 3. 召回口径是逐字，存储文本不被切碎 -----------------------------------


def test_存储文本不被切碎_索引里是逐字(tmp_path: Path) -> None:
    """`semantic.text` 一个字节不改；`semantic_fts` 里落的是**逐字拆**。

    T5 的裁决把 T4 的双字索引口径回退了（`store.normalize_for_fts`），所以这条
    断言与 `test_semantic_search.py::test_召回口径是逐字_不是双字` 一起，把"谁
    再把双字接回召回"这件事挡在红线上。
    """
    conn = _conn(tmp_path)
    original = "归档旧的任务记录"
    item = insert_semantic(conn, "fact", original, content_hash="t4-e")

    stored = conn.execute(
        "SELECT text FROM semantic WHERE id = ?", (item.id,)
    ).fetchone()["text"]
    assert stored == original  # 用户可见的原文：原样

    indexed = conn.execute(
        "SELECT text FROM semantic_fts WHERE rowid = ?", (item.id,)
    ).fetchone()["text"]
    assert indexed == normalize_for_fts(original)  # 索引：逐字（与 memory_fts 同口径）
    assert indexed != normalize_for_fts_bigram(original)  # 不是双字那一份
    conn.close()


def test_semantic_fts_里没有触发器_索引由应用层写(tmp_path: Path) -> None:
    """T4/T5 沿用「应用层显式写索引」这条纪律（触发器里没有分词的 Python 函数）。"""
    conn = _conn(tmp_path)
    assert (
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='semantic'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


@pytest.mark.parametrize(
    "query,expected",
    [
        ("测试命令", 1),  # 正文（逐字）
        ("pytest", 1),  # 字母数字段
        ("cli", 1),  # 证据列（`docs/cli.md:12`）
    ],
)
def test_三列都查得到_逐字口径下(tmp_path: Path, query: str, expected: int) -> None:
    """`semantic_fts(text,trigger,evidence)` 三列在逐字口径下仍都查得到。"""
    conn = _conn(tmp_path)
    insert_semantic(
        conn,
        "fact",
        "测试命令是 pytest -q",
        trigger="pytest,测试",
        evidence="docs/cli.md:12",
        content_hash="t4-f",
    )
    assert len(search_semantic(conn, query, limit=5)) == expected
    conn.close()


def test_夹具里的每条语料都切得出双字() -> None:
    """16 条语料**每条**都切得出至少一个双字 token（否则那条在索引里是空的）。"""
    corpus_path = (
        Path(__file__).resolve().parents[2] / "benchmarks" / "memory" / "corpus.jsonl"
    )
    rows = [
        json.loads(line)
        for line in corpus_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 16
    for row in rows:
        assert bigram_tokens(row["text"]), row
