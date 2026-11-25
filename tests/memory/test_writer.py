"""五道门禁落到 `semantic` 写入路径（P1 计划 T7 / 设计稿 §5.3）。

判据本身（J1–J5）在 `test_gate.py` 里逐条钉过；这一组用例钉的是**另一件事**：
这些门禁是不是真的长在**写入口**上——`semantic.write_memory`。

七件事，一件一组：

1. **去噪**：J1/J3/J4/J5 的噪声在写入口上都是 `skipped='denied'` + 可读理由；
2. **J2**：缺 `evidence` 一律拒（`evidence=""`），给得出出处才收；
3. **去重双层**：应用层给可读的 `duplicate` 结果，**数据库层**的
   `idx_semantic_dedupe` 部分唯一索引挡住"绕过应用层的直接 INSERT"；
4. **冲突**：旧行 `superseded`、新行 `active`、**两条都查得到**；判定失败时
   两条都留 `active` 并标 `needs_review`（不许静默选一个）；
5. **脱敏**：密钥形状的值不进库、不进 `semantic_fts`，也不进账本/审计与理由；
   全模块**只有一处**调用点；
6. **幂等**：同一轮由 `memory_write` 的 `idx_write_round` 挡住（不是先查后写）；
7. **单写**：写 `semantic` 不往旧 `memory` 表塞第二份（行数不变、旧代码路径照跑）。

源码级用例（三处调用点/插行点的计数）是**有意**的：它们钉的正是"只有一处"这条
设计纪律——行为用例证明"现在对"，源码级用例证明"以后多一个入口就红"。
"""

import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from spoolkit.memory import semantic
from spoolkit.memory.gate import content_hash as compute_content_hash
from spoolkit.memory.gate import sanitize
from spoolkit.memory.semantic import (
    WriteRequest,
    get_semantic,
    list_semantic,
    search_semantic,
    write_memory,
)
from spoolkit.memory.store import add_memory, init_memory_schema, search_memories
from spoolkit.store.db import open_db

SEMANTIC_SRC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "spoolkit"
    / "memory"
    / "semantic.py"
)
# 一个能过 J2 的出处（形状是 `file:line`；判据不要求文件真的存在）。
EVIDENCE = "tests/memory/test_writer.py:1"


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def _request(**overrides: Any) -> WriteRequest:
    """一条默认能过五道门禁的请求；`round_key` 默认空 = 没有轮次上下文。

    默认空是**有意的**：大多数用例考的是判据/判重/冲突，不该被幂等那道闸先拦住
    （考幂等的用例自己传 `round_key`）。
    """
    fields: dict[str, Any] = {
        "kind": "fact",
        "text": "测试命令是 pytest -q",
        "evidence": EVIDENCE,
        "source": "tool:remember",
    }
    fields.update(overrides)
    return WriteRequest(**fields)


def _ledger(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT round_key, source, written, skipped, detail FROM memory_write"
        )
    ]


# ---- 1. 去噪：J1/J3/J4/J5 长在写入口上 ---------------------------------------

DENY_CASES: list[tuple[str, str, str, str, str]] = [
    # （kind, 文本, 触发词, 出处, 应当命中的判据编号）
    ("fact", "这次先跑一遍测试看看。", "", EVIDENCE, "J1"),
    ("fact", "你之前问过两次同样的问题。", "", EVIDENCE, "J1"),
    ("fact", "你好！我是你的本地编程助手。", "", EVIDENCE, "J3"),
    ("fact", "感谢你的确认，我这就去改。", "", EVIDENCE, "J3"),
    ("fact", "运行方式：python -m spoolkit.cli.app --goal 目标", "", EVIDENCE, "J4"),
    ("fact", "| 城市 | 天气 | 温度 |\n| 厦门 | 晴 | 30 |\n| 北京 | 多云 | 18 |", "", EVIDENCE, "J4"),
    ("fact", "结尾给一段代码：\n\n```python\nprint(1)\n```", "", EVIDENCE, "J4"),
    ("lesson", "解析自定义格式先上语法约束", "", EVIDENCE, "J5"),
    ("fact", "构建命令是 uv build", "", "", "J2"),
]


