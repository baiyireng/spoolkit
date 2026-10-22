"""T8 的时钟一半：可注入时钟（设计稿 §15.5 硬约束 1 + 计划 Task 8）。

三件事钉在这里：

1. **协议真的能互换**：`SystemClock` 与 `FakeClock` 都满足 `Clock`（`runtime_checkable`）；
2. **同一文本、两个时钟、两个结果**（`test_同一文本两个时钟两个结果`，用例名照计划
   「证伪速查表」）——把 `parse_time` 改成内部现取 `datetime.now()`
   （或者把 `FakeClock` 的 `now()` 改成读真实时间）→ 两条结果相同 → **红**；
3. **源码级**：`timeline/` 里除 `SystemClock` 外**没有**裸 `datetime.now()` /
   `time.time()`（`test_新路径没有裸datetime_now`，计划 T8 验收 5）。判定用 AST，
   不是 grep：白名单是"**类** `SystemClock`"，所以搬进 `FakeClock` 里也照样红。
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import spoolkit.memory.timeline as timeline_pkg
from spoolkit.memory.timeline.clock import (
    Clock,
    FakeClock,
    SystemClock,
    day_start,
    epoch_at,
    local_date_key,
    local_dt,
    minute_start,
)
from spoolkit.memory.timeline.parse import parse_time

TIMELINE_DIR = Path(timeline_pkg.__file__).resolve().parent

# "裸取现在"的写法：`datetime.now()` / `datetime.utcnow()` / `datetime.today()` /
# `time.time()` / `time.time_ns()`。白名单**只有** `SystemClock` 这个类。
_BARE_NOW = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
    ("time", "time"),
    ("time", "time_ns"),
}


class _BareNowVisitor(ast.NodeVisitor):
    """把每个"裸取现在"的调用连同它**所属的类**记下来。"""

    def __init__(self) -> None:
        self.classes: list[str] = []
        self.hits: list[tuple[str | None, int, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if (func.value.id, func.attr) in _BARE_NOW:
                owner = self.classes[-1] if self.classes else None
                self.hits.append((owner, node.lineno, f"{func.value.id}.{func.attr}()"))
        self.generic_visit(node)


def test_系统时钟与假时钟都满足Clock协议() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FakeClock(epoch_at(2026, 10, 3), tz_offset_min=480), Clock)
    # `FakeClock` 的三个口就是注入进来的那三个值：没有任何"现取"路径。
    clock = FakeClock(epoch_at(2026, 10, 3, 10, 0), tz_offset_min=480, tz_name="Asia/Shanghai")
    assert clock.now() == epoch_at(2026, 10, 3, 10, 0)
    assert clock.tz_offset_min() == 480
    assert clock.tz_name() == "Asia/Shanghai"


def test_FakeClock可以推动与换时区() -> None:
    clock = FakeClock(epoch_at(2026, 10, 3, 10, 0), tz_offset_min=480)
    clock.advance(2 * 24 * 3600)
    assert clock.now() == epoch_at(2026, 10, 5, 10, 0)
    clock.set_tz(0, "UTC")
    assert (clock.tz_offset_min(), clock.tz_name()) == (0, "UTC")
    # 换时区**不改**绝对时刻：日界线的差别只由 tz_offset_min 决定。
    assert clock.now() == epoch_at(2026, 10, 5, 10, 0)


def test_同一文本两个时钟两个结果() -> None:
    """计划 T8 验收 1：同一份文本 `昨天`，两个 `FakeClock` → 两个 `at_abs`。

    这条就是"时钟真的被注入了"的机械判据：内部现取 `datetime.now()` 的实现
    会让两次结果相同（都是"真的昨天"），于是这里红。
    """
    first_clock = FakeClock(epoch_at(2026, 10, 3, 10, 0), tz_offset_min=480)
    second_clock = FakeClock(epoch_at(2026, 10, 5, 10, 0), tz_offset_min=480)

    first = parse_time("昨天", clock=first_clock)
    second = parse_time("昨天", clock=second_clock)

    assert first.at_abs == epoch_at(2026, 10, 2)
    assert second.at_abs == epoch_at(2026, 10, 4)
    assert first.at_abs != second.at_abs
    assert (first.at_source, second.at_source) == ("relative", "relative")
    assert (first.time_precision, second.time_precision) == ("day", "day")
    # 同一个钟往前推两天，同一句话又落到第三天（相对词锚在注入的"现在"上）。
    first_clock.advance(2 * 24 * 3600)
    assert parse_time("昨天", clock=first_clock).at_abs == epoch_at(2026, 10, 4)


def test_新路径没有裸datetime_now() -> None:
    """源码级：`timeline/` 里除 `SystemClock` 外没有裸 `datetime.now()` / `time.time()`。

    白名单是**类**，不是文件：把 `datetime.now()` 从 `SystemClock` 挪进 `FakeClock`
    或 `parse.py`，这条都会红。
    """
    hits: list[tuple[str, str | None, int, str]] = []
    files = sorted(TIMELINE_DIR.glob("*.py"))
    assert files, "timeline/ 下一个 .py 都没有？路径错了"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _BareNowVisitor()
        visitor.visit(tree)
        for owner, lineno, expr in visitor.hits:
            hits.append((path.name, owner, lineno, expr))

    owners = {owner for _, owner, _, _ in hits}
    # 白名单非空（否则这条用例是"空跑"）：`SystemClock` 里确实在取真实时间。
    assert owners == {"SystemClock"}, hits
    assert {name for name, _, _, _ in hits} == {"clock.py"}, hits
    assert {expr for _, _, _, expr in hits} == {"time.time()", "datetime.now()"}, hits


def test_时间辅助函数都是纯函数() -> None:
    """日界/分钟界/本地日期：只看参数，不读时钟（换时区就换日界，§15.5 硬约束 3）。"""
    instant = epoch_at(2026, 10, 3, 5, 0, tz_offset_min=480)
    assert day_start(instant, 480) == epoch_at(2026, 10, 3, 0, 0, tz_offset_min=480)
    assert day_start(instant, 0) == epoch_at(2026, 10, 2, 0, 0, tz_offset_min=0)
    assert local_date_key(instant, 480) == "2026-10-03"
    assert local_date_key(instant, 0) == "2026-10-02"
    assert minute_start(epoch_at(2026, 10, 3, 10, 30, 42), 480) == epoch_at(
        2026, 10, 3, 10, 30
    )
    assert isinstance(local_dt(instant, 480), datetime)
    # `epoch_at` 与 `local_dt` 互逆（同一个时区下）。
    assert local_dt(instant, 480).strftime("%Y-%m-%d %H:%M") == "2026-10-03 05:00"
