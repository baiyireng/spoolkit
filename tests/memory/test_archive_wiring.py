"""T7 的五道门禁接到生产写入路径之后（归档 / 蒸馏）的可证伪断言。

T7 把门禁落在 `semantic.write_memory` 上，但只要 `archive_task` 与 `distill` 还在
走老路（`store.add_memory` 直写旧 `memory` 表），门禁在生产里就是**空转**。
这一组用例钉的就是"接线"这件事，四块：

1. **写入口**：提升条目从 `write_memory` 进去（`memory_write` 有账本行、`semantic`
   有行），旧 `memory` 表**没有新增直写**（源码级 + 行为级两条）；
2. **J2 在生产链路里生效**：蒸馏出的条目给不出 evidence → 拒 → **降级为 episode**，
   原因在报告 / 账本 / 审计三处都看得到（不是丢掉、也不是静默变成 fact）；
3. **幂等按条**：一批多条各占一行账本（一轮共用一个键 = 第 2 条起被静默挡掉）；
4. **可读结论**：written / skipped / duplicate_of / 拒绝理由四样都能被人看见；
   热记忆文件的内容与容量口径不变（`test_archive.py` / `test_hot.py` 的既有用例
   继续钉），脱敏的值仍然一个都不落库。
"""

import json
from pathlib import Path

from spoolkit.agent.state import TaskState
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory import archive, lessons, store
from spoolkit.memory.archive import archive_task
from spoolkit.memory.distill import distill
from spoolkit.memory.hot import read_hot, write_hot
from spoolkit.memory.semantic import list_semantic, search_semantic
from spoolkit.memory.store import add_memory, init_memory_schema, list_memories
from spoolkit.store.db import open_db

ARCHIVE_SRC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "spoolkit"
    / "memory"
    / "archive.py"
)

# 一个能过 J2 的出处（形状是 `file:line`；判据不要求文件真的存在，与 test_writer 同）。
EVIDENCE = "tests/memory/test_archive_wiring.py:1"


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def _state() -> TaskState:
    """**没有 `excluded`**：这一组用例要能把"旧表零新增"说干净。

    `state.excluded` 那条 P0 落点（`memory.kind='failure'`）仍然是直写旧表，
    它是另一件事（见 `test_已排除的方案写成失败记忆`）。
    """
    return TaskState(
        task_id="t1",
        goal="给 parser 加增量更新",
        done=["读 parser.py", "确认索引表结构"],
        current="完成",
        step=4,
    )


def _archive(conn, tmp_path: Path, promote, **kwargs):
    return archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=promote,
        **kwargs,
    )


def _ledger(conn):
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT source, written, skipped, detail FROM memory_write ORDER BY id"
        )
    ]


def _audit(conn):
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT actor, action, target, reason FROM memory_audit ORDER BY id"
        )
    ]


# ---- 1. 写入口：提升条目从 write_memory 进去 -------------------------------


def test_提升条目从write_memory进去(tmp_path: Path) -> None:
    """真实归档一条带出处的 fact：`semantic` 有行、账本有行、报告能读。"""
    conn = _conn(tmp_path)
    fact = "测试命令是 pytest -q"
    result = _archive(conn, tmp_path, [("fact", fact, "", EVIDENCE)])

    rows = list_semantic(conn)
    assert [(row.kind, row.text, row.evidence) for row in rows] == [
        ("fact", fact, EVIDENCE)
    ]
    # 账本：source=distill（不是 tool:remember），written=1。
    assert _ledger(conn) == [("distill", 1, "", "")]
    # 审计：说得出来源与出处（§5.3 ②"收下也要写 audit"的同一口径）。
    audit = _audit(conn)
    assert audit and audit[-1][0] == "distill" and "收下" in audit[-1][3]
    # 报告里能看见（不是只活在进程里）。
    assert result.written == ["fact:1"]
    assert result.outcomes[0].written is True
    assert result.outcomes[0].ref == f"{rows[0].kind}:{rows[0].id}"
    # 旧 `memory` 表零新增（这一轮的 state 没有 excluded）。
    assert conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 0
    # 热记忆文件仍然是 P0 的样子。
    assert read_hot(tmp_path / "memory.md")["fact"] == [fact]
    # 写进去的条目当场检索得到（`semantic_fts` 的同步责任在写入口那一个门口）。
    assert [row.id for row in search_semantic(conn, "测试命令")] == [rows[0].id]
    conn.close()


