"""内核侧的事件输出器。

`--events` 模式下，stdout 上只应该有 JSON 行——不夹杂任何散文。
散文会被解析器忽略，但录下来的文件也会变脏，调试时难读。

每条都 flush：不 flush 的话输出攒在缓冲区里，UI 那边看起来像卡住了，
而你会先去怀疑网络。
"""

import sys
from typing import Any, TextIO

from agents_dev.web.protocol import Event


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

