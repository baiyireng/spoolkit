"""内核侧的事件输出器。

`--events` 模式下，stdout 上只应该有 JSON 行——不夹杂任何散文。
散文会被解析器忽略，但录下来的文件也会变脏，调试时难读。

每条都 flush：不 flush 的话输出攒在缓冲区里，UI 那边看起来像卡住了，
而你会先去怀疑网络。
"""

import sys
from pathlib import Path
from typing import Callable, Sequence
from typing import Any, TextIO

from spoolkit.agents.plan import out_of_scope
from spoolkit.cli.approval import apply_with_audit
from spoolkit.policy import AUTO
from spoolkit.tools.edit import PendingChanges, save_baseline
from spoolkit.web.protocol import AWAIT, CONFIRM, DIFF, Event


class EventWriter:
    """把事件按行写到流上。"""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def emit(self, kind: str, **data: Any) -> None:
        self._stream.write(Event(kind, data).to_line() + "\n")
        self._stream.flush()

    def handle(self, kind: str, data: dict) -> None:
        """签名与 AgentLoop 的 on_event 一致，可直接传进去。"""
        self.emit(kind, **data)


def settle_with_events(
    pending: PendingChanges,
    policy: str,
    scope: Sequence[str],
    baseline_path: Path | None,
    writer: EventWriter,
    reader: Callable[[], str] | object = None,
) -> str:
    """按策略处理待落盘改动，全程以事件表达。

    语义与终端模式完全一致，只是把「打印 diff 并问一句」换成了
    「发 diff 与 await，再从 stdin 读一行」。**两条路径的判定必须一样**，
    否则同一个策略在终端和网页里表现不同——那种差异不会报错，
    只会让人困惑「为什么这里自动、那里不自动」。
    """
    changes = pending.items()
    if not changes:
        return "none"

    for change in changes:
        writer.emit(DIFF, path=change.path, text=change.diff)

    if policy == AUTO and not out_of_scope([c.path for c in changes], scope):
        written = apply_with_audit(pending, baseline_path)
        writer.emit(CONFIRM, applied=True, auto=True, count=len(written))
        return "auto"

    writer.emit(AWAIT, count=len(changes))
    source = reader if reader is not None else sys.stdin
    answer = (source.readline() or "").strip().lower()
    apply = answer in ("y", "yes")
    writer.emit(CONFIRM, applied=apply, count=len(changes))
    if apply:
        if baseline_path is not None:
            save_baseline(baseline_path, pending.baseline())
        pending.apply()
        return "confirmed"
    pending.discard()
    return "declined"
