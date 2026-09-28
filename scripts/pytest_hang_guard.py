"""把"挂住"变成"失败 + 堆栈"，而不是把整轮执行一起拖死。

**为什么需要它**（这个仓库真踩过）：项目用例里有几类会永远等下去的东西——等 stdin、
等 socket、等子进程退出、等一个没人写的队列。挂在一次 pytest 里，代价不是"这一条没过"，
而是三倍：

1. **证据归零**：pytest 的输出重定向到文件时是块缓冲，进程被杀之后日志**一个字节都没有**
   ——连"停在哪条用例"都问不出来（本批真的出现过 0 字节日志）；
2. **调用方一起死**：跑测试的是人/agent 的一条命令，命令不返回，整轮工作就丢了
   （本批我自己的 tool call 连续三次被挂断）；
3. **残留毒化**：被杀之后可能留下子进程或半开端口，让后面几次运行也不正常。

**做法**（只用标准库，不引新依赖）：

- `faulthandler.dump_traceback_later(秒数, exit=True)`：到点把**所有线程的堆栈**打到
  stderr，然后**真的退出**。于是"挂住"变成"有堆栈的失败"，而且不会有残留。
  逐条用例重新计时（setup 时先 `cancel_dump_traceback_later()` 再 arm），
  这样"哪一条挂了"和"它卡在哪一行"一次就拿到。
- **逐条 flush 的进度行**：每条用例开始前把它写进 `PYTEST_HANG_LOG`（默认
  `.tmp/pytest-progress.log`）并 flush，所以即使进程被杀，日志最后一行就是凶手。
- **超时可配**：`PYTEST_HANG_SECONDS`（默认 90 秒）。**不写死在代码里**——慢机器上
  真跑模型/子进程的用例本来就慢，这个值是给调用方拧的。

用法（`scripts/pytest.ps1` 已经默认带上这个插件，所以正常跑就有保护）：

    $env:PYTEST_HANG_SECONDS = '45'      # 可选：改超时
    .\\scripts\\pytest.ps1 -Repo .worktrees\\x tests\\cli
    # 若某条挂住：日志里会有 "=== 卡住：<用例名> ===" 与全线程堆栈，
    # 进程退出码非零，下一轮可以用 -k 'not <用例名>' 把已知的挂死项排掉继续。
"""

from __future__ import annotations

import faulthandler
import os
from pathlib import Path

DEFAULT_SECONDS = 90.0


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


def pytest_configure(config) -> None:  # noqa: ARG001 - pytest 钩子签名
    seconds = _seconds()
    _mark(f"=== 本轮开始，单条用例超时 {seconds:g} 秒；卡住会打印全线程堆栈并退出 ===")
    faulthandler.enable()
    faulthandler.dump_traceback_later(seconds, exit=True)


def pytest_runtest_setup(item) -> None:
    """逐条重新计时：上一条跑完了就把计时器收回，这一条重新 armed。

    日志里**同时**写人类看的行与机器看的行。机器标记**只用 ASCII**（`START`/`END`/`HANG`）：
    中文标记会被"按 ANSI 读的调用方"整片读歪，`-like '开始 *'` 永远不匹配，
    于是自动隔离的驱动脚本会**误报"没有卡死"**——本批真踩过这一次。
    """
    faulthandler.cancel_dump_traceback_later()
    faulthandler.dump_traceback_later(_seconds(), exit=True)
    _mark(f"HANG {item.nodeid}")
    _mark(f"START {item.nodeid}")


def pytest_runtest_teardown(item, nextitem) -> None:  # noqa: ARG001
    faulthandler.cancel_dump_traceback_later()
    _mark(f"END {item.nodeid}")


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    faulthandler.cancel_dump_traceback_later()
    _mark(f"=== 本轮结束 exitstatus={exitstatus} ===")