@pytest.mark.parametrize(("kind", "text", "trigger", "evidence", "rule_id"), DENY_CASES)
def test_五道门禁都落在写入口上(
    tmp_path: Path, kind: str, text: str, trigger: str, evidence: str, rule_id: str
) -> None:
    conn = _conn(tmp_path)
    result = write_memory(
        conn,
        _request(
            kind=kind,
            text=text,
            trigger=trigger,
            evidence=evidence,
            round_key=f"deny-{rule_id}-{text[:4]}",
        ),
    )

    assert result.written is False
    assert result.skipped == "denied"
    assert result.reason.startswith(rule_id)
    assert list_semantic(conn) == []
    # 被拒也要留痕：账本一行 + 审计一行（§5.3 ②"降级要写 audit"）。
    assert [row[3] for row in _ledger(conn)] == ["denied"]
    assert conn.execute("SELECT count(*) FROM memory_audit").fetchone()[0] == 1
    conn.close()


def test_非法kind被拒(tmp_path: Path) -> None:
    """表上没有 `CHECK` 约束（§4.3 的 DDL 逐字如此），所以这道校验只能在写入口做。"""
    conn = _conn(tmp_path)
    result = write_memory(conn, _request(kind="gossip"))
    assert result.written is False
    assert result.skipped == "denied"
    assert "kind 非法" in result.reason
    assert list_semantic(conn) == []
    conn.close()


# ---- 2. J2：缺出处一律拒 -----------------------------------------------------


def test_缺证据被拒(tmp_path: Path) -> None:
    """计划 T7 验收 1：`evidence=""` → 拒 + 账本 `denied` + 审计一行。"""
    conn = _conn(tmp_path)
    result = write_memory(
        conn,
        _request(text="构建命令是 uv build", evidence="", round_key="round-j2"),
    )

    assert result.written is False
    assert result.skipped == "denied"
    assert result.reason.startswith("J2")
    assert "缺出处" in result.reason
    assert list_semantic(conn) == []
    ledger = _ledger(conn)
    assert len(ledger) == 1 and ledger[0][2] == 0 and ledger[0][3] == "denied"
    assert conn.execute("SELECT count(*) FROM memory_audit").fetchone()[0] == 1

    # 正例：给得出出处就收（同一段文本，只差 evidence）。
    accepted = write_memory(
        conn,
        _request(
            text="构建命令是 uv build",
            evidence="pyproject.toml:1",
            round_key="round-j2-ok",
        ),
    )
    assert accepted.written is True
    assert accepted.ref == "fact:1"
    row = get_semantic(conn, accepted.semantic_id)
    assert row is not None and row.evidence == "pyproject.toml:1"
    conn.close()


def test_看得出出处的evidence照收(tmp_path: Path) -> None:
    """J2 的第二段检查不许过严：命令、工具调用、库内引用名都算可核对出处。"""
    conn = _conn(tmp_path)
    for index, evidence in enumerate(
        (
            "pytest -q tests/memory",
            "read(src/spoolkit/memory/semantic.py)",
            "semantic:12",
        )
    ):
        result = write_memory(
            conn,
            _request(
                text=f"构建命令是 uv build 第 {index} 条",
                evidence=evidence,
                round_key=f"shape-{index}",
            ),
        )
        assert result.written is True, result.reason
    assert len(list_semantic(conn)) == 3
    conn.close()


# ---- 3. 去重：应用层可读 + 数据库层唯一（双层） ------------------------------


def test_判重命中既有行不改行数(tmp_path: Path) -> None:
    """计划 T7 验收 3：同一条第二次写 → 不新增、既有行 `use_count` +1、可读结论。"""
    conn = _conn(tmp_path)
    first = write_memory(conn, _request(text="测试命令是 pytest -q"))

    # 归一化后同一条（空白/标点/大小写不同）：这正是 content_hash 归一化的目标。
    second = write_memory(
        conn,
        _request(text=" 测试命令是 PYTEST -Q。", evidence="pyproject.toml:2"),
    )

    assert first.written is True
    assert second.written is False
    assert second.skipped == "duplicate"
    assert second.duplicate_of == first.semantic_id
    assert "同一条" in second.reason
    assert len(list_semantic(conn)) == 1
    assert get_semantic(conn, first.semantic_id).use_count == 1
    conn.close()


