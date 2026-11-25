"""T8 的解析一半：§15.5 的四步时间解析（设计稿 §15.5 + 计划 Task 8 / M4-T2）。

四组证据：

1. **M4-T2**：`今天/昨天/前天/刚才/上周` 都解析成**绝对时间戳 + `tz_offset_min`**，
   `at_source='relative'`，精度逐条对得上（`day` / `minute`）；
2. **四步映射逐条**：`explicit` / `relative` / `turn` / `unknown` 各一条，精度照表；
3. **歧义不猜**：跨年、只有钟点、12 小时制、越界日期/钟点、词表外的相对写法、
   点名了别的时区 → **一条都不许猜**（`at_abs is None` + 一句给人看的 `reason`），
   而且**不抛异常**（§15.5 硬约束 2：不阻塞写入）；
4. **日界按 `tz_offset_min` 算**，不按 UTC：同一个绝对时刻在两个时区下可以是两天。
   另外会话跨午夜时，相对日词落 `unknown`（"昨天"指的是哪一天有歧义）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from spoolkit.memory.timeline.clock import FakeClock, day_start, epoch_at, local_date_key, local_dt
from spoolkit.memory.timeline.parse import looks_temporal, parse_time

# 一个固定的"现在"：2026-10-03 10:00 +08:00。用例一律显式传时区，
# 不许依赖运行机器的本地时区（计划 T8 风险 ②）。
NOW = epoch_at(2026, 10, 3, 10, 0, tz_offset_min=480)


def _clock(at_abs: float = NOW, tz_offset_min: int = 480) -> FakeClock:
    return FakeClock(at_abs, tz_offset_min=tz_offset_min)


def test_M4_T2_相对词解析成绝对时间戳() -> None:
    """M4-T2：相对词 → 绝对时间戳（含 `tz_offset_min`），`at_source='relative'`。"""
    clock = _clock()
    today = local_dt(clock.now(), 480).date()
    last_monday = today - timedelta(days=today.weekday()) - timedelta(days=7)

    cases = (
        ("今天", epoch_at(2026, 10, 3), "day"),
        ("昨天", epoch_at(2026, 10, 2), "day"),
        ("前天", epoch_at(2026, 10, 1), "day"),
        ("刚才", float(int(clock.now() // 60) * 60), "minute"),
        (
            "上周",
            epoch_at(last_monday.year, last_monday.month, last_monday.day),
            "day",
        ),
    )
    for text, want_abs, want_precision in cases:
        result = parse_time(text, clock=clock)
        assert result.at_abs == want_abs, text
        assert result.at_source == "relative", text
        assert result.time_precision == want_precision, text
        assert result.tz_offset_min == 480, text
        assert result.reason == ""
        assert result.usable_as_instant is True
    # "刚才"是分钟级：它锚在注入的"现在"上，不受会话跨午夜影响。
    assert parse_time("刚才", clock=clock).at_abs <= clock.now()
    # 同一个钟换一天，只有相对词的结果跟着动（绝对时间不受影响）。
    later = _clock(epoch_at(2026, 10, 5, 10, 0))
    assert parse_time("今天", clock=later).at_abs == epoch_at(2026, 10, 5)
    assert parse_time("2026-10-03", clock=later).at_abs == epoch_at(2026, 10, 3)


def test_四步映射逐条() -> None:
    """§15.5 的表逐条：顺序 1 绝对 / 2 相对 / 3 没提时间 / 4 解析失败。"""
    clock = _clock()
    cases = (
        ("2026-10-03 下午 3 点", epoch_at(2026, 10, 3, 15, 0), "explicit", "minute"),
        ("2026-10-03 上午 9 点 30 分", epoch_at(2026, 10, 3, 9, 30), "explicit", "minute"),
        ("2026-10-03 15:00:30", epoch_at(2026, 10, 3, 15, 0, 30), "explicit", "exact"),
        ("2026-10-03", epoch_at(2026, 10, 3), "explicit", "day"),
        ("昨天下午 3 点", epoch_at(2026, 10, 2, 15, 0), "relative", "minute"),
        ("今天", epoch_at(2026, 10, 3), "relative", "day"),
        ("我身上有 100 块", NOW, "turn", "turn"),
        ("", NOW, "turn", "turn"),
    )
    for text, want_abs, want_source, want_precision in cases:
        result = parse_time(text, clock=clock)
        assert result.at_abs == want_abs, text
        assert result.at_source == want_source, text
        assert result.time_precision == want_precision, text
        assert result.tz_offset_min == 480, text
    # 顺序 1 的钟点按**注入时钟的时区**换算：同一个墙上时间在 UTC 下是另一个瞬间。
    assert parse_time("2026-10-03 10:00", clock=clock).at_abs == epoch_at(2026, 10, 3, 10, 0)
    assert parse_time(
        "2026-10-03 10:00", clock=_clock(NOW, tz_offset_min=0)
    ).at_abs == epoch_at(2026, 10, 3, 10, 0, tz_offset_min=0)


def test_没提时间就用本轮turn时间戳() -> None:
    clock = _clock()
    turn_at = epoch_at(2026, 10, 3, 10, 7, 30)
    result = parse_time("我赚了 50", clock=clock, turn_at=turn_at)
    assert (result.at_abs, result.at_source, result.time_precision) == (
        turn_at,
        "turn",
        "turn",
    )
    assert result.tz_offset_min == 480
    # 没给 turn 时间就用注入时钟的"现在"（**不是**真实的现在）。
    assert parse_time(None, clock=clock).at_abs == clock.now()
    assert parse_time("   ", clock=clock).at_abs == clock.now()
    # "文本里像时间"的判定与解析分开：句子里的"100 块"、"50"不算时间词。
    assert looks_temporal("我身上有 100 块") is False
    assert looks_temporal("前几天花了 20") is True


@pytest.mark.parametrize(
    "text",
    (
        "10 月 3 日",  # 跨年：没给年份
        "10/3",  # 同上，另一种写法
        "下午 3 点",  # 只有钟点，没给哪一天
        "2026-10-03 9 点",  # 12 小时制没给上午/下午
        "2026-02-30",  # 不存在的日期
        "2026-13-01",  # 不存在的月份
        "2026-10-03 25:00",  # 钟点越界
        "前几天花了 20",  # 词表外的相对写法（§15.5 只承诺那几个）
        "上个月",  # 同上
        "周三",  # 同上
        "伦敦时间下午 3 点",  # 跨时区：不做换算
    ),
)
def test_歧义不猜(text: str) -> None:
    """§15.5 硬约束 2：解析不出来**不猜**——落 unknown + 理由，且不抛异常。"""
    result = parse_time(text, clock=_clock())
    assert result.at_abs is None, text
    assert result.at_source == "unknown", text
    assert result.time_precision == "unknown", text
    assert result.reason, text  # 必须给理由：人要知道"这一条的时间不可用"
    assert result.tz_offset_min == 480
    assert result.usable_as_instant is False
    # 原始观察照样能落库（不阻塞写入）：unknown 不是异常。
    assert isinstance(result.reason, str)


def test_日界随时区() -> None:
    """§15.5 硬约束 3：日界线按 `tz_offset_min` 算，不许按 UTC 算。"""
    # ① 同一个绝对时刻、同一天（本地日期都是 10-03）→ 两个"今天"差 8 小时。
    instant = epoch_at(2026, 10, 3, 18, 0, tz_offset_min=480)
    shanghai = parse_time("今天", clock=_clock(instant, 480))
    utc = parse_time("今天", clock=_clock(instant, 0))
    assert shanghai.at_abs == epoch_at(2026, 10, 3, 0, 0, tz_offset_min=480)
    assert utc.at_abs == epoch_at(2026, 10, 3, 0, 0, tz_offset_min=0)
    assert utc.at_abs - shanghai.at_abs == 8 * 3600

    # ② 跨午夜的那条观测：同一个绝对时刻（+08:00 的 10-03 05:00 = UTC 的 10-02 21:00）
    #    在两个时区下**落在不同的两天**——按 UTC 算日界就会把"今天"算成前一天。
    midnight_crosser = epoch_at(2026, 10, 3, 5, 0, tz_offset_min=480)
    sh_abs = parse_time("今天", clock=_clock(midnight_crosser, 480)).at_abs
    utc_abs = parse_time("今天", clock=_clock(midnight_crosser, 0)).at_abs
    assert local_date_key(sh_abs, 480) == "2026-10-03"
    assert local_date_key(utc_abs, 0) == "2026-10-02"
    assert day_start(sh_abs, 480) != day_start(utc_abs, 0)
    # "昨天"同样随日界走：两个时区下的昨天差着一整天。
    assert parse_time("昨天", clock=_clock(midnight_crosser, 480)).at_abs == epoch_at(
        2026, 10, 2, tz_offset_min=480
    )
    assert parse_time("昨天", clock=_clock(midnight_crosser, 0)).at_abs == epoch_at(
        2026, 10, 1, tz_offset_min=0
    )


def test_会话跨午夜时相对日词落unknown() -> None:
    """会话自己跨了午夜 → "昨天"指的是哪一天有歧义（§15.5 硬约束 2）。"""
    now = epoch_at(2026, 10, 3, 0, 30, tz_offset_min=480)
    started = epoch_at(2026, 10, 2, 23, 50, tz_offset_min=480)
    clock = _clock(now)

    crossed = parse_time("昨天", clock=clock, session_started_at=started)
    assert crossed.at_abs is None
    assert crossed.at_source == "unknown"
    assert "午夜" in crossed.reason
    # 没给"会话什么时候开的"= 不知道跨没跨 = 按不跨处理（不凭空造歧义）。
    assert parse_time("昨天", clock=clock).at_abs == epoch_at(2026, 10, 2)
    # 同一个会话里"刚才"仍然可用：它是分钟级，锚在"现在"上。
    fresh = parse_time("刚才", clock=clock, session_started_at=started)
    assert fresh.at_source == "relative"
    assert fresh.at_abs == float(int(now // 60) * 60)
