"""把"挂住"变成"失败 + 堆栈"，而不是把整轮执行一起拖死。

**为什么需要它**（这个仓库真踩过）：项目用例里有几类会永远等下去的东西——等 stdin、
等 socket、等子进程退出、等一个没人写的队列。挂在一次 pytest 里，代价不是"这一条没过"，
而是三倍：

1. **证据归零**：pytest 的输出重定向到文件时是块缓冲，进程被杀之后日志**一个字节都没有**
   ——连"停在哪条用例"都问不出来（本批真的出现过 0 字节日志）；
2. **调用方一起死**：跑测试的是人/agent 的一条命令，命令不返回，整轮工作就丢了
   （本批我的 tool call 连续三次被挂断，另一个实现者因此空转了 6.5 小时）；
3. **残留毒化**：被杀之后可能留下子进程或半开端口，让后面几次运行也不正常。

**做法**（只用标准库，不引新依赖）：

- `faulthandler.dump_traceback_later(秒数, exit=True)`：到点把**所有线程的堆栈**写出来，
  然后**真的退出**。于是"挂住"变成"有堆栈的失败"，而且不会有残留。
  逐条用例重新计时（setup 时先 `cancel_dump_traceback_later()` 再 arm），
  这样"哪一条挂了"和"它卡在哪一行"一次就拿到。
- **堆栈写进同一个进度日志文件**（`PYTEST_HANG_LOG`），**不是 fd 2**。
  这是踩出来的：pytest 会捕获 fd 2，`faulthandler` 默认往那儿写，于是堆栈**被 pytest 吃掉**，
  日志里那句"堆栈见 progress.log"变成**空头支票**（真发生过）。所以 `enable(file=…)` 与
  `dump_traceback_later(file=…)` 都显式指到日志文件。
- **逐条 flush 的进度行**：每条用例开始前写 `START <nodeid>`、结束写 `END <nodeid>`
  （`scripts/pytest_bounded.ps1` 靠"有 START 没有 END"认出凶手），另外写一行 `HANG <nodeid>`。
  **机器读的标记只用 ASCII**：中文标记在"按 ANSI 读"的调用方那里会整片读歪，
  比较全部失配、于是驱动脚本**误报"没有卡死"**（也真发生过）。
- **超时可配**：`PYTEST_HANG_SECONDS`（默认 90 秒）。**不写死在代码里**——慢机器上
  真跑模型/子进程的用例本来就慢，这个值是给调用方拧的。
"""

from __future__ import annotations

import faulthandler
import os
import sys
from pathlib import Path

DEFAULT_SECONDS = 90.0

# 堆栈要写进去的那个文件。开着不关：进程退出时由操作系统收尾，而它的全部意义
# 就是"进程没能正常退出时也留下话"。
_fault_file = None


def _seconds() -> float:
    raw = os.environ.get("PYTEST_HANG_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SECONDS
    return value if value > 0 else DEFAULT_SECONDS


def _log_path() -> Path:
    raw = os.environ.get("PYTEST_HANG_LOG", "").strip()
    path = Path(raw) if raw else Path(".tmp") / "pytest-progress.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def _mark(text: str) -> None:
    """往进度日志追加一行并**立刻 flush**：被杀也要留证据，这是这个插件的全部意义。"""
    try:
        with _log_path().open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()
    except OSError:
        pass


def _fault_stream():
    """堆栈该写到哪儿：**优先日志文件**，开不出来才退回 stderr。

    为什么不能直接用它默认的 fd 2：pytest 会把 fd 2 捕获走，堆栈于是被吞掉，
    留给人一句"堆栈见 progress.log"的空头支票（本批真发生过，靠 `-s` 重跑才拿到堆栈）。
    """
    global _fault_file
    if _fault_file is None:
        try:
            _fault_file = _log_path().open("a", encoding="utf-8", errors="replace")
        except OSError:
            return sys.stderr
    return _fault_file


def pytest_configure(config) -> None:  # noqa: ARG001 - pytest 钩子签名
    seconds = _seconds()
    stream = _fault_stream()
    _mark(f"=== 本轮开始，单条用例超时 {seconds:g} 秒；卡住会把全线程堆栈写进本文件 ===")
    faulthandler.enable(file=stream)
    faulthandler.dump_traceback_later(seconds, exit=True, file=stream)


def pytest_runtest_setup(item) -> None:
    """逐条重新计时：上一条跑完了就把计时器收回，这一条重新 armed。"""
    stream = _fault_stream()
    faulthandler.cancel_dump_traceback_later()
    faulthandler.dump_traceback_later(_seconds(), exit=True, file=stream)
    _mark(f"HANG {item.nodeid}")
    _mark(f"START {item.nodeid}")


def pytest_runtest_teardown(item, nextitem) -> None:  # noqa: ARG001
    faulthandler.cancel_dump_traceback_later()
    _mark(f"END {item.nodeid}")


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    faulthandler.cancel_dump_traceback_later()
    _mark(f"=== 本轮结束 exitstatus={exitstatus} ===")