def test_绕过应用层直接插同hash被数据库拒绝(tmp_path: Path) -> None:
    """计划 T7 验收 2 的直接插入那一条：挡住它的是**数据库的唯一部分索引**。

    为什么必须从这个角度钉：应用层那一查只是"为了结果可读"，真正的守门人是
    `idx_semantic_dedupe ... WHERE status='active'`。这条用例绕过 `write_memory`、
    直接 INSERT 同一个 `content_hash`，所以它红只能是因为索引没了/被改成了普通索引。
    """
    conn = _conn(tmp_path)
    first = write_memory(conn, _request(text="测试命令是 pytest -q"))
    digest = get_semantic(conn, first.semantic_id).content_hash
    assert digest == compute_content_hash("测试命令是 pytest -q")

    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        conn.execute(
            "INSERT INTO semantic"
            "(kind, scope, scope_id, text, trigger, steps, source, evidence,"
            " confidence, importance, content_hash, status, created_at)"
            " VALUES ('fact','workspace','','绕过来的第二条','','','','x',0.5,0.5,?,"
            "'active',0)",
            (digest,),
        )
    assert "UNIQUE" in str(excinfo.value)
    conn.rollback()

    # 部分唯一索引的正面：旧行归档之后，同一个 hash 可以再写一条（§5.3 ⑤ 的反面）。
    conn.execute(
        "UPDATE semantic SET status = 'archived' WHERE id = ?", (first.semantic_id,)
    )
    conn.commit()
    again = write_memory(
        conn, _request(text="测试命令是 pytest -q", evidence="pyproject.toml:3")
    )
    assert again.written is True
    assert again.semantic_id != first.semantic_id
    conn.close()


def test_同义改写不合并_已知代价(tmp_path: Path) -> None:
    """§5.3 ④ 第 2 级（近似去重）**不在本轮的落地范围**：这里把代价如实钉住。

    "用 uv 管理依赖" 与 "本仓统一用 uv" 在指纹上不同 → 两条都收。这不是 bug，
    是"不引嵌入、只做结构性去重"的已知代价（设计稿 §5.3 ④ 的注）。
    """
    conn = _conn(tmp_path)
    first = write_memory(conn, _request(text="用 uv 管理依赖"))
    second = write_memory(
        conn, _request(text="本仓统一用 uv", evidence="pyproject.toml:2")
    )

    assert first.written is True
    assert second.written is True
    assert len(list_semantic(conn)) == 2

    # 只差空白/标点的写法仍被判重（精确级）。
    third = write_memory(
        conn, _request(text="用uv管理依赖。", evidence="pyproject.toml:3")
    )
    assert third.written is False
    assert third.duplicate_of == first.semantic_id
    conn.close()


def test_归一化后为空的条目被拒(tmp_path: Path) -> None:
    """空指纹不许落库：`idx_semantic_dedupe` 是**按 content_hash 唯一**的，
    两条空指纹会互相撞、被误报成"同一条"。"""
    conn = _conn(tmp_path)
    for index, text in enumerate(("！！！", "？？")):
        result = write_memory(
            conn, _request(text=text, round_key=f"empty-{index}")
        )
        assert result.written is False
        assert result.skipped == "denied"
        assert "content_hash" in result.reason
    assert list_semantic(conn) == []
    conn.close()


# ---- 4. 冲突：两条都查得到，active 是明确的 ---------------------------------


def test_冲突不静默选一个(tmp_path: Path) -> None:
    """计划 T7 验收 5：判定为"矛盾" → 旧行 superseded（**仍可查**）、新行 active。"""
    conn = _conn(tmp_path)
    old = write_memory(conn, _request(text="测试命令是 pytest -q"))
    new = write_memory(
        conn,
        _request(text="测试命令是 uv run pytest -q", evidence="pyproject.toml:2"),
        decide=lambda old_row, request: "矛盾",
    )

    assert new.written is True
    assert new.superseded_id == old.semantic_id

    # 「两条都能查出来」：默认列举（不过滤 status）两条都在，旧行的正文一字不改。
    rows = list_semantic(conn, kind="fact")
    assert [row.id for row in rows] == [old.semantic_id, new.semantic_id]
    old_row = get_semantic(conn, old.semantic_id)
    assert old_row.status == "superseded"
    assert old_row.superseded_by == new.semantic_id
    assert old_row.text == "测试命令是 pytest -q"

    # 「哪条 active 是明确的」：active 只有一个，就是新行。
    assert [row.id for row in list_semantic(conn, status="active")] == [
        new.semantic_id
    ]
    assert [row.id for row in list_semantic(conn, status="superseded")] == [
        old.semantic_id
    ]

    # 检索也两条都在（旧行不删：`show <旧 id>` 仍看得到"我们曾经这么认为"）。
    hits = {row.id for row in search_semantic(conn, "测试命令")}
    assert {old.semantic_id, new.semantic_id} <= hits

    links = {
        tuple(row)
        for row in conn.execute(
            "SELECT src_id, dst_id, relation FROM memory_link"
        )
    }
    assert (new.semantic_id, old.semantic_id, "supersedes") in links
    conn.close()


