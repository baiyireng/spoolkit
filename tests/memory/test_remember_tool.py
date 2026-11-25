"""`remember` 工具：模型可调用的中途写入口（P1 计划 T10 / 设计稿 §5.2）。

七件事，一件一组：

1. **走门禁**：收下一行走的是 `semantic.write_memory`（账本 `source='tool:remember'`），
   判据、脱敏、去重、幂等、冲突判定**一份都不在工具层**；
2. **五条拒绝路径各给可照做的理由**：判据不过 / 缺出处 / 重复 / 冲突 / 同一轮；
3. **五道门禁在工具路径上同样生效**：脱敏 / 判据 / 幂等 / 判重 / 冲突各一条；
4. **不写热记忆文件**（决议 6：`memory.md` 是注入层的产物，工具只写 `semantic`）；
5. **`scope='machine'` 一律拒绝**并给可读理由，不静默降级成 `workspace`；
6. **注册在同一处**（`cli/runtime.py` 的 `if parts.memory is not None:` 块）：
   挂上记忆就出现在装配出来的工具集里，`--no-memory` 就没有，提示词里也一样；
7. **源码级**：工具只走 `write_memory`，**不许自己直插** `semantic`。

第 7 条与 `test_writer.py` 的源码级用例同一理由：行为用例证明"现在对"，
源码级用例证明"以后多一个直插口就红"。
"""

import re
from pathlib import Path
from typing import Any

import pytest

from spoolkit.agent.loop import SYSTEM_PROMPT, build_workflow
from spoolkit.cli.runtime import LoopWiring, assemble_loop
from spoolkit.config import Config
from spoolkit.context import templates as T
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.semantic import list_semantic
from spoolkit.memory.session import MemorySession
from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.tools import remember_spec
from spoolkit.memory.transcript import USER, record_message
from spoolkit.store.db import open_db
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall, ToolResult, ToolSpec

TOOLS_SRC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "spoolkit"
    / "memory"
    / "tools.py"
)
# 一个能过 J2 的出处（形状是 `file:line`；判据不要求文件真的存在）。
EVIDENCE = "pyproject.toml:1"
# 提示词里必须留住的三类要点：用途（记下）、必填项（必须给出处）、适用场景（跨任务）。
SEMANTIC_ANCHORS = ("remember", "记下", "跨任务", "必须给出处")


def _session(tmp_path: Path, *, session_id: str = "s") -> MemorySession:
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return MemorySession(
        conn=conn,
        hot_path=tmp_path / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=4096,
        session_id=session_id,
    )


def _spec(session: MemorySession, **kwargs: Any) -> ToolSpec:
    return remember_spec(session, overrides={}, **kwargs)


def _call(spec: ToolSpec, **args: Any) -> ToolResult:
    return spec.handler(dict(args))


def _ledger(session: MemorySession) -> list[tuple]:
    return [
        tuple(row)
        for row in session._conn.execute(
            "SELECT round_key, source, written, skipped FROM memory_write"
        )
    ]


# ---- 1/2. 收下与拒绝：一条路径一条用例 ---------------------------------------


def test_收下一条并落库(tmp_path: Path) -> None:
    session = _session(tmp_path)
    record_message(session._conn, "s", USER, "构建怎么跑")

    result = _call(_spec(session), text="构建命令是 pytest -q", evidence=EVIDENCE)

    assert result.ok is True
    assert "已记住" in result.content
    rows = list_semantic(session._conn)
    assert [row.text for row in rows] == ["构建命令是 pytest -q"]
    assert rows[0].kind == "fact"
    assert rows[0].evidence == EVIDENCE
    # 账本一行、来源是工具这条路（门禁真的跑过）。
    ledger = _ledger(session)
    assert len(ledger) == 1
    assert ledger[0][1] == "tool:remember"
    assert ledger[0][2] == 1


def test_判据不过给出可照做的理由(tmp_path: Path) -> None:
    """寒暄（J3）被拒：理由要能直接读，且指明下一步。"""
    session = _session(tmp_path)
    result = _call(
        _spec(session), text="你好！我是你的本地编程助手。", evidence=EVIDENCE
    )

    assert result.ok is False
    assert result.content.startswith("没记住（判据不过）")
    assert "判据不过" in result.content
    assert "J3" in result.content
    assert "下一步" in result.content
    assert list_semantic(session._conn) == []
    assert _ledger(session)[0][3] == "denied"


