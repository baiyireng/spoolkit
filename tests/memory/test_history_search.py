"""按查询检索历史消息（`search_messages`）。

这一层的行为是「历史不进上下文，但要能按需取回来」，所以用例盯三件事：
**跨会话**能不能取到、**出处**够不够追溯、**边界**（范围/数量/转义/脱敏）
是不是说清楚了。
"""

from pathlib import Path

from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.transcript import (
    ASSISTANT,
    USER,
    no_hits_text,
    parse_moment,
    record_message,
    redact_secrets,
    render_history_hits,
    search_messages,
)
from spoolkit.store.db import open_db

LIMIT = 5
SNIPPET = 240


def _conn(tmp_path: Path, name: str = "memory.db"):
    conn = open_db(tmp_path / name)
    init_memory_schema(conn)
    return conn


def _workspace(tmp_path: Path, name: str):
    """一个独立工作区的记忆库（`.agent/memory.db`）。"""
    root = tmp_path / name
    (root / ".agent").mkdir(parents=True)
    return _conn(root / ".agent", "memory.db")


def _search(conn, query: str, **kwargs):
    kwargs.setdefault("limit", LIMIT)
    kwargs.setdefault("snippet_chars", SNIPPET)
    return search_messages(conn, query, **kwargs)


def _at(conn, message_id: int, stamp: float) -> None:
    """把一条消息的时间改成指定值：时间范围用例不能靠"跑得快"。"""
    conn.execute("UPDATE transcript SET created_at = ? WHERE id = ?", (stamp, message_id))
    conn.commit()


def test_命中另一个会话里的历史(tmp_path: Path) -> None:
    """「这个问题是从哪来的」——答案常常写在**别的**会话里。"""
    conn = _conn(tmp_path)
    record_message(conn, "旧的排查", USER, "先看看归档会不会丢东西")
    record_message(conn, "现在", USER, "继续刚才那件事")
    hits = _search(conn, "归档")
    assert len(hits) == 1
    assert hits[0].session_id == "旧的排查"
    conn.close()


def test_默认跨会话(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "a", USER, "归档要加锁")
    record_message(conn, "b", ASSISTANT, "归档已经加锁了")
    sessions = {hit.session_id for hit in _search(conn, "归档")}
    assert sessions == {"a", "b"}
    conn.close()


def test_只看一条会话(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "a", USER, "归档要加锁")
    record_message(conn, "b", USER, "归档已经加锁了")
    hits = _search(conn, "归档", session="a")
    assert [hit.session_id for hit in hits] == ["a"]
    conn.close()


def test_出处包含会话角色时间与命中位置(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "alpha", USER, "归档那条命令为什么这么写")
    hit = _search(conn, "归档")[0]
    text = render_history_hits(_search(conn, "归档"), {"alpha": "修 calc.py"})
    assert "alpha" in text
    assert "修 calc.py" in text
    assert "你" in text  # 角色：人看得懂的那种写法，不是 "user"
    assert "#" in text and "命中第" in text
    assert hit.created_at > 0
    # 命中位置对得上：片段里 offset 处就是那个词。
    assert hit.snippet[hit.offset : hit.offset + 2] == "归档"
    conn.close()