@pytest.mark.parametrize(
    "decide",
    [
        None,  # 没有判定调用（本模块不引模型）
        lambda old_row, request: "大概吧",  # 判定不可解析
    ],
)
def test_冲突判定失败时两条都留active(tmp_path: Path, decide: Any) -> None:
    """§5.3 ⑤ 原话："静默丢一条比留着两条更危险"：判定失败 → 两条 active + 待复核。"""
    conn = _conn(tmp_path)
    old = write_memory(conn, _request(text="测试命令是 pytest -q"))
    new = write_memory(
        conn,
        _request(text="测试命令是 uv run pytest -q", evidence="pyproject.toml:2"),
        decide=decide,
    )

    assert new.written is True
    assert new.skipped == "conflict"
    assert "needs_review" in new.reason
    assert get_semantic(conn, old.semantic_id).status == "active"
    assert get_semantic(conn, new.semantic_id).status == "active"
    links = {
        tuple(row)
        for row in conn.execute("SELECT src_id, dst_id, relation FROM memory_link")
    }
    assert (new.semantic_id, old.semantic_id, "needs_review") in links
    conn.close()


def test_判定调用抛异常也不许炸掉写入(tmp_path: Path) -> None:
    """判定是一个**可失败点**：它抛异常时走"两条都 active"的保守路径，不崩。"""
    conn = _conn(tmp_path)
    old = write_memory(conn, _request(text="测试命令是 pytest -q"))

    def boom(old_row: Any, request: Any) -> str:
        raise RuntimeError("判定服务没起来")

    new = write_memory(
        conn,
        _request(text="测试命令是 uv run pytest -q", evidence="pyproject.toml:2"),
        decide=boom,
    )
    assert new.written is True
    assert get_semantic(conn, old.semantic_id).status == "active"
    assert new.superseded_id is None
    conn.close()


def test_判定为无关时不动旧行(tmp_path: Path) -> None:
    """机械的主题键重叠只是**候选**；判定说"无关"就不取代、也不打待复核标记。"""
    conn = _conn(tmp_path)
    old = write_memory(conn, _request(text="测试命令是 pytest -q"))
    new = write_memory(
        conn,
        _request(text="测试命令是 uv run pytest -q", evidence="pyproject.toml:2"),
        decide=lambda old_row, request: "无关",
    )
    assert new.written is True
    assert get_semantic(conn, old.semantic_id).status == "active"
    assert new.superseded_id is None
    assert (
        conn.execute("SELECT count(*) FROM memory_link").fetchone()[0] == 0
    )
    conn.close()


# ---- 5. 敏感：脱敏只在写入侧一处，值不进库也不进索引 ------------------------


SECRET_CASES: list[tuple[str, str, str]] = [
    # （文本, 值本身, 应当命中的形状名里的关键词）
    ("部署时带上 sk-live-abcdefghijklmnop，别写进记忆", "sk-live-abcdefghijklmnop", "供应商密钥前缀"),
    ("配置里写 SPOOLKIT_QQ_SECRET=abcdef123456，先放着", "abcdef123456", "键值对里的密钥值"),
    ("请求头写 Authorization: Bearer abcdefghijklmn", "abcdefghijklmn", "Bearer"),
]


@pytest.mark.parametrize(("text", "secret", "shape_hint"), SECRET_CASES)
def test_脱敏在写入侧一处_值不进库不进索引(
    tmp_path: Path, text: str, secret: str, shape_hint: str
) -> None:
    """计划 T7 验收 6：`skipped='redacted'`、库里查不到值、理由里只有形状名。"""
    conn = _conn(tmp_path)
    result = write_memory(
        conn,
        _request(text=text, evidence="tests/memory/test_writer.py:9", round_key="secret"),
    )

    assert result.written is False
    assert result.skipped == "redacted"
    assert any(shape_hint in shape for shape in result.shapes)
    # 理由只回显**形状名**，不回显值。
    assert secret not in result.reason

    # 值不进表、不进索引、也不进账本与审计。
    assert list_semantic(conn) == []
    assert conn.execute("SELECT count(*) FROM semantic_fts").fetchone()[0] == 0
    ledger_row = conn.execute(
        "SELECT skipped, detail FROM memory_write"
    ).fetchone()
    assert ledger_row["skipped"] == "redacted"
    assert secret not in ledger_row["detail"]
    for row in conn.execute("SELECT reason, before, after FROM memory_audit"):
        assert secret not in " ".join(str(value) for value in tuple(row))
    # 连数据库文件的字节里都搜不到（`memory.db` 长期落盘，密钥进去就是长期泄漏）。
    assert secret.encode("utf-8") not in (tmp_path / "memory.db").read_bytes()
    conn.close()


