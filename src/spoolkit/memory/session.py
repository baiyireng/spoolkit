"""记忆会话。

把记忆的读写收敛成一个对象，主循环只需要调用它，不需要知道
热记忆文件、冷记忆库和容量规则的存在。
"""

import sqlite3
from pathlib import Path
from typing import Sequence

from spoolkit.agent.state import TaskState
from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.memory.archive import ArchiveResult, archive_task
from spoolkit.memory.hot import hot_budget, read_hot, render_hot, trim_to_budget
from spoolkit.memory.store import search_episodes, search_memories, touch_memory


class MemorySession:
    """一次会话的记忆读写入口。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        hot_path: Path,
        counter: TokenCounter,
        context_window: int,
        session_id: str = "default",
        overrides: dict | None = None,
    ) -> None:
        self._conn = conn
        self._hot_path = hot_path
        self._counter = counter
        self._window = context_window
        self.session_id = session_id
        # 热记忆的占比是能力标定值：装配层和裁剪层必须从同一个出处取。
        self._overrides = overrides or {}

    def hot_text(self) -> str:
        """返回本轮应当注入的热记忆文本，已按容量裁剪。"""
        sections = read_hot(self._hot_path)
        if not sections:
            return ""
        kept, _ = trim_to_budget(
            sections, self._counter, hot_budget(self._window, overrides=self._overrides)
        )
        if not kept:
            return ""
        return render_hot(kept).strip()

    def recall(self, query: str, limit: int = 5) -> str:
        """按关键词从冷记忆与历史事件里召回，附来源便于追溯。"""
        hits = search_memories(self._conn, query)[:limit]
        # 记一次使用：没有这一步，打分里的「使用频率」永远是初始值，
        # 那条维度等于不存在。
        for item in hits:
            touch_memory(self._conn, item.id)
        lines = [
            f"[{item.kind}] {item.text}（来源 {item.source or '未标注'}）"
            for item in hits
        ]

        # 事件表以前只写不读，等于几百条历史躺在库里没人用。
        # 它回答的是「上次这类事做到哪、结果如何」，和记忆是互补的。
        events = search_episodes(self._conn, query, limit=limit)
        lines.extend(
            f"[事件] {row['task']} —— {row['summary']}（结果 {row['outcome']}）"
            for row in events
        )
        return "\n".join(lines)

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
            overrides=self._overrides,
        )
