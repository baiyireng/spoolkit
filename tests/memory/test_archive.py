from pathlib import Path

from spoolkit.agent.state import TaskState
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.archive import archive_task
from spoolkit.memory.hot import read_hot, write_hot
from spoolkit.memory.store import add_memory, init_memory_schema, list_memories
from spoolkit.store.db import open_db


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def _state() -> TaskState:
    return TaskState(
        task_id="t1",
        goal="给 parser 加增量更新",
        done=["读 parser.py", "确认索引表结构"],
        current="完成",
        excluded=["整文件重解析（太慢）"],
        step=4,
    )


def test_归档写入一条事件(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
    )
    assert result.episode_id > 0
    rows = conn.execute("SELECT task, outcome FROM episode").fetchall()
    assert rows[0]["task"] == "给 parser 加增量更新"
    assert rows[0]["outcome"] == "success"
    conn.close()


def test_已排除的方案写成失败记忆(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
    )
    failures = list_memories(conn, kind="failure")
    assert len(failures) == 1
    assert "整文件重解析" in failures[0].text
    assert failures[0].source.startswith("ep#")
    conn.close()


def test_任务结束后进行中条目被清空(tmp_path: Path) -> None:
    hot_path = tmp_path / "memory.md"
    write_hot(hot_path, {"doing": ["给 parser 加增量更新"], "fact": ["事实一"]})
    conn = _conn(tmp_path)
    archive_task(
        conn,
        hot_path,
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
    )
    assert "doing" not in read_hot(hot_path)
    assert read_hot(hot_path)["fact"] == ["事实一"]
    conn.close()


def test_提升候选写入热记忆(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", "测试命令是 pytest -q")],
    )
    assert read_hot(tmp_path / "memory.md")["fact"] == ["测试命令是 pytest -q"]
    conn.close()


def test_超出预算的条目下沉到冷记忆(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    # 两条都在 hot_item_chars=200 以内（所以它们进得了热记忆），但 token 预算装不下：
    # 1200 窗口的热预算 = int(1200×0.85×0.15) = 153，而两条渲染后 190。
    # **T7 接线起 `doing` 不再是合法的提升类型**（写入口只认 `semantic` 的五个
    # kind，`doing` 会被判"kind 非法"→ 降级），所以这里用两条 fact 表达同一件事。
    long_entry = "很长的一段记忆内容" * 15
    short_fact = "短" * 80
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=1200,
        promote=[("fact", long_entry), ("fact", short_fact)],
    )
    assert result.demoted
    assert any("很长的一段记忆内容" in m.text for m in list_memories(conn, kind="episode"))
    conn.close()


def test_教训被路由到教训库而不是热记忆文件(tmp_path: Path) -> None:
    from spoolkit.memory.lessons import match_lessons

    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("lesson", "解析自定义格式先上语法约束", "解析,正则,格式")],
    )
    # 不进热记忆文件
    assert "lesson" not in read_hot(tmp_path / "memory.md")
    # 但能被场景触发
    assert len(match_lessons(conn, "修一下解析逻辑")) == 1
    conn.close()


def test_教训保留来源便于回溯(tmp_path: Path) -> None:
    from spoolkit.memory.lessons import match_lessons

    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("lesson", "规则", "解析")],
    )
    hit = match_lessons(conn, "解析")[0]
    assert hit.source.startswith("ep#")
    conn.close()


def test_非教训条目仍然进热记忆(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", "测试命令是 pytest -q")],
    )
    assert read_hot(tmp_path / "memory.md")["fact"] == ["测试命令是 pytest -q"]
    conn.close()


def test_兼容不带触发词的两元组(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", "旧格式的条目")],
    )
    assert result.promoted == ["旧格式的条目"]
    conn.close()


def test_归档结果报告提升与下沉(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", "事实一")],
    )
    assert result.promoted == ["事实一"]
    assert result.demoted == []
    conn.close()


def test_归档摘要带上结论(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        final="厦门近两天约 28°C。",
    )
    row = conn.execute("SELECT summary FROM episode").fetchone()
    assert "28°C" in row["summary"]
    assert "给 parser 加增量更新" in row["summary"]
    conn.close()


def test_没有结论时摘要不含结论行(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
    )
    row = conn.execute("SELECT summary FROM episode").fetchone()
    assert "结论：" not in row["summary"]
    conn.close()


def test_失败任务的事件outcome为失败(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        outcome="fail",
    )
    assert conn.execute("SELECT outcome FROM episode").fetchone()["outcome"] == "fail"
    conn.close()


