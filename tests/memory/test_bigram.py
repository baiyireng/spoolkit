"""T4：`semantic` 侧的双字 shingle 口径（设计稿 §14.8 方案 A）。

这一组用例钉住四件事：

1. **切分规则**：`归档旧的任务记录` → `归档 档旧 旧的 的任 任务 务记 记录`
   **逐字**（字面量断言，不是"大概像"）；`归档` → `归档`；字母数字段整段成 token。
2. **不许造假双字**：标点/空白是段边界，`归档，删除` 里**不生成** `档，` / `，删`
   这类跨边界 token（§14.8 取舍列点名的噪声源）。
3. **单字查询不回归**：查 `归` 仍能命中 `归档旧的任务记录`（走原文子串回退）——
   它正是 §14.8 点名的「粒度」案例，归零就是回归。
4. **分词只动检索侧**：`semantic.text`（用户可见的原文）一个字节不被切碎；
   `memory` / `memory_fts` 侧的逐字口径（`normalize_for_fts`）**没被顺手改掉**。

顺序纪律（§13.11 / 计划 T4）：本任务的改动**只到分词口径**——`bm25()` 排序是 T5，
所以这里没有任何一条用例碰 `ORDER BY`。仪器不读数的情形由
`tests/memory/test_relevance_harness.py` 钉。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spoolkit.memory.semantic import insert_semantic, search_semantic
from spoolkit.memory.store import (
    _match_query,
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
    """`_match_query` 的形态照旧（OR + 引号），只是 token 换成了双字。"""
    assert semantic_match_query("清理旧文件") == '"清理" OR "理旧" OR "旧文" OR "文件"'
    assert semantic_match_query("uv") == '"uv"'


# ---- 2. 单字回退（不回归）---------------------------------------------------


def test_单字查询_能命中_不回归(tmp_path: Path) -> None:
    """验收 2：查 `归` 命中 `归档旧的任务记录`（FTS 命中为空 → 原文子串回退）。

    这条用例是**回归锚**：删掉回退（或让 `search_semantic` 在单字上直接返回空）
    它就红——而 `归` 正是设计稿 §14.8 点名的粒度案例。
    """
    conn = _conn(tmp_path)
    item = insert_semantic(conn, "fact", "归档旧的任务记录", content_hash="t4-a")
    insert_semantic(conn, "fact", "把过期条目存档", content_hash="t4-b")

    assert semantic_match_query("归") == '"归"'  # 单字仍切得出一个 token
    assert [row.id for row in search_semantic(conn, "归", limit=5)] == [item.id]
    # 语料里的第 2 条**不含** `归`（"把过期条目存档" 一个字都不匹配）——所以
    # 连回退也只给 1 条。这说明回退的召回恰好等于"含该字的条目"，不是把逐字
    # OR 偷偷放回来（放回来的话 `存档` 会一起命中）。
    assert [row.id for row in search_semantic(conn, "归", limit=100)] == [item.id]
    conn.close()


def test_单字回退_不会掩盖真实的零命中(tmp_path: Path) -> None:
    """回退只在**查询切不出可用命中**时兜底，不把"双字确实命中 0 条"改写成别的。

    `完全无关的词` 的双字（`完全` `全无` `无关` `关的` `的词`）在两条条目里一个都
    不存在，FTS 命中 0 条；而单字回退**也只对长度 < 2 的查询**生效（这里逐字
    `完` / `全` / … 都不在原文里，所以回退同样 0 条）。真实结果就是"查不到"——
    回退不该把它掩盖成"退回逐字 OR"（那样 `归档` / `存档` 会重新串味，
    精度增益就被吃掉了）。
    """
    conn = _conn(tmp_path)
    insert_semantic(conn, "fact", "归档旧的任务记录", content_hash="t4-c")
    insert_semantic(conn, "fact", "清理历史数据并备份", content_hash="t4-d")

    assert search_semantic(conn, "完全无关的词", limit=5) == []
    # 对照：双字命中时**不**走回退（`清理` 在第二条里）。
    assert len(search_semantic(conn, "清理", limit=5)) == 1
    conn.close()


# ---- 3. 分词只动检索侧，不动存储文本 ---------------------------------------


def test_存储文本不被切碎_索引里才是双字(tmp_path: Path) -> None:
    """`semantic.text` 一个字节不改；双字只落进 `semantic_fts` 的索引文本。"""
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
    assert indexed == "归档 档旧 旧的 的任 任务 务记 记录"  # 索引：双字
    assert indexed != normalize_for_fts(original)  # 不是逐字拆那一份
    conn.close()


def test_semantic_fts_里没有触发器_索引由应用层写(tmp_path: Path) -> None:
    """T4 沿用「应用层显式写索引」这条纪律（触发器里没有分词的 Python 函数）。"""
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
        ("测试命令", 1),  # 双字命中正文
        ("pytest", 1),  # 字母数字段整段
        ("cli", 1),  # 证据列（`docs/cli.md:12`）
    ],
)
def test_三列都查得到_双字口径下(tmp_path: Path, query: str, expected: int) -> None:
    """`semantic_fts(text,trigger,evidence)` 三列在双字口径下仍都查得到。"""
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