def test_时间方向是新到旧(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    old = record_message(conn, "s", USER, "归档第一次")
    new = record_message(conn, "s", USER, "归档第二次")
    _at(conn, old, parse_moment("2026-09-01 10:00", label="t"))
    _at(conn, new, parse_moment("2026-09-20 10:00", label="t"))
    text = render_history_hits(_search(conn, "归档"))
    assert text.index("第二次") < text.index("第一次")
    assert text.index("09-01") > 0 and text.index("09-20") > 0
    conn.close()


def test_没命中要说清在哪个范围里没找到(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "无关的话")
    assert _search(conn, "完全无关的词") == []
    said = no_hits_text("完全无关的词")
    assert "没有找到" in said
    assert "全部会话" in said and "工作区" in said
    assert "完全无关的词" in said
    conn.close()


def test_空查询返回空(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "有内容")
    assert _search(conn, "   ") == []
    conn.close()


def test_数量受上限约束(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for index in range(8):
        record_message(conn, "s", USER, f"归档任务{index}")
    assert len(_search(conn, "归档", limit=3)) == 3
    conn.close()


def test_命中词多的排前面(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "归档只是顺带一提")
    record_message(conn, "s", USER, "归档与命令两件事")
    hits = _search(conn, "归档 命令")
    assert "命令" in hits[0].snippet
    assert hits[0].matched == ("归档", "命令")
    conn.close()


def test_长消息只给命中附近的片段(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "前缀" * 300 + "归档在这里" + "后缀" * 300)
    hit = _search(conn, "归档", snippet_chars=60)[0]
    assert len(hit.snippet) <= 62  # 60 + 两端各一个省略号
    assert "归档在这里" in hit.snippet
    assert "前缀" * 300 not in hit.snippet
    conn.close()


def test_时间范围能收窄(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    early = record_message(conn, "s", USER, "归档早期")
    late = record_message(conn, "s", USER, "归档晚期")
    _at(conn, early, parse_moment("2026-09-01 10:00", label="t"))
    _at(conn, late, parse_moment("2026-09-20 10:00", label="t"))
    only_late = _search(conn, "归档", since=parse_moment("2026-09-10", label="t"))
    assert [hit.id for hit in only_late] == [late]
    only_early = _search(conn, "归档", until=parse_moment("2026-09-10", label="t"))
    assert [hit.id for hit in only_early] == [early]
    conn.close()


def test_时间格式认不出就报错不猜(tmp_path: Path) -> None:
    try:
        parse_moment("前天", label="since")
    except ValueError as exc:
        assert "since" in str(exc) and "前天" in str(exc)
    else:  # pragma: no cover - 走到这里说明它猜了
        raise AssertionError("认不出的时间必须报错")


def test_通配符按普通字符处理(tmp_path: Path) -> None:
    """`%` / `_` 是查询里的普通字符：不转义的话 `%` 会命中所有消息。"""
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "普通的归档说明")
    record_message(conn, "s", USER, "折扣是 50% 那次")
    assert [hit.snippet for hit in _search(conn, "%")] == ["折扣是 50% 那次"]
    assert _search(conn, "_") == []
    conn.close()


def test_工作区之间的历史互不可见(tmp_path: Path) -> None:
    """默认作用域就是**当前工作区**：A 的检索拿不到 B 的历史。"""
    left = _workspace(tmp_path, "left")
    right = _workspace(tmp_path, "right")
    record_message(left, "s", USER, "左边的归档问题")
    record_message(right, "s", USER, "右边的归档问题")
    assert [hit.snippet for hit in _search(left, "归档")] == ["左边的归档问题"]
    assert [hit.snippet for hit in _search(right, "归档")] == ["右边的归档问题"]
    left.close()
    right.close()


def test_密钥形状的内容不进检索结果(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(
        conn,
        "s",
        USER,
        "配置是 SPOOLKIT_QQ_SECRET=abcdef123456，key 用 sk-live-abcdefghijkl 这个",
    )
    snippet = _search(conn, "配置")[0].snippet
    assert "abcdef123456" not in snippet
    assert "sk-live-abcdefghijkl" not in snippet
    assert "SPOOLKIT_QQ_SECRET" in snippet  # 键名留着：看得出这里原本有个值
    conn.close()


def test_脱敏覆盖几种常见形状() -> None:
    assert "sk-***" in redact_secrets("令牌 sk-abcdefghijkl 到期")
    assert "AIza***" in redact_secrets("AIzaSyABCdefghijklmnopqrstuvwxyz0123")
    assert "abcdef123456" not in redact_secrets("WECOM_SECRET: abcdef123456")
    assert "abcdef123456" not in redact_secrets("Authorization: Bearer abcdef123456")
    assert redact_secrets("普通的一句话") == "普通的一句话"


def test_脱敏只盖值不动后面的句子() -> None:
    """值后面紧跟中文时，别把下一句话也当成值吞掉——那是删上下文，不是脱敏。"""
    said = redact_secrets("SPOOLKIT_QQ_SECRET=abcdef123456，先放着")
    assert "abcdef123456" not in said
    assert "先放着" in said


def test_检索不写库(tmp_path: Path) -> None:
    """纯读：检索不改消息、不建索引——历史是给人看的原文，不该被检索动过。"""
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "归档要加锁")
    before = conn.execute("SELECT * FROM transcript").fetchall()[0]
    _search(conn, "归档")
    after = conn.execute("SELECT * FROM transcript").fetchall()[0]
    assert dict(before) == dict(after)
    conn.close()