# ---- 停噪声（设计稿 §3 缺陷 1 / §5.3 ②）：判据不过 = 降级，不是丢弃 ----


def test_寒暄不进热记忆但结论仍能在事件摘要里查到(tmp_path: Path) -> None:
    """被拒的内容必须两个断言同时成立：热记忆里没有它，事件摘要里查得到它。"""
    conn = _conn(tmp_path)
    greeting = "你好！我是你的本地编程助手。"
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", greeting)],
        final=greeting,
    )
    assert "fact" not in read_hot(tmp_path / "memory.md")
    assert result.promoted == []
    assert len(result.downgraded) == 1
    text, reason = result.downgraded[0]
    assert text == greeting
    assert reason.startswith("J3")
    summary = conn.execute("SELECT summary FROM episode").fetchone()["summary"]
    assert f"结论：{greeting}" in summary
    conn.close()


def test_一次性收尾不进热记忆(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    closing = "任务已完成。请确认最终文件状态并宣告完成。"
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", closing)],
    )
    assert "fact" not in read_hot(tmp_path / "memory.md")
    assert result.downgraded[0][1].startswith("J1")
    # 降级出口写进库里，检索得到（不污染事实层，但「发生过」不丢）。
    rows = list_memories(conn, kind="episode")
    assert [row.text for row in rows] == [closing]
    conn.close()


def test_整段答复不进热记忆且理由提到长度(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    long_final = "天气表与交付答复的正文内容。" * 40
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", long_final)],
        final="短结论",
    )
    assert "fact" not in read_hot(tmp_path / "memory.md")
    reason = result.downgraded[0][1]
    assert reason.startswith("J4")
    assert "长度" in reason
    assert "remember_max_chars" in reason
    # 降级不是丢弃：整段正文仍在冷记忆里，检索得到。
    assert [row.text for row in list_memories(conn, kind="episode")] == [long_final]
    conn.close()


def test_长度阈值可用工作区覆盖调低(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        overrides={"remember_max_chars": 10},
        promote=[("fact", "测试命令是 pytest -q")],
    )
    assert "fact" not in read_hot(tmp_path / "memory.md")
    assert result.downgraded[0][1].startswith("J4")
    conn.close()


def test_无触发词的教训被拒且不进教训库(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("lesson", "规则", "")],
    )
    count = conn.execute(
        "SELECT count(*) AS n FROM memory WHERE kind = 'lesson'"
    ).fetchone()["n"]
    assert count == 0
    assert "J5" in result.downgraded[0][1]
    conn.close()


def test_真事实不被判据误杀(tmp_path: Path) -> None:
    """判据过宽是另一种回归：真项目事实一条都不许降级。"""
    conn = _conn(tmp_path)
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", "测试命令是 pytest -q")],
    )
    assert read_hot(tmp_path / "memory.md")["fact"] == ["测试命令是 pytest -q"]
    assert result.downgraded == []
    assert result.demoted == []
    conn.close()


# ---- 写入路径过脱敏（设计稿 §5.3 ① / 缺陷 9）----


def _secret_state() -> TaskState:
    state = _state()
    state.excluded = [f"整文件重解析（太慢），密钥 sk-live-abcdefghijklmnop"]
    return state


def test_写入路径过脱敏_值不进热记忆库与索引(tmp_path: Path) -> None:
    """构造三类密钥形状，走一次真实归档，断言值不出现在任何落点。"""
    conn = _conn(tmp_path)
    sk = "sk-live-abcdefghijklmnop"
    keyed_value = "abcdef123456"
    bearer = "Authorization: Bearer abcdef123456"
    hot_fact = f"部署时带上 {sk} 就够了"
    # 这条会被 J3 拒 → 降级落库（memory + memory_fts）
    rejected = f"你好！我是你的本地编程助手，密钥是 SPOOLKIT_QQ_SECRET={keyed_value}"
    result = archive_task(
        conn,
        tmp_path / "memory.md",
        _secret_state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", hot_fact), ("fact", rejected)],
        final=f"结论：请求头用 {bearer}",
    )

    hot_text = (tmp_path / "memory.md").read_text(encoding="utf-8")
    rows = conn.execute("SELECT text, trigger, source FROM memory").fetchall()
    fts = conn.execute("SELECT text FROM memory_fts").fetchall()
    episode = conn.execute("SELECT task, summary FROM episode").fetchone()
    haystack = "\n".join(
        [hot_text]
        + [f"{row['text']}|{row['trigger']}|{row['source']}" for row in rows]
        + [row["text"] for row in fts]
        + [f"{episode['task']}|{episode['summary']}"]
    )
    assert sk not in haystack
    assert keyed_value not in haystack
    assert bearer not in haystack

    # 键名可以留：看得出「这里原本有个值」。
    assert "SPOOLKIT_QQ_SECRET" in haystack
    assert "Authorization" in haystack

    # 写入侧说出了命中哪一类形状，且理由里不回显值。
    reasons = " ".join(reason for _, reason in result.redacted)
    assert result.redacted
    assert "密钥形状" in reasons
    assert sk not in reasons and keyed_value not in reasons
    conn.close()


