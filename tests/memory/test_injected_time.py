"""注入到上下文里的时间：**同一份内容重跑必须逐字相同**。

现场（35 次采样里抓到的第一条缺陷）：同一份冻结代码上两次 t=0 的 pass，
`merge-html` / `vague-optimize` / `rename-cross-file` 的事件流逐字节相同，
而 `add-feature`（F11EFFBD vs 6FD9C64A）与 `fix-bug` 不同。分叉点找到了：
`search_history` 的输出里带着 `[MM-DD HH:MM]` 的**实际时刻**，两次 pass 相差
6 分钟，贪心解码把它放大成 28 行 / 62 行两种轨迹。

判据写成一句话：**同一份内容在不同实际时刻重跑，进提示的那段文字逐字相同**。
落库/审计/给人看的日志不受影响——**漂的是提示，不是记录**（库里
`transcript.created_at` 一个字节没动，聊天区域与 CLI 检索照旧精确到分钟）。

所以这里量三件事：

1. `prompt_stamp` 的形状里**没有分钟级时钟**（有的话，跑得慢一点就漂）；
2. 用**可注入的运行起点**把「两次相隔 6 分钟的运行」真的构造出来（库里的
   内容一字不差、实际时刻整体后移 360 秒），注入文本逐字相同；
   反向对照：给人看的那一份**确实差了 6 分钟**——否则上面那个相等只是
   因为夹具没动过时间，测不出任何东西；
3. `dispatch_history` 那份同样会进上下文的战绩，注入时也不带实际时刻。
"""

from __future__ import annotations

import re
from pathlib import Path

from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.session import MemorySession
from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.tools import search_history_spec
from spoolkit.memory.transcript import (
    USER,
    clear_run_origin,
    prompt_stamp,
    record_message,
    render_history_hits,
    render_transcript,
    resolve_scope,
    run_origin,
    search_messages,
    set_run_origin,
)
from spoolkit.store.db import open_db
from spoolkit.tools.dispatch import dispatch_history, record_dispatch

#: 夹具的绝对基准：不依赖「跑得快」，两次运行的时刻差是写死的。
BASE = 1_800_000_000.0
#: 两条 pass 之间实际过去了 6 分钟（现场那次的读数）。
SIX_MINUTES = 360.0
#: 分钟级的实际时刻。它**不许**出现在进提示的那一段里。
MINUTE_CLOCK = re.compile(r"\d{2}-\d{2} \d{2}:\d{2}")


def _root(parent: Path) -> Path:
    root = parent / "ws"
    (root / ".agent").mkdir(parents=True)
    return root


def _spec(root: Path):
    conn = open_db(root / ".agent" / "memory.db")
    init_memory_schema(conn)
    session = MemorySession(
        conn=conn,
        hot_path=root / ".agent" / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=8192,
    )
    spec = search_history_spec(
        session, root, default_limit=5, max_limit=20, snippet_chars=240
    )
    return spec, session, conn


def _fill(parent: Path, *, origin: float):
    """建一个工作区、灌入固定内容；内容相对**运行起点**的位置是写死的。

    这正是现场那两条 pass 的形状：第二次跑发生在 6 分钟之后，沙箱落地、
    消息落库、运行起点**整体后移**了 360 秒，而「每条消息距起点多久」一字
    不差。所以要构造第二次，只需要把 `origin` 移过去。
    """
    spec, session, conn = _spec(parent)
    for index, offset in enumerate(
        (
            2 * 86400,  # 两天前的那条：走「N 天前」那一支
            3600,  # 一小时前的那条
        )
    ):
        message_id = record_message(conn, "default", USER, f"归档第{index}件事")
        conn.execute(
            "UPDATE transcript SET created_at = ? WHERE id = ?",
            (origin - offset, message_id),
        )
    conn.commit()
    return spec, session, conn


def _render(conn, spec, *, exact: bool = False) -> str:
    """按当前标定的起点渲染一次（标定/清理由调用方负责）。"""
    if exact:
        # 给人看的那一条路（CLI 的 `--search`）：同一批命中，精确到分钟。
        hits = search_messages(
            conn, "归档", limit=5, snippet_chars=240, session="default"
        )
        return render_history_hits(
            hits, None, scope_said="当前会话 default", exact_time=True
        )
    result = spec.handler({"query": "归档"})
    assert result.ok is True
    return result.content