@pytest.mark.parametrize(("text", "secret", "shape_hint"), SECRET_CASES)
def test_脱敏保留键名只盖值(text: str, secret: str, shape_hint: str) -> None:
    """脱敏的口径（复用读路径那一份形状表）：**键名可留**，值一律盖掉。"""
    cleaned, shapes = sanitize(text)
    assert secret not in cleaned
    assert shapes and any(shape_hint in shape for shape in shapes)
    if "=" in text:
        assert "SPOOLKIT_QQ_SECRET" in cleaned


def test_脱敏在写入侧只有一处调用点(tmp_path: Path) -> None:
    """§5.3 ① 的"只有一处"是**源码级**的：多一个调用点，就多一处会漏的地方。"""
    src = SEMANTIC_SRC.read_text(encoding="utf-8")
    assert len(re.findall(r"\bsanitize\(", src)) == 1
    # 形状表只有一份：语义层不自己抄正则、也不直接去调读路径那个函数。
    assert "redact_secrets" not in src


# ---- 6. 幂等：同一轮由唯一索引挡（不是先查后写） ----------------------------


def test_幂等由唯一索引挡(tmp_path: Path) -> None:
    """计划 T7 验收 2：同一 `round_key` + 同一 `source` 写两次 → 账本一行、条目一条。"""
    conn = _conn(tmp_path)
    first = write_memory(conn, _request(round_key="round-1"))
    # 第二次连内容都换了：挡住它的是**轮次**，不是判重。
    second = write_memory(
        conn, _request(text="依赖只用标准库", evidence="pyproject.toml:2", round_key="round-1")
    )

    assert first.written is True
    assert second.written is False
    assert second.skipped == "duplicate"
    assert "同一轮" in second.reason
    assert "idx_write_round" in second.reason
    assert len(_ledger(conn)) == 1
    assert len(list_semantic(conn)) == 1
    conn.close()


def test_被拒的那一轮也被烧掉(tmp_path: Path) -> None:
    """幂等盯的是**轮次**：第一次被 J2 拒了、同一轮换条合格内容也不许再写。"""
    conn = _conn(tmp_path)
    denied = write_memory(conn, _request(evidence="", round_key="round-2"))
    again = write_memory(
        conn, _request(text="依赖只用标准库", evidence="pyproject.toml:1", round_key="round-2")
    )

    assert denied.skipped == "denied"
    assert again.skipped == "duplicate"
    assert list_semantic(conn) == []
    assert len(_ledger(conn)) == 1
    conn.close()


def test_没有轮次上下文的写入不互相挡(tmp_path: Path) -> None:
    """`round_key` 为空 = 人手敲的一条：账本照记，但不拿内容当轮次键。"""
    conn = _conn(tmp_path)
    first = write_memory(conn, _request(text="依赖只用标准库"))
    second = write_memory(conn, _request(text="构建命令是 uv build"))

    assert first.written is True and second.written is True
    assert len(list_semantic(conn)) == 2
    assert len(_ledger(conn)) == 2
    conn.close()


def test_绕过应用层直接插同round被数据库拒绝(tmp_path: Path) -> None:
    """幂等的第二层也是数据库：直接 INSERT 同一 `(round_key, source)` → `IntegrityError`。"""
    conn = _conn(tmp_path)
    write_memory(conn, _request(round_key="round-x"))

    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        conn.execute(
            "INSERT INTO memory_write(round_key, source, written, detail, created_at)"
            " VALUES (?,?,0,'',0)",
            ("round-x", "tool:remember"),
        )
    assert "UNIQUE" in str(excinfo.value)
    conn.rollback()
    assert len(_ledger(conn)) == 1
    conn.close()