def test_既有热记忆里的密钥在重写时也被盖掉(tmp_path: Path) -> None:
    """热记忆是整份重写的：写入侧不过闸，等于把文件里的旧密钥又抄回去。"""
    hot_path = tmp_path / "memory.md"
    write_hot(hot_path, {"fact": ["部署命令是 TOOL_TOKEN=abcdef123456"]})
    conn = _conn(tmp_path)
    archive_task(
        conn,
        hot_path,
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        promote=[("fact", "测试命令是 pytest -q")],
    )
    hot_text = hot_path.read_text(encoding="utf-8")
    assert "abcdef123456" not in hot_text
    assert "TOOL_TOKEN" in hot_text
    conn.close()


# ---- 跨任务/跨蒸馏的结构性去重（设计稿 §3 缺陷 2 / §5.3 ④ 第 1 级）----
#
# 现状病根是 `archive.py` 的落点无条件 append：`distill._merge` 只管单次
# 蒸馏分治内的合并，跨任务/跨蒸馏没有任何判重，于是同一句寒暄能存三条。
# 这一组用例钉的是「同一事实只落一次 + 报告里说出被去重几条」。


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


def test_跨任务同一事实不再二次入库(tmp_path: Path) -> None:
    """连续两次归档同一事实：热记忆只有一条，第二次一条都没新增。"""
    conn = _conn(tmp_path)
    fact = "测试命令是 pytest -q"
    first = _archive(conn, tmp_path, [("fact", fact)])
    assert first.promoted == [fact]
    assert first.duplicates == []

    second = _archive(conn, tmp_path, [("fact", fact)])
    assert read_hot(tmp_path / "memory.md")["fact"] == [fact]
    assert second.promoted == []
    assert second.duplicates == [fact]
    conn.close()


def test_同一批里重复条目只留一条(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    fact = "测试命令是 pytest -q"
    result = _archive(conn, tmp_path, [("fact", fact), ("fact", fact)])
    assert read_hot(tmp_path / "memory.md")["fact"] == [fact]
    assert result.promoted == [fact]
    assert result.duplicates == [fact]
    conn.close()


def test_规范化后相等即算同一条(tmp_path: Path) -> None:
    """「测试命令是 pytest -q」与「测试命令是 pytest -q。」是同一条。"""
    conn = _conn(tmp_path)
    first = _archive(conn, tmp_path, [("fact", "测试命令是 pytest -q")])
    assert first.promoted == ["测试命令是 pytest -q"]
    second = _archive(conn, tmp_path, [("fact", "测试命令是 pytest -q。")])
    assert read_hot(tmp_path / "memory.md")["fact"] == ["测试命令是 pytest -q"]
    assert second.duplicates == ["测试命令是 pytest -q。"]

    # 大小写/空白/全角半角同属一个口径。
    third = _archive(conn, tmp_path, [("fact", "  测试命令是  PYTEST - Q ")])
    assert third.promoted == []
    conn.close()


def test_库里已有同一事实时只记一次使用(tmp_path: Path) -> None:
    """既有行在库里（不在热记忆里）也要判重：命中就 touch，不新增行。"""
    conn = _conn(tmp_path)
    fact = "测试命令是 pytest -q"
    existing = add_memory(conn, kind="fact", text=fact)
    result = _archive(conn, tmp_path, [("fact", fact)])
    assert result.promoted == []
    assert result.duplicates == [fact]
    assert "fact" not in read_hot(tmp_path / "memory.md")
    row = conn.execute(
        "SELECT use_count FROM memory WHERE id = ?", (existing.id,)
    ).fetchone()
    assert row["use_count"] == 1
    # 不新增行：库里的条数仍然是「已排除的失败记忆 1 条 + 既有那条事实 1 条」。
    assert conn.execute("SELECT count(*) AS n FROM memory").fetchone()["n"] == 2
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM memory WHERE kind = 'fact' AND text = ?",
            (fact,),
        ).fetchone()["n"]
        == 1
    )
    conn.close()


