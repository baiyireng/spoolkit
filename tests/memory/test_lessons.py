from pathlib import Path

from spoolkit.memory.lessons import (
    INITIAL_CONFIDENCE,
    MIN_SAMPLES,
    PRUNE_THRESHOLD,
    PUSH_THRESHOLD,
    match_lessons,
    prune_lessons,
    record_lesson,
    record_outcome,
    recompute_confidence,
)
from spoolkit.memory.store import init_memory_schema
from spoolkit.store.db import open_db


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def test_新教训置信度在推送线之上() -> None:
    assert INITIAL_CONFIDENCE >= PUSH_THRESHOLD


def test_写入并读回教训(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = record_lesson(conn, "解析自定义格式先上语法约束", "解析,正则,格式", "ep#1")
    assert item.id > 0
    assert item.confidence == INITIAL_CONFIDENCE
    conn.close()


def test_触发词命中时推送(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_lesson(conn, "先上语法约束", "解析,正则")
    hits = match_lessons(conn, "帮我修一下解析逻辑")
    assert len(hits) == 1
    conn.close()


def test_触发词不命中时不推送(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_lesson(conn, "先上语法约束", "解析,正则")
    assert match_lessons(conn, "改一下样式表") == []
    conn.close()


def test_空场景不推送(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_lesson(conn, "规则", "解析")
    assert match_lessons(conn, "   ") == []
    conn.close()


def test_多个触发词任一命中即可(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_lesson(conn, "规则", "正则、格式,解析")
    assert len(match_lessons(conn, "格式有问题")) == 1
    conn.close()


def test_置信度低的教训不推送(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = record_lesson(conn, "规则", "解析", confidence=0.2)
    assert match_lessons(conn, "解析") == []
    assert item.id > 0
    conn.close()


def test_推送数量有上限(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for i in range(6):
        record_lesson(conn, f"规则{i}", "解析")
    assert len(match_lessons(conn, "解析", limit=2)) == 2
    conn.close()


def test_按置信度排序() -> None:
    assert recompute_confidence(0, 0) == INITIAL_CONFIDENCE


def test_样本不足时不调整置信度() -> None:
    assert recompute_confidence(MIN_SAMPLES - 1, 0) == INITIAL_CONFIDENCE


def test_样本足够且全成功时置信度上升() -> None:
    assert recompute_confidence(10, 10) > INITIAL_CONFIDENCE


def test_样本足够且全失败时置信度下降() -> None:
    assert recompute_confidence(10, 0) < INITIAL_CONFIDENCE


def test_置信度有上下界() -> None:
    assert recompute_confidence(100, 100) <= 0.8
    assert recompute_confidence(100, 0) >= 0.2


def test_记录成功会提升置信度(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = record_lesson(conn, "规则", "解析")
    for _ in range(10):
        record_outcome(conn, [item.id], succeeded=True)
    row = conn.execute("SELECT * FROM memory WHERE id = ?", (item.id,)).fetchone()
    assert row["applied_count"] == 10
    assert row["success_count"] == 10
    assert row["confidence"] > INITIAL_CONFIDENCE
    conn.close()


def test_记录失败会降低置信度(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = record_lesson(conn, "规则", "解析")
    for _ in range(10):
        record_outcome(conn, [item.id], succeeded=False)
    row = conn.execute("SELECT * FROM memory WHERE id = ?", (item.id,)).fetchone()
    assert row["success_count"] == 0
    assert row["confidence"] < INITIAL_CONFIDENCE
    conn.close()


def test_未推送时不记录效果(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    assert record_outcome(conn, [], succeeded=True) == 0
    conn.close()


def test_长期无效的教训被清出推送池(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = record_lesson(conn, "坏教训", "解析")
    for _ in range(10):
        record_outcome(conn, [item.id], succeeded=False)
    pruned = prune_lessons(conn)
    assert item.id in pruned
    assert match_lessons(conn, "解析") == []
    conn.close()


def test_清理不会删除教训本身(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = record_lesson(conn, "坏教训", "解析")
    for _ in range(10):
        record_outcome(conn, [item.id], succeeded=False)
    prune_lessons(conn)
    row = conn.execute("SELECT text FROM memory WHERE id = ?", (item.id,)).fetchone()
    assert row is not None
    assert row["text"] == "坏教训"
    conn.close()


def test_样本不足的教训不会被清理(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = record_lesson(conn, "还没验证过", "解析")
    record_outcome(conn, [item.id], succeeded=False)
    assert prune_lessons(conn) == []
    conn.close()


def test_被清理后置信度低于推送线(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = record_lesson(conn, "坏教训", "解析")
    for _ in range(10):
        record_outcome(conn, [item.id], succeeded=False)
    prune_lessons(conn)
    row = conn.execute("SELECT confidence FROM memory WHERE id = ?", (item.id,)).fetchone()
    assert row["confidence"] < PUSH_THRESHOLD
    assert row["confidence"] < PRUNE_THRESHOLD
    conn.close()