def _inject(parent: Path, *, origin: float, exact: bool = False) -> str:
    """一次运行的注入文本（`origin` 就是这次运行的时间起点）。"""
    spec, _session, conn = _fill(parent, origin=origin)
    set_run_origin(origin)
    try:
        return _render(conn, spec, exact=exact)
    finally:
        clear_run_origin()
        conn.close()


# ---- 1. 形状：没有分钟级时钟 ------------------------------------------------


def test_进提示的时间戳不带分钟级时钟() -> None:
    """有分钟级时钟的话，两次跑差几分钟就是两段不同的提示。"""
    set_run_origin(BASE)
    try:
        assert prompt_stamp(BASE - 60) == "今天"
        assert prompt_stamp(BASE - 86400) == "1 天前"
        assert prompt_stamp(BASE - 3 * 86400) == "3 天前"
        assert not MINUTE_CLOCK.search(prompt_stamp(BASE - 60))
    finally:
        clear_run_origin()


def test_没有标定起点时退回只到日期() -> None:
    """不在一次 agent 运行里的调用方（脚本直接查库）也不许拿到分钟级时刻。"""
    clear_run_origin()
    assert MINUTE_CLOCK.search(prompt_stamp(BASE)) is None
    assert prompt_stamp(BASE) == prompt_stamp(BASE + 1)


def test_起点标定读到的是标定值() -> None:
    assert run_origin() is None
    assert set_run_origin(BASE) == BASE
    assert run_origin() == BASE
    clear_run_origin()
    assert run_origin() is None


# ---- 2. 现场：相隔 6 分钟的两次运行，注入文本逐字相同 ------------------------


def test_相隔六分钟的两次运行注入文本逐字相同(tmp_path: Path) -> None:
    """这一条就是那条分叉的回归：`search_history` 的输出必须一模一样。"""
    first = _inject(tmp_path / "a", origin=BASE)
    second = _inject(tmp_path / "b", origin=BASE + SIX_MINUTES)

    assert "归档第0件事" in first and "归档第1件事" in first
    assert "2 天前" in first
    assert not MINUTE_CLOCK.search(first), f"注入文本里还有分钟级时刻：{first}"
    assert first == second, "同一份内容、相隔 6 分钟的两次运行给出了不同的注入文本"


def test_对照_给人看的那一份确实差了六分钟(tmp_path: Path) -> None:
    """反向对照：夹具真的把时间移了 6 分钟。

    没有这一条，上面那个「逐字相同」可能只是因为两次的库内容与时刻一模一样
    ——那就什么都没测出来。给人看的那一份（`exact_time=True`）必须**看得出**
    这 6 分钟，才证明确实有东西被归一化掉了。
    """
    first = _inject(tmp_path / "a", origin=BASE, exact=True)
    second = _inject(tmp_path / "b", origin=BASE + SIX_MINUTES, exact=True)

    assert MINUTE_CLOCK.search(first), "给人看的那一份应当保留精确到分钟的时刻"
    assert first != second, "夹具没有真的移动时间：两次的精确时刻应当相差 6 分钟"


def test_进提示的渲染不看挂钟(tmp_path: Path, monkeypatch) -> None:
    """注入文本的输入**只有**运行起点与被渲染的时刻，没有「现在几点」。

    把挂钟换成会炸的家伙，渲染照样要跑得完——这是「同一次运行内稳定」最硬的
    证据：它根本没读那个会漂的值。夹具灌库要用到挂钟，所以先灌、后换。
    """
    import spoolkit.memory.transcript as transcript

    spec, _session, conn = _fill(tmp_path / "a", origin=BASE)
    set_run_origin(BASE)
    try:
        def boom():  # pragma: no cover - 被调到就是失败
            raise AssertionError("进提示的渲染不该读挂钟")

        monkeypatch.setattr(transcript.time, "time", boom)
        first = _render(conn, spec)
        second = _render(conn, spec)
    finally:
        clear_run_origin()
        conn.close()
    assert first == second
    assert "归档第0件事" in first


# ---- 3. 记录侧不动：聊天区域照旧精确到分钟 ----------------------------------