def test_缺出处给出可照做的理由(tmp_path: Path) -> None:
    """缺出处（J2）被拒：理由的要点是「缺出处」，不是整句。

    J2 是判据里唯一"改一个字段就能过"的那条，所以它单独一个标签，下一步就一句
    「补上出处再写」——标签取自 `judge` 给的理由前缀，不在工具层另判一遍。
    """
    session = _session(tmp_path)
    result = _call(_spec(session), text="构建命令是 uv build", evidence="")

    assert result.ok is False
    assert result.content.startswith("没记住（缺出处）")
    assert "缺出处" in result.content
    assert "file:line" in result.content
    assert list_semantic(session._conn) == []
    assert _ledger(session)[0][3] == "denied"
    # 拒了也要留痕：审计一行（不是静默丢弃）。
    assert (
        session._conn.execute("SELECT count(*) FROM memory_audit").fetchone()[0] == 1
    )


def test_重复给出可照做的理由(tmp_path: Path) -> None:
    """同一条在**另一轮**再写：判重（④）命中，理由是「重复」。"""
    session = _session(tmp_path)
    spec = _spec(session)
    record_message(session._conn, "s", USER, "第一轮")
    first = _call(spec, text="构建命令是 pytest -q", evidence=EVIDENCE)
    # 下一轮：换一条用户消息 → 轮次标识变了 → 落到判重上（不是"同一轮"）。
    record_message(session._conn, "s", USER, "第二轮")
    second = _call(spec, text="构建命令是 pytest -q", evidence=EVIDENCE)

    assert first.ok is True
    assert second.ok is False
    assert second.content.startswith("没记住（重复）")
    assert "重复" in second.content
    assert "同一轮" not in second.content
    assert "下一步" in second.content
    assert len(list_semantic(session._conn)) == 1


def test_同一轮重复写同一条给出同一轮理由(tmp_path: Path) -> None:
    """幂等（③）管**轮次**：同一轮里同一条写两次，第二条被账本挡住。

    同一轮里的**另一条**必须照收——所以末位用的是这一条的内容，不是整轮共用一个键
    （整轮一个键会把第二条静默挡掉，那是丢条目，不是幂等）。
    """
    session = _session(tmp_path)
    spec = _spec(session)
    record_message(session._conn, "s", USER, "这一轮的问题")
    first = _call(spec, text="构建命令是 pytest -q", evidence=EVIDENCE)
    again = _call(spec, text="构建命令是 pytest -q", evidence=EVIDENCE)
    other = _call(spec, text="测试命令是 uv run pytest -q", evidence="pyproject.toml:2")

    assert first.ok is True
    assert again.ok is False
    # 分类标签钉住：「同一轮」与「重复」不是一回事，标签混了就是给出了错的下一步。
    assert again.content.startswith("没记住（同一轮）")
    assert "同一轮" in again.content
    assert "下一步" in again.content
    assert other.ok is True
    assert len(list_semantic(session._conn)) == 2


def test_没有轮次标识时不断言同一轮(tmp_path: Path) -> None:
    """这个会话还没记过用户消息 → 工具**不发明**轮次标识。

    没有轮次就只剩判重那条路：第二次写回的是「重复」，不是「同一轮」——
    「同一轮」是一句关于轮次的断言，没有轮次就不许说。
    """
    session = _session(tmp_path)
    spec = _spec(session)
    first = _call(spec, text="构建命令是 pytest -q", evidence=EVIDENCE)
    second = _call(spec, text="构建命令是 pytest -q", evidence=EVIDENCE)

    assert first.ok is True
    assert second.ok is False
    assert "重复" in second.content
    assert "同一轮" not in second.content


def test_冲突给出可照做的理由(tmp_path: Path) -> None:
    """主题键重叠 → 冲突路径：两条都留、标待复核（不许静默选一个）。

    工具路径没有独立上下文的判定调用（设计稿 §5.3 ⑤ 要求判定由独立上下文给），
    所以默认走的是「两条都 active + needs_review」那一支，理由里说清这点。
    """
    session = _session(tmp_path)
    spec = _spec(session)
    first = _call(spec, text="测试命令是 pytest -q", evidence=EVIDENCE)
    second = _call(
        spec, text="测试命令是 uv run pytest -q", evidence="pyproject.toml:2"
    )

    assert first.ok is True
    assert second.ok is True  # 写下了（不是拒收），但标了待复核
    assert second.content.startswith("已记住（冲突）")
    assert "冲突" in second.content
    assert "needs_review" in second.content
    assert "下一步" in second.content
    assert len(list_semantic(session._conn)) == 2