def test_降级下沉的落点也判重(tmp_path: Path) -> None:
    """被拒条目落进 memory.kind='episode' 的那条路也要判重，否则照样三份。"""
    conn = _conn(tmp_path)
    rejected = "任务已完成。请确认最终文件状态并宣告完成。"
    first = _archive(conn, tmp_path, [("fact", rejected)])
    assert first.downgraded and first.duplicates == []
    second = _archive(conn, tmp_path, [("fact", rejected)])
    assert second.downgraded == []
    assert second.duplicates == [rejected]
    count = conn.execute(
        "SELECT count(*) AS n FROM memory WHERE kind = 'episode' AND text = ?",
        (rejected,),
    ).fetchone()["n"]
    assert count == 1
    # 去重不是把降级也吞掉：这条被拒条目在库里仍然只有一行。
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM memory WHERE text = ?", (rejected,)
        ).fetchone()["n"]
        == 1
    )
    conn.close()


def test_三条逐字相同只落一条并报出去重条数(tmp_path: Path) -> None:
    """`duplicates` 的条数就是被去重的条数：实测病灶就是三条逐字相同的寒暄。"""
    conn = _conn(tmp_path)
    fact = "测试命令是 pytest -q"
    result = _archive(
        conn, tmp_path, [("fact", fact), ("fact", fact), ("fact", fact)]
    )
    assert len(result.duplicates) == 2
    assert read_hot(tmp_path / "memory.md")["fact"] == [fact]
    conn.close()


def test_去重口径可被工作区覆盖(tmp_path: Path) -> None:
    """关掉去标点：两条只差标点的条目就不再是同一条。"""
    conn = _conn(tmp_path)
    overrides = {"dedup_strip_punct": 0}
    _archive(conn, tmp_path, [("fact", "测试命令是 pytest -q")], overrides=overrides)
    second = _archive(
        conn, tmp_path, [("fact", "测试命令是 pytest -q。")], overrides=overrides
    )
    assert second.duplicates == []
    assert read_hot(tmp_path / "memory.md")["fact"] == [
        "测试命令是 pytest -q",
        "测试命令是 pytest -q。",
    ]
    conn.close()


# ---- 单条长度线 hot_item_chars（设计稿 §4.3 容量口径 / §7.1）----


def test_超长条目不进热记忆(tmp_path: Path) -> None:
    """201 字按 hot_item_chars=200 不收：理由含 too_long，内容不丢。"""
    conn = _conn(tmp_path)
    long_entry = "这是一条超过两百字的条目。" * 20
    assert len(long_entry) == 260
    result = _archive(
        conn, tmp_path, [("fact", long_entry)], final=f"结论：{long_entry[:50]}"
    )
    assert "fact" not in read_hot(tmp_path / "memory.md")
    assert result.promoted == []
    assert len(result.downgraded) == 1
    text, reason = result.downgraded[0]
    assert text == long_entry
    assert reason.startswith("too_long")
    assert "hot_item_chars" in reason
    # 不丢：结论仍在事件摘要里，可以核对。
    summary = conn.execute("SELECT summary FROM episode").fetchone()["summary"]
    assert long_entry[:50] in summary
    conn.close()


def test_恰好两百字的条目仍进热记忆(tmp_path: Path) -> None:
    """钉住边界方向：200 是「超过才不收」，等号那条仍然收。"""
    conn = _conn(tmp_path)
    entry = "热记忆的单条长度线是两百字。" * 15
    assert len(entry) == 210
    ok = entry[:200]
    assert len(ok) == 200
    result = _archive(conn, tmp_path, [("fact", ok)])
    assert read_hot(tmp_path / "memory.md")["fact"] == [ok]
    assert result.downgraded == []
    conn.close()


def test_单条长度线可被工作区覆盖(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    entry = "短条目也要能按工作区调线。"
    result = _archive(
        conn,
        tmp_path,
        [("fact", entry)],
        overrides={"remember_max_chars": 400, "hot_item_chars": 5},
    )
    assert "fact" not in read_hot(tmp_path / "memory.md")
    assert result.downgraded[0][1].startswith("too_long")
    conn.close()
