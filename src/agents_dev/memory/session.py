"""记忆会话。

把记忆的读写收敛成一个对象，主循环只需要调用它，不需要知道
热记忆文件、冷记忆库和容量规则的存在。
"""

import sqlite3
from pathlib import Path
from typing import Sequence

from agents_dev.agent.state import TaskState
from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.memory.archive import ArchiveResult, archive_task
from agents_dev.memory.hot import hot_budget, read_hot, render_hot, trim_to_budget
from agents_dev.memory.store import search_memories


class MemorySession:
    """一次会话的记忆读写入口。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        hot_path: Path,
        counter: TokenCounter,
        context_window: int,
        session_id: str = "default",
    ) -> None:
        self._conn = conn
        self._hot_path = hot_path
        self._counter = counter
        self._window = context_window
        self.session_id = session_id

    def hot_text(self) -> str:
        """返回本轮应当注入的热记忆文本，已按容量裁剪。"""
        sections = read_hot(self._hot_path)
        if not sections:
            return ""
        kept, _ = trim_to_budget(
            sections, self._counter, hot_budget(self._window)
        )
        if not kept:
            return ""
        return render_hot(kept).strip()

    def recall(self, query: str, limit: int = 5) -> str:
        """按关键词从冷记忆里召回，附来源编号便于追溯。"""
        hits = search_memories(self._conn, query)[:limit]
        if not hits:
            return ""
        return "\n".join(
            f"[{item.kind}] {item.text}（来源 {item.source or '未标注'}）"
            for item in hits
        )

    def archive(
        self,
        state: TaskState,
        outcome: str = "success",
        promote: Sequence[tuple[str, str]] = (),
    ) -> ArchiveResult:
        """归档一次已结束的任务。"""
        return archive_task(
            self._conn,
            self._hot_path,
            state,
            session_id=self.session_id,
            counter=self._counter,
            context_window=self._window,
            outcome=outcome,
            promote=promote,
        )