def test_幂等不是先查后写(tmp_path: Path) -> None:
    """源码级：抢位靠 INSERT 撞唯一索引，代码里**没有**对 `memory_write` 的预查。

    先查后写有竞态，而且"漏查"这类 bug 不会报错（§5.3 ③ 原话）。把这里改成
    "先 SELECT 再 INSERT"，`FROM memory_write` 这半句就出现了 → 这条用例红。
    """
    src = SEMANTIC_SRC.read_text(encoding="utf-8")
    assert src.count("INSERT INTO memory_write") == 1
    assert "FROM memory_write" not in src
    assert "IntegrityError" in src
    assert "idx_write_round" in src  # 理由要能说出是被哪个索引挡的


def test_判重的竞态兜底(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并发窗口：查过之后、写之前被别人抢了同一个 hash → 给可读结论，不崩。

    这里用 monkeypatch 把 `insert_semantic` 换成"必撞唯一索引"，模拟那个窗口
    （真造竞态要两个连接抢同一瞬间，测试里不可靠）。
    """
    conn = _conn(tmp_path)
    write_memory(conn, _request(text="测试命令是 pytest -q"))

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.IntegrityError("UNIQUE constraint failed: semantic.content_hash")

    monkeypatch.setattr(semantic, "insert_semantic", boom)
    result = write_memory(conn, _request(text="构建命令是 uv build"))

    assert result.written is False
    assert result.skipped == "duplicate"
    assert "idx_semantic_dedupe" in result.reason
    assert len(list_semantic(conn)) == 1
    # 回滚之后账本行重新抢位，还是只有一行。
    assert len(_ledger(conn)) == 2  # 第一条 + 第二条（各一行）
    conn.close()


# ---- 7. 单写：写 semantic 不往旧 memory 表塞第二份 ---------------------------


def test_单写semantic(tmp_path: Path) -> None:
    """计划 T7 验收 7：`write_memory` 成功后旧 `memory` 表行数不变、旧路径照跑。"""
    conn = _conn(tmp_path)
    legacy = add_memory(conn, kind="fact", text="旧表里的一条事实：构建命令是 uv build")
    before = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    fts_before = conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0]

    result = write_memory(conn, _request(text="依赖只用标准库"))

    assert result.written is True
    assert conn.execute("SELECT count(*) FROM memory").fetchone()[0] == before
    assert conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == fts_before
    assert len(list_semantic(conn)) == 1
    # 旧代码路径仍然查得到旧表里的行（旧表冻结、只读，不是"搬空"）。
    assert [row.id for row in search_memories(conn, "旧表")] == [legacy.id]
    conn.close()


def test_写入路径不碰旧memory表(tmp_path: Path) -> None:
    """源码级：写 `semantic` 的模块里没有一句写旧表的 SQL。"""
    src = SEMANTIC_SRC.read_text(encoding="utf-8")
    assert re.search(r"INSERT INTO memory\b", src) is None
    assert re.search(r"INSERT INTO memory_fts\b", src) is None
    assert "add_memory" not in src
    assert "insert_memory" not in src


def test_semantic只有一处插行点(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`write_memory` 只能走 `insert_semantic`（FTS 同步责任只在那一个门口）。"""
    conn = _conn(tmp_path)
    calls: list[tuple] = []
    real = semantic.insert_semantic

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(semantic, "insert_semantic", spy)
    result = write_memory(conn, _request())

    assert result.written is True
    assert len(calls) == 1

    src = SEMANTIC_SRC.read_text(encoding="utf-8")
    assert src.count('"INSERT INTO semantic"') == 1
    assert src.count("INSERT INTO semantic_fts(") == 1
    # 索引确实写了：写进去的条目当场检索得到。
    assert [row.id for row in search_semantic(conn, "测试命令")] == [result.semantic_id]
    conn.close()


# ---- 判据与写入口的一致性（不许两套判据） -----------------------------------


def test_写入口走gate的judge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.2：判据只许有一份。把 `gate.judge` 换成"一律拒"，写入口必须跟着拒。

    这条钉的是"写入口没有自己另写一套判据"——否则这里会照收不误。
    """
    from spoolkit.memory.gate import Verdict

    conn = _conn(tmp_path)
    monkeypatch.setattr(
        semantic, "judge", lambda *args, **kwargs: Verdict(False, "J9 假判据（被替换过）")
    )
    result = write_memory(conn, _request())

    assert result.written is False
    assert result.skipped == "denied"
    assert result.reason.startswith("J9")
    assert list_semantic(conn) == []
    conn.close()
