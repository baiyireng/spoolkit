"""可注入时钟：`Clock` 协议 + 生产用 `SystemClock` + 测试用 `FakeClock`（计划 T8）。

设计稿 §15.5 硬约束 1 把这件事写死了：**会话当前时间必须来自注入的时钟**
（`session.started_at` + 本轮 turn 时间），禁止在解析/归约时现取 `datetime.now()`。
理由不是洁癖，是**可回放**："昨天"必须永远解析成同一个日期——同一份数据两次重算
得到两个日期时，它**不报错**，只是数不对（§15.3 第 4 条对同类 bug 的判词）。
§13.13 第 2 条把注入形式定案成"可注入时钟"（选 A），理由是硬性的：
**不可注入则 M4-T1 / M4-T6 无法自动化**。

本包内**唯一**允许读真实时间的地方是 `SystemClock.tz_name` / `now` / `tz_offset_min`；
其余模块一律从参数拿时钟。用例
`tests/memory/test_timeline_clock.py::test_新路径没有裸datetime_now` 逐文件钉住这件事
（AST 找出 `datetime.now()` / `time.time()` 的**所属类**，白名单只有 `SystemClock`）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """时间与时钟的两个来源口。

    三个方法都是**纯读**：不许改状态、不许取网络时间、不许读进程环境之外的
    隐式输入——`FakeClock` 与 `SystemClock` 必须可互换。
    """

    def now(self) -> float:
        """当前时刻，epoch 秒。"""
        ...

    def tz_offset_min(self) -> int:
        """当前时区相对 UTC 的偏移（分钟）。日界线按它算（§15.5 硬约束 3）。"""
        ...

    def tz_name(self) -> str | None:
        """时区名（可空）。只做展示与审计用，日界线不看它。"""
        ...


class SystemClock:
    """生产用真实时钟。

    **整个 `timeline/` 包唯一允许读真实时间的地方**：`time.time()` 取 epoch，
    `datetime.now()` 取本机时区。它没有状态，可以随便新建。
    """

    __slots__ = ()

    def now(self) -> float:
        return time.time()

    def tz_offset_min(self) -> int:
        offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        return int(offset.total_seconds() // 60)

    def tz_name(self) -> str | None:
        return datetime.now().astimezone().tzname()


class FakeClock:
    """测试用假时钟：时刻与时区都是**显式参数**，没有任何"现取"路径。

    `tz_offset_min` 默认 `480`（`+08:00`，本机时区），**但用例一律显式传**——
    依赖运行机器的本地时区会让"在我这儿能过"变成结论（计划 T8 风险 ②）。
    """

    __slots__ = ("_at_abs", "_tz_offset_min", "_tz_name")

    def __init__(
        self,
        at_abs: float,
        tz_offset_min: int = 480,
        tz_name: str | None = "Asia/Shanghai",
    ) -> None:
        self._at_abs = float(at_abs)
        self._tz_offset_min = int(tz_offset_min)
        self._tz_name = tz_name

    def now(self) -> float:
        return self._at_abs

    def tz_offset_min(self) -> int:
        return self._tz_offset_min

    def tz_name(self) -> str | None:
        return self._tz_name

    def advance(self, seconds: float) -> None:
        """把"现在"往前推 `seconds` 秒（会话跨天、事件晚到这类用例要用）。"""
        self._at_abs += float(seconds)

    def set_tz(self, tz_offset_min: int, tz_name: str | None = None) -> None:
        """换一个时区（同一个绝对时刻、另一个日界线的用例要用）。"""
        self._tz_offset_min = int(tz_offset_min)
        if tz_name is not None:
            self._tz_name = tz_name

    def __repr__(self) -> str:  # pragma: no cover - 只为报错好读
        return (
            f"FakeClock(at_abs={self._at_abs!r}, tz_offset_min={self._tz_offset_min!r},"
            f" tz_name={self._tz_name!r})"
        )


def fixed_tz(tz_offset_min: int) -> timezone:
    """把「分钟偏移」变成 `datetime.timezone`。

    **不查系统时区库**：日界线只由 `tz_offset_min` 决定（§15.5 硬约束 3），
    所以这里用的是**固定偏移**（不做夏令时）。夏令时是另一件事，本任务不碰。
    """
    return timezone(timedelta(minutes=int(tz_offset_min)))


def local_dt(at_abs: float, tz_offset_min: int) -> datetime:
    """把 epoch 秒渲染成**给定时区**下的 aware `datetime`（纯函数，不读时钟）。"""
    return datetime.fromtimestamp(float(at_abs), tz=fixed_tz(tz_offset_min))


def epoch_at(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    *,
    tz_offset_min: int = 480,
) -> float:
    """把一个**显式写出**的本地时刻换算成 epoch 秒。

    用例与夹具靠它写"2026-10-03 10:00 +08:00"这种可读的时刻，而不是一串魔数；
    它不读时钟，所以和"注入"这件事不冲突。
    """
    dt = datetime(
        int(year),
        int(month),
        int(day),
        int(hour),
        int(minute),
        int(second),
        tzinfo=fixed_tz(tz_offset_min),
    )
    return dt.timestamp()


def day_start(at_abs: float, tz_offset_min: int) -> float:
    """该时刻所在**那一日**的零点（按 `tz_offset_min` 算），epoch 秒。

    "今天花了 20" 的"今天"就从这里开始算；按 UTC 算会跨到前一天（§15.5 硬约束 3）。
    """
    dt = local_dt(at_abs, tz_offset_min)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def minute_start(at_abs: float, tz_offset_min: int) -> float:
    """该时刻所在**那一分钟**的起点（"同一精度窗口"要用，§15.6 第 3 条）。"""
    return float(int(float(at_abs) // 60) * 60)


def local_date_key(at_abs: float, tz_offset_min: int) -> str:
    """该时刻在给定时区下属于哪一天（`YYYY-MM-DD`）。只用来判等与展示。"""
    return local_dt(at_abs, tz_offset_min).strftime("%Y-%m-%d")