def test_冲突判定为矛盾时旧行被取代(tmp_path: Path) -> None:
    """判定调用可注入（与 `write_memory(decide=…)` 同一个口子，工具不另判一遍）。"""
    session = _session(tmp_path)
    spec = _spec(session, decide=lambda old, request: "矛盾")
    first = _call(spec, text="测试命令是 pytest -q", evidence=EVIDENCE)
    second = _call(
        spec, text="测试命令是 uv run pytest -q", evidence="pyproject.toml:2"
    )

    assert first.ok is True
    assert second.ok is True
    assert "冲突" in second.content
    rows = {row.id: row.status for row in list_semantic(session._conn)}
    assert rows == {1: "superseded", 2: "active"}


def test_脱敏在工具路径上生效(tmp_path: Path) -> None:
    """密钥形状的值不进库、不进索引，理由里只有形状名。"""
    session = _session(tmp_path)
    secret = "sk-live-abcdefghijklmnop"
    result = _call(
        _spec(session),
        text=f"部署时带上 {secret}，别写进记忆",
        evidence="tests/memory/test_remember_tool.py:1",
    )

    assert result.ok is False
    assert result.content.startswith("没记住（脱敏）")
    assert "脱敏" in result.content
    assert secret not in result.content
    assert list_semantic(session._conn) == []
    assert (
        session._conn.execute("SELECT count(*) FROM semantic_fts").fetchone()[0] == 0
    )
    assert _ledger(session)[0][3] == "redacted"


def test_空文本被拒且理由来自写入口(tmp_path: Path) -> None:
    """工具层不另写判据：空条目的理由就是写入口那句。"""
    session = _session(tmp_path)
    result = _call(_spec(session), text="   ", evidence=EVIDENCE)

    assert result.ok is False
    assert "没有任何内容可记" in result.content
    assert list_semantic(session._conn) == []


def test_非法kind被拒(tmp_path: Path) -> None:
    session = _session(tmp_path)
    result = _call(
        _spec(session), text="构建命令是 pytest -q", evidence=EVIDENCE, kind="gossip"
    )
    assert result.ok is False
    assert "kind 非法" in result.content
    assert list_semantic(session._conn) == []


# ---- 4. 不写热记忆文件 -------------------------------------------------------


def test_已有热记忆文件时一个字节都不改(tmp_path: Path) -> None:
    session = _session(tmp_path)
    hot = tmp_path / "memory.md"
    hot.write_text(
        "# 项目记忆（自动维护，也可手动编辑）\n\n## 结论\n- 已有的热记忆条目\n",
        encoding="utf-8",
    )
    before = hot.read_bytes()
    stamp = hot.stat().st_mtime_ns

    result = _call(_spec(session), text="构建命令是 pytest -q", evidence=EVIDENCE)

    assert result.ok is True
    assert hot.read_bytes() == before
    assert hot.stat().st_mtime_ns == stamp


def test_没有热记忆文件时不会新建(tmp_path: Path) -> None:
    session = _session(tmp_path)
    assert not (tmp_path / "memory.md").exists()
    result = _call(_spec(session), text="构建命令是 pytest -q", evidence=EVIDENCE)

    assert result.ok is True
    assert not (tmp_path / "memory.md").exists()
    # 这条确实进了库：不是"什么都没干"，而是"只写了 semantic"。
    assert len(list_semantic(session._conn)) == 1


# ---- 5. scope='machine' 一律拒绝 ---------------------------------------------


def test_machine级范围被拒且不静默降级(tmp_path: Path) -> None:
    session = _session(tmp_path)
    result = _call(
        _spec(session),
        text="构建命令是 pytest -q",
        evidence=EVIDENCE,
        scope="machine",
    )

    assert result.ok is False
    assert "machine" in result.content
    assert "下一步" in result.content
    # 没有静默当 workspace 收下：库里一条都没有、账本也没有这一笔。
    assert list_semantic(session._conn) == []
    assert _ledger(session) == []