def test_源码级_提升路径没有直写旧表() -> None:
    """源码级证据：归档层不再自己判据、不再直写旧表，提升条目一律走写入口。"""
    src = ARCHIVE_SRC.read_text(encoding="utf-8")
    # 判据只有一份（`gate.judge`），归档层不另写一套、也不预判一次。
    assert "judge(" not in src
    # 提升条目只有一处写点。
    assert src.count("write_memory(") == 1
    # 旧 `memory` 表只剩两个已定案的落点：`memory.kind='episode'` 的降级/下沉
    # （都在 `_episode_sink` 里）与 `state.excluded` 的失败记忆。
    assert src.count("add_memory(") == 2
    assert "insert_memory" not in src
    # 教训仍要进教训库（`match_lessons` 读的是旧表，那是**读路径**，不许破坏）。
    assert src.count("record_lesson(") == 1
    # 五道门禁只有一处写入口：本模块从 `semantic` 只拿这三样。
    assert "insert_semantic" not in src
    assert "INSERT INTO semantic" not in src


def test_提升条目不再直写旧memory表(tmp_path: Path, monkeypatch) -> None:
    """行为级证据：把旧表唯一的写入口 `store.insert_memory` 换成计数器。

    提升一条 fact 时它**一次都不许被调用**；提升一条 lesson 时只允许教训库那一处
    调用（P0 的教训推送池读的是旧表，迁移它不在本次接线面上）。

    两个模块各持一份 `insert_memory` 引用（`lessons.py` 直接 `from ... import`），
    所以两处都要换成计数器，否则"教训库也走旧表"这半句会假绿。
    """
    conn = _conn(tmp_path)
    calls: list[tuple] = []
    real = store.insert_memory

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "insert_memory", spy)
    monkeypatch.setattr(lessons, "insert_memory", spy)

    _archive(conn, tmp_path, [("fact", "测试命令是 pytest -q", "", EVIDENCE)])
    assert calls == []
    assert len(list_semantic(conn)) == 1
    assert conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 0

    _archive(
        conn,
        tmp_path,
        [("lesson", "解析自定义格式先上语法约束", "解析,正则", EVIDENCE)],
    )
    # 教训那一条：只有教训库（P0 落点）写了一次旧表，`semantic` 那边由写入口写。
    assert len(calls) == 1
    assert [row.kind for row in list_semantic(conn)].count("lesson") == 1
    assert [
        row[0] for row in conn.execute("SELECT kind FROM memory")
    ] == ["lesson"]
    conn.close()


# ---- 2. J2 在生产链路里生效：缺证据 → 拒 + 降级为 episode ------------------


def test_缺证据的条目被拒并降级为episode(tmp_path: Path) -> None:
    """`evidence=""`（有这一列但给不出）→ 拒；**降级**而不是丢；原因三处可查。"""
    conn = _conn(tmp_path)
    text = "构建命令是 uv build"
    result = _archive(conn, tmp_path, [("fact", text, "", "")])

    # ① 没进 `semantic`（J2 拒收）。
    assert list_semantic(conn) == []
    # ② 不是静默丢：降级为 `memory.kind='episode'`，结论仍查得到。
    assert [row.text for row in list_memories(conn, kind="episode")] == [text]
    # ③ 原因可读，且三处都在：报告 / 账本 / 审计。
    outcome = result.outcomes[0]
    assert outcome.written is False and outcome.skipped == "denied"
    assert outcome.reason.startswith("J2") and "缺出处" in outcome.reason
    assert result.downgraded == [(text, outcome.reason)]
    assert _ledger(conn) == [("distill", 0, "denied", outcome.reason)]
    assert _audit(conn)[-1][1] == "add" and _audit(conn)[-1][3].startswith("J2")
    # ④ 没静默变成 fact：热记忆文件里没有它。
    assert "fact" not in read_hot(tmp_path / "memory.md")
    conn.close()