def test_聊天区域照旧保留精确时刻(tmp_path: Path) -> None:
    """漂的是提示，不是记录：给人看的聊天区域仍然是实际时刻。"""
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    message_id = record_message(conn, "s", USER, "归档那条命令")
    conn.execute(
        "UPDATE transcript SET created_at = ? WHERE id = ?",
        (BASE, message_id),
    )
    conn.commit()
    set_run_origin(BASE)
    try:
        from spoolkit.memory.transcript import recent_messages

        shown = render_transcript(recent_messages(conn, "s"))
    finally:
        clear_run_origin()
        conn.close()
    assert MINUTE_CLOCK.search(shown), "记录侧不该被这次修改动到"


# ---- 4. 同一类污染：事件链与派发战绩注入时也不带实际时刻 --------------------
#
# 「任何注入到上下文的时间渲染」都要过这一关，不只是 `search_history`：
# 事件链的每一行也带时刻，而 `at_source='turn'` 的那些时刻是**这一轮的真实
# 钟点**（`session.started_at` + turn），同一份内容换个时刻重跑就会漂。


def _turn_event(at: float, event_id: int = 1):
    """一条 `at_source='turn'` 的事件：时刻来自这一轮的真实钟点。"""
    from spoolkit.memory.timeline.models import Event

    return Event(
        id=event_id,
        entity="wallet",
        attribute="balance",
        op="delta",
        value="-20",
        tz_offset_min=480,
        at_source="turn",
        time_precision="minute",
        source_ref="session:s1#turn:2",
        raw_text="花了 20",
        content_hash="h",
        unit="CNY",
        at_abs=at,
        speaker="user",
        confidence=0.9,
    )


def test_事件链里_本轮真实钟点按运行起点归一化() -> None:
    """`at_source='turn'` 的那一段两次重跑要一样；`explicit` 的不动（那是内容）。"""
    from spoolkit.memory.session import _event_line

    first = _turn_event(BASE - 30)
    second = _turn_event(BASE + SIX_MINUTES - 30)  # 同一条观察，晚 6 分钟重跑

    set_run_origin(BASE)
    try:
        line_first = _event_line(first)
    finally:
        clear_run_origin()
    set_run_origin(BASE + SIX_MINUTES)
    try:
        line_second = _event_line(second)
    finally:
        clear_run_origin()

    assert line_first == line_second, f"事件链的实时钟点会漂：{line_first} / {line_second}"
    assert not MINUTE_CLOCK.search(line_first)
    assert "event:1" in line_first and "花了 20" in line_first


def test_事件链里从正文读出的时刻保持精确() -> None:
    """从消息正文里读出来的时刻是**内容**：同一句话哪天重跑都是同一段渲染。"""
    from spoolkit.memory.session import _event_line

    event = _turn_event(BASE, event_id=2)
    explicit = type(event)(
        **{
            **{field: getattr(event, field) for field in event.__dataclass_fields__},
            "at_source": "explicit",
        }
    )
    set_run_origin(BASE + 3 * 86400)
    try:
        line = _event_line(explicit)
    finally:
        clear_run_origin()
    # 精确到分钟的实际时刻：`tz_offset_min=480` 把 BASE 渲染成这个钟点（纯函数）。
    from spoolkit.memory.timeline.clock import local_dt

    exact = local_dt(BASE, 480).strftime("%Y-%m-%d %H:%M")
    assert exact in line, f"正文里的时刻不该被归一化：{line}"


def test_派发战绩注入时剥掉实际时刻(tmp_path: Path) -> None:
    """日志文件留精确时刻（给人看的记录），注入上下文的那一份不留。"""
    root = tmp_path / "ws"
    root.mkdir()
    record_dispatch(root, kind="通过", calls=2, rounds=1, targets=3)

    raw = (root / ".agent" / "dispatch-log.md").read_text(encoding="utf-8")
    assert MINUTE_CLOCK.search(raw), "日志文件自己应当保留精确时刻"

    injected = dispatch_history(root)
    assert "[通过]" in injected and "次调用" in injected
    assert not MINUTE_CLOCK.search(injected), f"注入文本里还有实际时刻：{injected}"


# ---- 5. 作用域解析没被这次修改挪动 -----------------------------------------
#
# 这一条是**顺手钉住**的：上面几条改了渲染，作用域那套「默认只查当前会话」
# 的口径一个字都不能跟着动（它有自己的一组用例，这里只做交叉核对）。


def test_作用域口径没被渲染层的修改带着走() -> None:
    assert resolve_scope("", "现在").session == "现在"
    assert resolve_scope("*", "现在").session == ""