def test_session级写的是会话id_默认是workspace(tmp_path: Path) -> None:
    """`scope_id` 的语义是设计稿定的：session 级时为会话 id，否则空串。"""
    session = _session(tmp_path, session_id="chat-7")
    spec = _spec(session)
    session_scoped = _call(
        spec, text="构建命令是 pytest -q", evidence=EVIDENCE, scope="session"
    )
    default_scoped = _call(
        spec, text="测试命令是 uv run pytest -q", evidence="pyproject.toml:2"
    )

    assert session_scoped.ok is True and default_scoped.ok is True
    rows = list_semantic(session._conn)
    assert (rows[0].scope, rows[0].scope_id) == ("session", "chat-7")
    assert (rows[1].scope, rows[1].scope_id) == ("workspace", "")


# ---- 6. 注册位置与提示词 -----------------------------------------------------


def _assembled(tmp_path: Path, memory: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(tmp_path / "missing.toml"))
    return assemble_loop(
        tmp_path,
        FakeModel(script=[], tokenizer=OfflineTokenCounter()),
        config=Config(project_root=tmp_path, context_window=4096),
        wiring=LoopWiring(memory=memory, mcp=False),
    )


def test_装配出的工具集里有remember(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """与 recall 同一处注册：挂上记忆就有它。"""
    session = _session(tmp_path)
    loop = _assembled(tmp_path, session, monkeypatch)

    assert "remember" in loop.registry.names()
    assert loop.registry.get("remember") is not None
    index = loop.registry.describe()
    assert "remember(text,evidence,kind,trigger,scope)" in index
    assert T.REMEMBER_BRIEF in index
    session._conn.close()


def test_没挂记忆时工具与提示词都没有它(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提到一个没注册的工具，就是给模型一条它做不到的指令。"""
    loop = _assembled(tmp_path, None, monkeypatch)

    assert "remember" not in loop.registry.names()
    prompt = SYSTEM_PROMPT.format(
        tools=loop.registry.describe(), workflow=build_workflow(loop.registry)
    )
    assert "remember" not in prompt
    assert T.REMEMBER_BRIEF not in prompt
    assert T.REMEMBER_DESCRIPTION not in prompt


def test_机械核对_占位符丢0与语义锚点缺0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改模板就必须做的两件机械核对（T10）：占位符、语义锚点。

    占位符：拼出来的系统提示里不许留下 `{名字}` 形态的未替换占位符
    （JSON 里的大括号是双写的，替换后是 `{"thought"…}`，不匹配这个名字形态）。
    """
    session = _session(tmp_path)
    loop = _assembled(tmp_path, session, monkeypatch)
    prompt = SYSTEM_PROMPT.format(
        tools=loop.registry.describe(), workflow=build_workflow(loop.registry)
    )
    missing = re.findall(r"\{[a-z_]+\}", prompt)
    assert missing == [], f"未替换的占位符: {missing}"

    absent = [anchor for anchor in SEMANTIC_ANCHORS if anchor not in prompt]
    assert absent == [], f"语义锚点缺: {absent}"
    session._conn.close()


def test_工具描述说清用途必填与边界(tmp_path: Path) -> None:
    spec = _spec(_session(tmp_path))

    assert spec.name == "remember"
    assert spec.group == "记"
    assert T.REMEMBER_DESCRIPTION == spec.description
    assert spec.brief == T.REMEMBER_BRIEF
    # 两段文案都要留住那三个要点（索引里看 brief，tool_help 里看 description）。
    for anchor in SEMANTIC_ANCHORS[1:]:
        assert anchor in spec.description, f"描述里缺语义锚点: {anchor}"
        assert anchor in spec.brief, f"索引文案里缺语义锚点: {anchor}"
    # 用途 + 必填项 + 边界三句以内（描述过长会吃系统提示的 token）。
    assert len(spec.description) <= 120
    assert spec.parameters["required"] == ["text", "evidence"]
    properties = spec.parameters["properties"]
    assert properties["evidence"]["description"] == (
        "出处：file:line / 命令 / 工具输出摘要"
    )
    assert properties["kind"]["enum"] == [
        "fact",
        "preference",
        "decision",
        "lesson",
        "skill",
    ]
    assert properties["scope"]["enum"] == ["session", "workspace", "machine"]


def test_注册表的参数校验仍然管用(tmp_path: Path) -> None:
    """schema 是闸门：编出来的参数不该进到写入里。"""
    session = _session(tmp_path)
    registry = ToolRegistry()
    registry.register(_spec(session))

    assert registry.invoke(ToolCall("remember", {})).ok is False
    unknown = ToolCall(
        "remember", {"text": "构建命令是 pytest -q", "evidence": EVIDENCE, "nope": 1}
    )
    assert registry.invoke(unknown).ok is False
    bad_kind = ToolCall(
        "remember",
        {"text": "构建命令是 pytest -q", "evidence": EVIDENCE, "kind": "gossip"},
    )
    assert registry.invoke(bad_kind).ok is False
    good = ToolCall(
        "remember", {"text": "构建命令是 pytest -q", "evidence": EVIDENCE}
    )
    assert registry.invoke(good).ok is True


# ---- 7. 源码级：只走写入口 ---------------------------------------------------


def test_工具源码只走write_memory不直插(tmp_path: Path) -> None:
    """行为用例证明"现在对"，这条证明"以后多一个直插口就红"。"""
    source = TOOLS_SRC.read_text(encoding="utf-8")

    assert "write_memory(" in source
    assert "insert_semantic" not in source
    assert "INSERT INTO semantic" not in source
    assert "UPDATE semantic" not in source
    # 热记忆那一路也不许出现在这个模块里：它是注入层的产物（决议 6）。
    assert "write_hot(" not in source
    assert "hot.py" not in source


def test_拒收时账本与审计都留痕(tmp_path: Path) -> None:
    """直插会绕过账本与审计——这两张表有行，是"走了写入口"的直接证据。"""
    session = _session(tmp_path)
    result = _call(
        _spec(session), text="你好！我是你的本地编程助手。", evidence=EVIDENCE
    )

    assert result.ok is False
    ledger = _ledger(session)
    assert len(ledger) == 1
    assert (ledger[0][1], ledger[0][2], ledger[0][3]) == ("tool:remember", 0, "denied")
    assert (
        session._conn.execute("SELECT count(*) FROM memory_audit").fetchone()[0] == 1
    )


def test_轮次键的末位是这一条的内容(tmp_path: Path) -> None:
    """账本键的形态是 `sha1(会话|remember|轮次|这一条的内容)`：

    末位是**这一条的内容**（与 `archive._round_key` 同一形态）——整轮共用一个键，
    同一轮里的第二条会被 `idx_write_round` 当成"重跑"静默挡掉。同一条重写不落第二行
    （抢位撞唯一索引就返回了），所以两张账本行恰好对应那两条不同的内容。
    """
    import hashlib

    session = _session(tmp_path)
    spec = _spec(session)
    record_message(session._conn, "s", USER, "这一轮的问题")
    first = _call(spec, text="构建命令是 pytest -q", evidence=EVIDENCE)
    second = _call(spec, text="测试命令是 uv run pytest -q", evidence="pyproject.toml:2")
    again = _call(spec, text="构建命令是 pytest -q", evidence=EVIDENCE)

    assert (first.ok, second.ok) == (True, True)
    assert again.ok is False and "同一轮" in again.content
    keys = [row[0] for row in _ledger(session)]
    assert len(keys) == 2
    assert keys[0] != keys[1]
    marker = session._conn.execute(
        "SELECT id FROM transcript WHERE session_id = 's' AND role = 'user'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    expected = hashlib.sha1(
        f"s|remember|{marker}|构建命令是 pytest -q".encode("utf-8")
    ).hexdigest()
    assert keys[0] == expected


def test_轮次键跟会话与用户消息走(tmp_path: Path) -> None:
    """换了用户消息（下一轮）→ 键就变了：这就是"同一轮"的判据来源。"""
    session = _session(tmp_path)
    spec = _spec(session)
    record_message(session._conn, "s", USER, "第一轮")
    _call(spec, text="构建命令是 pytest -q", evidence=EVIDENCE)
    record_message(session._conn, "s", USER, "第二轮")
    _call(spec, text="测试命令是 uv run pytest -q", evidence="pyproject.toml:2")

    first, second = [row[0] for row in _ledger(session)]
    assert first != second