def test_蒸馏到归档的真实链路_J2生效(tmp_path: Path) -> None:
    """把 `loop.py::_archive` 的调用链照搬一遍：`distill` 的 entries 交给归档。

    两条 fact：一条模型给了 `evidence`，一条没给。给得出的进 `semantic`，
    给不出的降级为 episode——这是"门禁在生产里真的生效"的那条端到端证据。
    """
    conn = _conn(tmp_path)
    model = FakeModel(
        script=[
            json.dumps(
                {
                    "entries": [
                        {"kind": "fact", "text": "构建命令是 uv build"},
                        {
                            "kind": "fact",
                            "text": "测试命令是 pytest -q",
                            "evidence": "pyproject.toml:1",
                        },
                    ]
                },
                ensure_ascii=False,
            )
        ],
        tokenizer=OfflineTokenCounter(),
    )
    produced = distill(model, _state())
    assert len(produced.entries) == 2

    result = _archive(conn, tmp_path, list(produced.entries))

    assert result.written == ["fact:1"]
    assert [row.text for row in list_semantic(conn)] == ["测试命令是 pytest -q"]
    assert [row.text for row in list_memories(conn, kind="episode")] == [
        "构建命令是 uv build"
    ]
    assert len(result.downgraded) == 1
    assert result.downgraded[0][1].startswith("J2")
    conn.close()


# ---- 3. 幂等：一轮一批，一条一行账本 ---------------------------------------


