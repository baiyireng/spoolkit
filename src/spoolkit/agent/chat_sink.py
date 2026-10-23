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
        """这一节首次提交的最终正文。只在守卫通过并写入 BodyStore 之后调用。"""

    def section_replace(self, index: int, body: str) -> None:
        """这一节已提交过，校验重开后替换成新的最终正文。不要追加。"""

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

    def section_replace(self, index: int, body: str) -> None:
        self.events.append(("section_replace", index, body))

    def progress(self, phase: str, index: int) -> None:
        self.events.append(("progress", phase, index))

    def complete(self, result: object) -> None:
        done = bool(getattr(result, "done", False))
        ending = str(getattr(result, "ending", ""))
        self.events.append(("complete", done, ending))

    def fail(self, reasons: tuple[str, ...]) -> None:
        self.events.append(("fail", reasons))


class CollectingChatSink:
    """只留下已提交或已替换的节。complete 之后按节号拼出正文，不写界面。"""

    def __init__(self) -> None:
        self._titles: dict[int, str] = {}
        self._sections: dict[int, str] = {}
        self._done = False

    def start(self, state: DocumentState) -> None:
        del state

    def section_start(self, index: int, title: str) -> None:
        self._titles.setdefault(index, title)

    def section_commit(self, index: int, body: str) -> None:
        self._sections[index] = body

    def section_restore(self, index: int, body: str, title: str = "") -> None:
        """恢复已完成节，不算新写一遍。"""
        if title:
            self._titles.setdefault(index, title)
        self._sections[index] = body

    def section_replace(self, index: int, body: str) -> None:
        self._sections[index] = body

    def progress(self, phase: str, index: int) -> None:
        del phase, index

    def complete(self, result: object) -> None:
        del result
        self._done = True

    def fail(self, reasons: tuple[str, ...]) -> None:
        del reasons
        self._done = False

    def text(self) -> str:
        """完成之后按节号排列。没完成时是空串，避免半截正文流出去。"""
        if not self._done:
            return ""
        parts: list[str] = []
        for index in sorted(self._sections):
            body = self._sections[index].strip()
            title = self._titles.get(index, "")
            if title:
                parts.append(f"## {title}\n\n{body}")
            elif body:
                parts.append(body)
        return "\n\n".join(parts)
