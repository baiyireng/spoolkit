"""任务归档。

一次任务结束时做三件事：把过程沉淀成事件写进冷记忆、把「试过但不通」
的方案固化成失败记忆、把提炼出来的候选条目写进热记忆并做容量下沉。

提炼这一步刻意保守：能确定性提取的（比如已排除的方案）就自动提取，
需要理解语义的交给独立的归纳会话（见 distill.py）。
把过程噪声灌进每轮都要注入的热记忆，比不记更糟。

写入侧的两条纪律在这里收口（设计稿 §5.3）：

- **先脱敏，再判据**：进 `memory` / `memory_fts` / `memory.md` 的每一段文本
  都先过 `gate.sanitize`。密钥一旦进了热记忆就是每轮注入的长期泄漏。
- **判据不过 = 降级，不是丢弃**：不过的条目写进 `memory.kind='episode'`，
  理由记在 `ArchiveResult.downgraded` 里；任务结论另有 `episode.summary` 兜底。
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from spoolkit import limits
from spoolkit.agent.state import TaskState
from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.memory.gate import judge, sanitize
from spoolkit.memory.hot import hot_budget, read_hot, trim_to_budget, write_hot
from spoolkit.memory.lessons import record_lesson
from spoolkit.memory.store import add_episode, add_memory


@dataclass
class ArchiveResult:
    """归档结果。"""

    episode_id: int
    promoted: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    # （被拒文本, 判据理由）：进了冷记忆，但没进热记忆。
    downgraded: list[tuple[str, str]] = field(default_factory=list)
    # （脱敏后的文本, 命中的形状）：值不落库，但「这里原本有个密钥」要留痕。
    redacted: list[tuple[str, str]] = field(default_factory=list)


def summarize(
    state: TaskState, final: str = "", max_final_chars: int = 400
) -> str:
    """把任务状态压成一句话，作为事件摘要。"""
    steps = "、".join(state.done) if state.done else "无记录"
    base = f"{state.goal}：已完成 {steps}"
    clipped = (final or "").strip()
    if not clipped:
        return base
    flat = " ".join(clipped.split())
    if len(flat) > max_final_chars:
        flat = flat[:max_final_chars] + "…"
    return f"{base}\n结论：{flat}"


def _clean(text: str, report: list[tuple[str, str]]) -> str:
    """写入前的脱敏，并把「命中了哪一类形状」记进报告（不回显值）。"""
    cleaned, shapes = sanitize(text)
    if shapes:
        report.append((cleaned, "命中密钥形状：" + "、".join(shapes)))
    return cleaned


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
    final: str = "",
) -> ArchiveResult:
    """归档一次已结束的任务。"""
    max_final_chars = int(limits.resolve("episode_final_chars", overrides)[0])
    # 报告里的一条 = 一次脱敏动作。整条归档只走这一个出口，避免「哪个调用点忘了过」。
    redacted: list[tuple[str, str]] = []
    episode_id = add_episode(
        conn,
        session_id=session_id,
        task=_clean(state.goal, redacted),
        summary=_clean(
            summarize(state, final=final, max_final_chars=max_final_chars), redacted
        ),
        outcome=outcome,
    )

    # 「已排除」是失败记忆里价值最高的一类：它直接防止同一种错误再犯。
    for excluded in state.excluded:
        add_memory(
            conn,
            kind="failure",
            text=_clean(excluded, redacted),
            source=f"ep#{episode_id}",
            confidence=0.6,
        )

    # 既有条目也要过一遍：热记忆文件是整份重写的，写入侧不过闸等于把
    # 文件里原有的密钥又抄一遍回去。
    sections = {
        kind: [_clean(item, redacted) for item in items]
        for kind, items in read_hot(hot_path).items()
    }
    # 任务已结束，进行中的条目没有继续留在热记忆里的理由。
    sections.pop("doing", None)

    promoted_texts: list[str] = []
    downgraded: list[tuple[str, str]] = []
    for entry in promote:
        kind = entry[0]
        text = _clean(entry[1], redacted).strip()
        trigger = _clean(entry[2], redacted).strip() if len(entry) > 2 else ""
        if not text:
            continue
        verdict = judge(kind, text, trigger, overrides=overrides)
        if not verdict.ok:
            # 判据不过 = 降级为 episode，不是丢弃：结论仍能在事件与检索里查到，
            # 但它不再每轮注入（设计稿 §5.3 ②）。
            add_memory(
                conn,
                kind="episode",
                text=text,
                source=f"ep#{episode_id}",
                confidence=0.4,
                trigger=trigger,
            )
            downgraded.append((text, verdict.reason))
            continue
        if kind == "lesson":
            # 教训不进热记忆文件：它需要触发词、置信度和统计，那是数据库形状的。
            # 塞进 Markdown 就再也拿不出来了，也就永远推不到该出现的场景。
            record_lesson(conn, text, trigger=trigger, source=f"ep#{episode_id}")
            promoted_texts.append(text)
            continue
        sections.setdefault(kind, []).append(text)
        promoted_texts.append(text)

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
        episode_id=episode_id,
        promoted=promoted_texts,
        demoted=demoted,
        downgraded=downgraded,
        redacted=redacted,
    )
