"""长文聊天输出。第一版按节提交已经接受的正文。

不写 DocumentState，不写 BodyStore，不写文件，也不发 TaskEvent。
未经守卫的文本不能进 sink。要做逐字 delta，先要有丢弃和回滚协议。
"""

from __future__ import annotations

from typing import Protocol

from spoolkit.agent.document_state import DocumentState


class ChatSink(Protocol):
    """runner 的输出钩子。实现方只记录或展示，不改运行时状态。"""

    def start(self, state: DocumentState) -> None:
        """文档开始。state 是调用方传入的那一份。"""

    def section_start(self, index: int, title: str) -> None:
        """开始写这一节。此时还没有可提交的正文。"""

    def section_commit(self, index: int, body: str) -> None:
        """这一节的最终正文。只在守卫通过并写入 BodyStore 之后调用。"""

    def progress(self, phase: str, index: int) -> None:
        """runner phase 前进一步。index 为 -1 表示还没落在某一节。"""

    def complete(self, result: object) -> None:
        """全文通过。result 是 LongFormResult。"""

    def fail(self, reasons: tuple[str, ...]) -> None:
        """全文没有通过。reasons 是 check_bodies 的原因；收尾失败时为 ("finalize",)。"""


class RecorderSink:
    """测试用记录器。events 里只有名字和参数，不碰 state 或正文仓库。"""

    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def start(self, state: DocumentState) -> None:
        self.events.append(("start", state.phase, state.current_section is None))

    def section_start(self, index: int, title: str) -> None:
        self.events.append(("section_start", index, title))

    def section_commit(self, index: int, body: str) -> None:
        self.events.append(("section_commit", index, body))

    def progress(self, phase: str, index: int) -> None:
        self.events.append(("progress", phase, index))

    def complete(self, result: object) -> None:
        done = bool(getattr(result, "done", False))
        ending = str(getattr(result, "ending", ""))
        self.events.append(("complete", done, ending))

    def fail(self, reasons: tuple[str, ...]) -> None:
        self.events.append(("fail", reasons))
