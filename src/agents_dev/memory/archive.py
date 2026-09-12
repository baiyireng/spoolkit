"""任务归档。

一次任务结束时做三件事：把过程沉淀成事件写进冷记忆、把「试过但不通」
的方案固化成失败记忆、把提炼出来的候选条目写进热记忆并做容量下沉。

提炼这一步刻意保守：能确定性提取的（比如已排除的方案）就自动提取，
需要理解语义的交给独立的归纳会话（见 distill.py）。
把过程噪声灌进每轮都要注入的热记忆，比不记更糟。
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from agents_dev.agent.state import TaskState
from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.memory.hot import hot_budget, read_hot, trim_to_budget, write_hot
from agents_dev.memory.lessons import record_lesson
from agents_dev.memory.store import add_episode, add_memory


@dataclass
class ArchiveResult:
    """归档结果。"""

    episode_id: int
    promoted: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)


def summarize(state: TaskState) -> str:
    """把任务状态压成一句话，作为事件摘要。"""
    steps = "、".join(state.done) if state.done else "无记录"
    return f"{state.goal}：已完成 {steps}"


def archive_task(
    conn: sqlite3.Connection,
    hot_path: Path,
    state: TaskState,
    session_id: str,
    counter: TokenCounter,
    context_window: int,
    outcome: str = "success",
    overrides: dict | None = None,
    promote: Sequence[tuple] = (),
) -> ArchiveResult:
    """归档一次已结束的任务。"""
    episode_id = add_episode(
        conn,
        session_id=session_id,
        task=state.goal,
        summary=summarize(state),
        outcome=outcome,
    )

    # 「已排除」是失败记忆里性价比最高的一类：它直接防止同一个坑踩第二次。
    for excluded in state.excluded:
        add_memory(
            conn,
            kind="failure",
            text=excluded,
            source=f"ep#{episode_id}",
            confidence=0.6,
        )

    sections = read_hot(hot_path)
    # 任务已结束，进行中的条目没有继续留在热记忆里的理由。
    sections.pop("doing", None)

    promoted_texts: list[str] = []
    for entry in promote:
        kind, text = entry[0], entry[1]
        trigger = entry[2] if len(entry) > 2 else ""
        if not text.strip():
            continue
        if kind == "lesson":
            # 教训不进热记忆文件：它需要触发词、置信度和统计，那是数据库形状的。
            # 塞进 Markdown 就再也拿不出来了，也就永远推不到该出现的场景。
            record_lesson(conn, text, trigger=trigger, source=f"ep#{episode_id}")
            promoted_texts.append(text.strip())
            continue
        sections.setdefault(kind, []).append(text.strip())
        promoted_texts.append(text.strip())

    kept, demoted = trim_to_budget(
        sections, counter, hot_budget(context_window, overrides=overrides)
    )
    for text in demoted:
        add_memory(
            conn,
            kind="episode",
            text=text,
            source=f"ep#{episode_id}",
            confidence=0.4,
        )

    write_hot(hot_path, kept)
    return ArchiveResult(
        episode_id=episode_id, promoted=promoted_texts, demoted=demoted
    )
