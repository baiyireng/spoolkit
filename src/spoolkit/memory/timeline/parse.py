"""§15.5 的四步时间解析：绝对时间 / 相对词 / 没提时间 / 解析失败（计划 T8）。

**解析顺序（写死，第一命中即止）**——照 §15.5 的表：

| 顺序 | 输入形状 | `at_abs` | `at_source` | `time_precision` |
|---|---|---|---|---|
| 1 | 绝对时间（"2026-10-03 下午 3 点"） | 按用户时区换算成 epoch | `explicit` | `exact` / `minute` / `day` |
| 2 | 相对词（`今天` / `昨天` / `前天` / `刚才` / `上周`） | **基于注入时钟的当前时间**算 | `relative` | `day` / `minute` |
| 3 | 没提时间 | 本轮 turn 时间戳 | `turn` | `turn` |
| 4 | 解析失败 / 歧义 | `None` | `unknown` | `unknown` |

四条口径在本文件里落地：

1. **时钟必须注入**：`clock` 是必填关键字参数，`now()` / `tz_offset_min()` 都从它取
   （§15.5 硬约束 1）。本模块**没有** `datetime.now()`（用例逐文件钉住）。
2. **歧义不猜**（§15.5 硬约束 2）：跨年（"10 月 3 日"没给年）、12 小时制没给上午/下午
   （"3 点"）、只有钟点没给哪一天（"下午 3 点"）、点名了别的时区（"伦敦时间下午 3 点"）、
   会话跨了午夜（"昨天"指的是哪一天）→ 一律 `at_abs=None` + `at_source='unknown'` +
   `time_precision='unknown'` + 一句**给人看的** `reason`。**不抛异常、不给猜的值**，
   也不阻塞写入（调用方照样把原始观察落库，§15.5"不阻塞写入"）。
3. **日界线按 `tz_offset_min` 算**，不许按 UTC 算（§15.5 硬约束 3）："今天"是两个
   不同时区下的两个零点，同一个绝对时刻在 `+08:00` 与 `UTC` 下可以属于不同的两天。
4. **词表只承诺 §15.5 表里列出的那几个**（计划 T8 风险 ①）："前几天" / "上周三" /
   "上个月"这类**没定型**的写法落 `unknown`——**这是设计要的行为，不是缺陷**。

本模块只产出 `TimeResult`；渲染（链上带 `?` 标记、排在已解析事件之后）是 T11 的事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from spoolkit.memory.timeline.clock import Clock, day_start, epoch_at, fixed_tz, local_dt

# ---- 词表（只承诺这些；其余落 unknown）--------------------------------------

# 相对词：日期级。值 = 相对"今天"的天数偏移。
_DAY_WORDS: dict[str, int] = {"今天": 0, "昨天": -1, "前天": -2}
# 相对词：分钟级（锚在"现在"上，所以不受会话跨午夜影响）。
_MINUTE_WORDS: tuple[str, ...] = ("刚才",)
# 相对词：周级。口径：**上一个自然周的周一零点**（设计稿没给死数值，写在这里备查）。
_WEEK_WORDS: tuple[str, ...] = ("上周",)

_PERIODS = "凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|夜里|夜间"
# 上午侧 / 下午侧：12 小时制换 24 小时制时用。
_AM_PERIODS = ("凌晨", "清晨", "早上", "上午")

_RE_FULL_DATE = re.compile(
    r"(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*日?"
)
# 只有月日的两种写法：都**没给年份** → 跨年歧义（§15.5 硬约束 2）。
_RE_MD_CN = re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]")
_RE_MD_SLASH = re.compile(r"(?P<m>\d{1,2})\s*/\s*(?P<d>\d{1,2})(?!\d)")
_RE_HM = re.compile(
    r"(?<![\d:])(?P<h>\d{1,2})\s*[:：]\s*(?P<mi>\d{1,2})"
    r"(?:\s*[:：]\s*(?P<s>\d{1,2}))?"
)
# `3 点` 一定是钟点；`3 时` 只在有点名上下午的词时才算钟点——否则"2 小时"这种**时长**
# 会被误当成钟点，把"今天花了 2 小时"整句打成 unknown（时长不是时刻，读不出就落 turn）。
_RE_CN_DIAN = re.compile(
    r"(?:(?P<period>" + _PERIODS + r")\s*)?"
    r"(?P<h>\d{1,2})\s*点(?:\s*(?P<mi>\d{1,2})\s*分?)?"
)
_RE_CN_SHI = re.compile(
    r"(?P<period>" + _PERIODS + r")\s*"
    r"(?P<h>\d{1,2})\s*[时点](?:\s*(?P<mi>\d{1,2})\s*分?)?"
)
# "文本里像有时间词吗"：用来区分顺序 3（没提时间 → turn）与顺序 4（提了但解析不了
# → unknown）。宁可偏保守：把"读不出时刻"的句子标 unknown，也不假装它是本轮发生的。
_RE_TEMPORAL_HINT = re.compile(
    r"[年月日号天周]|星期|礼拜|今|昨|明|刚|\d\s*[点时秒分]"
    r"|上午|下午|晚上|早上|中午|凌晨|清晨|傍晚|夜里|夜间"
)
# 点名了另一个时区：跨时区歧义（§15.5 硬约束 2）——我们**不**做时区换算。
_RE_FOREIGN_TZ = re.compile(
    r"伦敦|纽约|东京|洛杉矶|巴黎|柏林|悉尼|太平洋时间|东部时间|中部时间"
    r"|UTC|GMT|格林尼治",
    re.IGNORECASE,
)

# 缺少基准之外的"证据链里没有可用值"也走 `missing_base`：`STATUS_REASONS` 只有五个
# （models.py），不给第六个出口。
_REASON_NO_YEAR = "只有月日、没给年份：跨年歧义，不猜是哪一年"
_REASON_NO_DAY = "只有钟点、没给哪一天：不猜是今天还是别的哪一天"
_REASON_AMPM = "12 小时制没给「上午 / 下午」：不猜是哪半天"
_REASON_BAD_CLOCK = "钟点越界（小时>23 / 分钟>59 / 秒>59），不是合法时刻"
_REASON_BAD_DATE = "日期越界（不存在的月或日），不是合法日期"
_REASON_FOREIGN_TZ = "文本点名了另一个时区：跨时区歧义，不做时区换算"
_REASON_CROSS_MIDNIGHT = "会话跨了午夜：相对日词指的是哪一天有歧义"
_REASON_UNKNOWN_WORD = "文本里有时间词，但不在承诺的词表里（§15.5 只承诺那几个）"


@dataclass(frozen=True)
class TimeResult:
    """一次解析的结果。`at_abs is None` 就是"这条时间不可用"的**唯一**表示。"""

    at_abs: float | None
    tz_offset_min: int
    at_source: str  # explicit|relative|turn|unknown
    time_precision: str  # exact|minute|day|turn|unknown
    reason: str = ""  # 仅 unknown 时有话；其余为空串
    tz_name: str | None = None

    @property
    def usable_as_instant(self) -> bool:
        """能不能当"精确时刻"用（§15.5：unknown 的不参与同刻冲突判定）。"""
        return self.at_abs is not None and self.time_precision != "unknown"


def looks_temporal(text: str | None) -> bool:
    """文本里有没有"像时间"的写法。用来把"没提时间"与"提了但读不出"分开。"""
    return bool(_RE_TEMPORAL_HINT.search(text or ""))


def parse_time(
    text: str | None,
    *,
    clock: Clock,
    session_started_at: float | None = None,
    turn_at: float | None = None,
) -> TimeResult:
    """把一句原文里的时间解析成绝对时间戳（§15.5 四步，第一命中即止）。

    `text=None` / 空串 = **没提时间** → 走本轮的 `turn_at`（没给就用 `clock.now()`）。
    `session_started_at` 只用来判"会话跨了午夜"这条歧义；不给就等于"会话没跨天"。
    """
    tz_min = clock.tz_offset_min()
    tz_name = clock.tz_name()
    raw = (text or "").strip()

    if _RE_FOREIGN_TZ.search(raw):
        return _unknown(tz_min, tz_name, _REASON_FOREIGN_TZ)

    # ---- 顺序 1：绝对时间（日期 + 可选的钟点）--------------------------------
    date_hit = _match_full_date(raw)
    if isinstance(date_hit, _Fail):
        return _unknown(tz_min, tz_name, date_hit.reason)
    clock_hit = _match_clock(raw)
    if isinstance(clock_hit, _Fail):
        return _unknown(tz_min, tz_name, clock_hit.reason)
    if date_hit is not None:
        year, month, day = date_hit
        if clock_hit is None:
            return _hit(epoch_at(year, month, day, tz_offset_min=tz_min), "day", tz_min, tz_name, "explicit")
        hour, minute, second, precision = clock_hit
        at_abs = epoch_at(
            year, month, day, hour, minute, second, tz_offset_min=tz_min
        )
        return _hit(at_abs, precision, tz_min, tz_name, "explicit")

    # ---- 顺序 2：相对词（可以和钟点合用，如"昨天下午 3 点"）-------------------
    now = clock.now()
    same_day = _same_local_day(session_started_at, now, tz_min)
    day_word = _find_day_word(raw)
    if day_word is not None:
        if not same_day:
            return _unknown(tz_min, tz_name, _REASON_CROSS_MIDNIGHT)
        base = _day_start_offset(now, tz_min, _DAY_WORDS[day_word])
        return _combine_day(base, clock_hit, tz_min, tz_name)
    if any(word in raw for word in _MINUTE_WORDS):
        at_abs = float(int(now // 60) * 60)  # "刚才"：分钟级，锚在"现在"
        return _hit(at_abs, "minute", tz_min, tz_name, "relative")
    if any(word in raw for word in _WEEK_WORDS):
        if not same_day:
            return _unknown(tz_min, tz_name, _REASON_CROSS_MIDNIGHT)
        base = _last_week_monday(now, tz_min)
        return _combine_day(base, clock_hit, tz_min, tz_name)

    # ---- 只有钟点、没有日期：歧义（不猜是哪一天）-----------------------------
    if clock_hit is not None:
        return _unknown(tz_min, tz_name, _REASON_NO_DAY)
    if isinstance(date_hit, _Fail):  # pragma: no cover - 上面已经拦过，留作兜底
        return _unknown(tz_min, tz_name, date_hit.reason)
    if _RE_MD_CN.search(raw) or _RE_MD_SLASH.search(raw):
        return _unknown(tz_min, tz_name, _REASON_NO_YEAR)

    # ---- 顺序 3：没提时间（turn 时间戳）／顺序 4：提了但读不出（unknown）------
    if looks_temporal(raw):
        return _unknown(tz_min, tz_name, _REASON_UNKNOWN_WORD)
    at = float(turn_at) if turn_at is not None else float(now)
    return _hit(at, "turn", tz_min, tz_name, "turn")


# ---- 内部：小的载体与规则 ----------------------------------------------------


@dataclass(frozen=True)
class _Fail:
    """"看得像时间、但**不许猜**"的三种失败共用一个载体（带上给人的理由）。"""

    reason: str


def _hit(
    at_abs: float,
    precision: str,
    tz_min: int,
    tz_name: str | None,
    source: str,
) -> TimeResult:
    return TimeResult(
        at_abs=float(at_abs),
        tz_offset_min=int(tz_min),
        at_source=source,
        time_precision=precision,
        tz_name=tz_name,
    )


def _unknown(tz_min: int, tz_name: str | None, reason: str) -> TimeResult:
    return TimeResult(
        at_abs=None,
        tz_offset_min=int(tz_min),
        at_source="unknown",
        time_precision="unknown",
        reason=reason,
        tz_name=tz_name,
    )


def _match_full_date(text: str) -> tuple[int, int, int] | _Fail | None:
    """完整的年月日（含非法日期检测）。只有月日 → `_Fail`（跨年歧义）。"""
    match = _RE_FULL_DATE.search(text)
    if match is None:
        if _RE_MD_CN.search(text) or _RE_MD_SLASH.search(text):
            return _Fail(_REASON_NO_YEAR)
        return None
    year, month, day = (
        int(match.group("y")),
        int(match.group("m")),
        int(match.group("d")),
    )
    try:
        datetime(year, month, day)
    except ValueError:
        return _Fail(_REASON_BAD_DATE)
    return (year, month, day)


def _match_clock(text: str) -> tuple[int, int, int, str] | _Fail | None:
    """钟点：`15:00` / `15:00:30`（24 小时制）或 `下午 3 点` / `上午 9 点 30 分`。

    返回 `(hour, minute, second, precision)`。中文的 `3 点`**没给上午/下午**时是歧义
    （`_Fail`）——这与"不猜"是同一条规矩。
    """
    match = _RE_HM.search(text)
    if match is not None:
        hour, minute = int(match.group("h")), int(match.group("mi"))
        second = int(match.group("s") or 0)
        if hour > 23 or minute > 59 or second > 59:
            return _Fail(_REASON_BAD_CLOCK)
        return (hour, minute, second, "exact" if match.group("s") else "minute")

    match = _RE_CN_DIAN.search(text) or _RE_CN_SHI.search(text)
    if match is None:
        return None
    hour = int(match.group("h"))
    minute = int(match.group("mi") or 0)
    period = match.group("period")
    if hour > 23 or minute > 59:
        return _Fail(_REASON_BAD_CLOCK)
    if period is None:
        if 1 <= hour <= 12:
            return _Fail(_REASON_AMPM)
        return (hour, minute, 0, "minute")
    if period in _AM_PERIODS:
        hour = 0 if hour == 12 else hour
    elif hour < 12:
        hour += 12
    if hour > 23:
        return _Fail(_REASON_BAD_CLOCK)
    return (hour, minute, 0, "minute")


def _find_day_word(text: str) -> str | None:
    for word in _DAY_WORDS:
        if word in text:
            return word
    return None


def _same_local_day(
    session_started_at: float | None, now: float, tz_min: int
) -> bool:
    """会话有没有跨午夜。没给 `session_started_at` = 不知道跨没跨 = 当作没跨。"""
    if session_started_at is None:
        return True
    return day_start(session_started_at, tz_min) == day_start(now, tz_min)


def _day_start_offset(at_abs: float, tz_min: int, days: int) -> float:
    """`at_abs` 所在那一天偏移 `days` 天后的零点（按日算，不按 86400 秒加）。"""
    date = local_dt(at_abs, tz_min).date() + timedelta(days=int(days))
    return datetime(
        date.year, date.month, date.day, tzinfo=fixed_tz(tz_min)
    ).timestamp()


def _last_week_monday(at_abs: float, tz_min: int) -> float:
    """"上周"= 上一个自然周的周一零点（口径写在这里，设计稿只承诺词、没给数值）。"""
    today = local_dt(at_abs, tz_min).date()
    monday = today - timedelta(days=today.weekday()) - timedelta(days=7)
    return datetime(
        monday.year, monday.month, monday.day, tzinfo=fixed_tz(tz_min)
    ).timestamp()


def _combine_day(
    day_start_abs: float,
    clock_hit: tuple[int, int, int, str] | _Fail | None,
    tz_min: int,
    tz_name: str | None,
) -> TimeResult:
    """相对日词 + 可选钟点：`昨天` → 那一日零点（`day`）；`昨天 15:00` → `minute`。"""
    if not isinstance(clock_hit, tuple):
        return _hit(day_start_abs, "day", tz_min, tz_name, "relative")
    hour, minute, second, precision = clock_hit
    return _hit(
        day_start_abs + hour * 3600 + minute * 60 + second,
        precision,
        tz_min,
        tz_name,
        "relative",
    )