def test_一批多条各占一行账本(tmp_path: Path) -> None:
    """一轮蒸馏出多条时，每条一个轮次键——共用一个键 = 第 2 条起被静默挡掉。"""
    conn = _conn(tmp_path)
    result = _archive(
        conn,
        tmp_path,
        [
            ("fact", "事实甲是构建命令", "", "a.py:1"),
            ("fact", "事实乙是测试命令", "", "a.py:2"),
        ],
    )
    assert result.written == ["fact:1", "fact:2"]
    rows = conn.execute(
        "SELECT round_key, source, written FROM memory_write ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert len({row["round_key"] for row in rows}) == 2
    assert all(row["source"] == "distill" and row["written"] == 1 for row in rows)
    conn.close()


def test_同一轮重跑由账本挡住(tmp_path: Path, monkeypatch) -> None:
    """绕开 P0 那一层判重（把三层既有指纹当成空的）：挡第二遍的是账本唯一索引。"""
    conn = _conn(tmp_path)
    promote = [("fact", "测试命令是 pytest -q", "", EVIDENCE)]
    first = _archive(conn, tmp_path, promote)
    assert first.written == ["fact:1"]

    monkeypatch.setattr(archive, "_hot_hashes", lambda *args, **kwargs: {})
    monkeypatch.setattr(archive, "_legacy_hashes", lambda *args, **kwargs: {})
    monkeypatch.setattr(archive, "_semantic_hashes", lambda *args, **kwargs: {})
    second = _archive(conn, tmp_path, promote)

    assert second.written == []
    assert second.outcomes[0].skipped == "duplicate"
    assert "同一轮" in second.outcomes[0].reason
    # 没新增第二条，热记忆文件里也没多出一行。
    assert len(list_semantic(conn)) == 1
    assert read_hot(tmp_path / "memory.md")["fact"] == ["测试命令是 pytest -q"]
    conn.close()


def test_热记忆命中判重不误touch旧表的行(tmp_path: Path) -> None:
    """热记忆命中只报判重，**不许**拿 list 下标去 touch 旧表的行。

    P0 的 `known` 是「指纹 → 下标/行 id」一个 int：热记忆里第 2 条命中时，
    `touch_memory(conn, 1)` 会碰到 id=1 那条**无关**的旧行。这里把这条钉死。
    """
    conn = _conn(tmp_path)
    unrelated = add_memory(conn, kind="failure", text="整文件重解析（太慢）")
    assert unrelated.id == 1
    write_hot(tmp_path / "memory.md", {"fact": ["甲", "测试命令是 pytest -q"]})

    result = _archive(conn, tmp_path, [("fact", "测试命令是 pytest -q", "", EVIDENCE)])

    assert result.duplicates == ["测试命令是 pytest -q"]
    assert result.outcomes[0].duplicate_of == "hot:fact"
    row = conn.execute(
        "SELECT use_count FROM memory WHERE id = ?", (unrelated.id,)
    ).fetchone()
    assert row["use_count"] == 0
    assert conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 1
    conn.close()


# ---- 4. 可读结论：长度线、脱敏、无分节的 kind -------------------------------


def test_太长条目仍过门禁_只是不进热记忆(tmp_path: Path) -> None:
    """260 字：过 J4（400）但超 `hot_item_chars`（200）——进 `semantic`，不进热记忆。

    P0 这一条是"不落库"；接线之后它过了五道门禁，所以**落在 `semantic` 里**
    （可检索），只是不进每轮注入的热记忆文件。热记忆层的去向另记一行审计，
    否则"账本说收下、文件里没有"这段差就只在报告里。
    """
    conn = _conn(tmp_path)
    long_entry = "这是一条超过两百字的条目。" * 20
    assert len(long_entry) == 260
    result = _archive(conn, tmp_path, [("fact", long_entry, "", EVIDENCE)])

    assert [row.text for row in list_semantic(conn)] == [long_entry]
    assert "fact" not in read_hot(tmp_path / "memory.md")
    outcome = result.outcomes[0]
    assert outcome.written is True and outcome.skipped == "too_long"
    assert result.downgraded[0][1].startswith("too_long")
    assert [row[1] for row in _audit(conn)] == ["add", "demote"]
    assert _audit(conn)[-1][3].startswith("too_long")
    conn.close()


def test_脱敏的值不进semantic与semantic_fts(tmp_path: Path) -> None:
    """写入侧过闸：密钥的值不进 `semantic` / `semantic_fts` / 旧表 / 热记忆 / 审计。"""
    conn = _conn(tmp_path)
    sk = "sk-live-abcdefghijklmnop"
    value = "abcdef123456"
    fact = f"部署时带上 {sk} 就够了"
    result = _archive(
        conn,
        tmp_path,
        [("fact", fact, "", "deploy.md:3")],
        final=f"请求头用 Authorization: Bearer {value}",
    )

    haystack = "\n".join(
        [row[0] for row in conn.execute("SELECT text FROM semantic")]
        + [row[0] for row in conn.execute("SELECT text FROM semantic_fts")]
        + [row[0] for row in conn.execute("SELECT text FROM memory")]
        + [row[0] for row in conn.execute("SELECT text FROM memory_fts")]
        + [
            f"{row[0]}|{row[1]}"
            for row in conn.execute("SELECT task, summary FROM episode")
        ]
        + [
            " ".join(str(value) for value in row)
            for row in conn.execute("SELECT reason, before, after FROM memory_audit")
        ]
        + [row[0] for row in conn.execute("SELECT detail FROM memory_write")]
        + [(tmp_path / "memory.md").read_text(encoding="utf-8")]
    )
    assert sk not in haystack
    assert value not in haystack
    # 值盖掉了、键名与前缀留着：看得出"这里原本有个密钥"。
    assert "sk-***" in haystack
    assert sk.encode("utf-8") not in (tmp_path / "memory.db").read_bytes()
    assert result.redacted
    conn.close()


def test_热记忆文件没有的分节不进文件但结论不丢(tmp_path: Path) -> None:
    """`skill` 在 `semantic` 里合法，但热记忆文件的 `SECTION_TITLES` 里没有它。

    写进 `sections` 的话 `render_hot` 会**静默不渲染**它，它还会白占 token 预算
    挤掉别的条目。所以这一条要的是：进 `semantic`（检索得到）、不进热记忆文件。
    """
    conn = _conn(tmp_path)
    result = _archive(conn, tmp_path, [("skill", "先 rg 再读文件", "检索", EVIDENCE)])

    assert [row.kind for row in list_semantic(conn)] == ["skill"]
    assert result.written == ["skill:1"]
    assert read_hot(tmp_path / "memory.md") == {}
    conn.close()
