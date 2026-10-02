"""任务归档。

一次任务结束时做三件事：把过程沉淀成事件写进冷记忆、把「试过但不通」
的方案固化成失败记忆、把提炼出来的候选条目写进热记忆并做容量下沉。

提炼这一步刻意保守：能确定性提取的（比如已排除的方案）就自动提取，
需要理解语义的交给独立的归纳会话（见 distill.py）。
把过程噪声灌进每轮都要注入的热记忆，比不记更糟。

写入侧的三条纪律在这里收口（设计稿 §5.3）：

- **先脱敏**：进 `memory` / `memory_fts` / `memory.md` 的每一段文本都先过
  `gate.sanitize`。密钥一旦进了热记忆就是每轮注入的长期泄漏。
- **再判重、后判据**：判重放在判据之前，是因为「同一条已经落过了」比
  「这条该不该收」更靠前——第二次归档同一条时不该再走一遍判据、更不该
  因为判据结果不同就多落一份（设计稿 §5.3 ④ 第 1 级；本仓计划 Task 2 的
  要点写作"先判据、再判重"，落地口径以本文件与用例为准）。
- **判据不过 = 降级，不是丢弃**：不过的条目写进 `memory.kind='episode'`，
  理由记在 `ArchiveResult.downgraded` 里；任务结论另有 `episode.summary` 兜底。
- **同一事实只落一次**：热记忆文件是整份重写的，`_merge`（distill）只管单次
  蒸馏分治内的合并，跨任务/跨蒸馏的逐字重复要靠这里判（`ArchiveResult.duplicates`）。
  判重是**精确级**：`gate.content_hash` 相同才算同一条，近似/语义去重属 P1。
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from spoolkit import limits
from spoolkit.agent.state import TaskState
from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.memory.gate import content_hash, judge, normalize_policy, sanitize
from spoolkit.memory.hot import hot_budget, read_hot, trim_to_budget, write_hot
from spoolkit.memory.lessons import record_lesson
from spoolkit.memory.store import add_episode, add_memory, touch_memory


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
    # 命中既有条目（归一化后同一条）而被跳过的文本：第二次归档同一事实时不再新增。
    duplicates: list[str] = field(default_factory=list)


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


def _known_hashes(conn: sqlite3.Connection, overrides: dict | None) -> dict[str, int]:
    """库里既有条目的指纹 → 第一条同指纹的 id（用于 touch 既有条目）。

    P0 的 `memory` 表没有 `content_hash` 列（原则一：不改旧表），所以指纹在
    Python 侧算。库里行数在「一个工作区一个库」这个规模上不会成为瓶颈；
    真要按指纹查，是 P1 的 `semantic.content_hash` 唯一索引那一条。
    """
    known: dict[str, int] = {}
    rows = conn.execute("SELECT id, text FROM memory").fetchall()
    for row in rows:
        digest = content_hash(row["text"], overrides)
        if digest and digest not in known:
            known[digest] = row["id"]
    return known


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

    # 判重与判据共用一份归一化口径：取一次，别在同一轮里读两遍登记表。
    policy = normalize_policy(overrides)
    # 既有条目也要过一遍：热记忆文件是整份重写的，写入侧不过闸等于把
    # 文件里原有的密钥又抄一遍回去。判重也顺带在这一次读里做（既有内容
    # 就在手里，检查它不需要任何新依赖——设计稿 §2.6）。
    sections = {
        kind: [_clean(item, redacted) for item in items]
        for kind, items in read_hot(hot_path).items()
    }
    # 任务已结束，进行中的条目没有继续留在热记忆里的理由。
    sections.pop("doing", None)
    known: dict[str, int] = {
        digest: index
        for kind, items in sections.items()
        for index, item in enumerate(items)
        if (digest := content_hash(item, overrides, policy))
    }
    known.update(_known_hashes(conn, overrides))

    promoted_texts: list[str] = []
    downgraded: list[tuple[str, str]] = []
    duplicates: list[str] = []
    hot_item_chars = int(limits.resolve("hot_item_chars", overrides)[0])
    for entry in promote:
        kind = entry[0]
        text = _clean(entry[1], redacted).strip()
        trigger = _clean(entry[2], redacted).strip() if len(entry) > 2 else ""
        if not text:
            continue
        digest = content_hash(text, overrides, policy)
        if digest and digest in known:
            # 结构性去重（设计稿 §5.3 ④ 第 1 级）：同一条不再新增，不重复上报。
            # 库里已有行就 touch 记一次使用（store.touch_memory）；热记忆没有行
            # 可 touch，所以只在 duplicates 里记一次，不凭空造行。
            existing = known[digest]
            if existing is not None:
                touch_memory(conn, existing)
            duplicates.append(text)
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
            if digest:
                known[digest] = None
            continue
        if len(text) > hot_item_chars:
            # 单条超过 hot_item_chars（设计稿 §4.3 定案 200 字）不进热记忆：
            # 一条占半屏的条目会把同节里几条真事实挤出去。与判据不同的是
            # **不落库**——它没被拒，只是太长了，`episode.summary` 里还在。
            downgraded.append(
                (
                    text,
                    f"too_long 超过 hot_item_chars={hot_item_chars}："
                    "热记忆只收短条目（设计稿 §4.3 定案的 hot_item_chars）",
                )
            )
            if digest:
                known[digest] = None
            continue
        if kind == "lesson":
            # 教训不进热记忆文件：它需要触发词、置信度和统计，那是数据库形状的。
            # 塞进 Markdown 就再也拿不出来了，也就永远推不到该出现的场景。
            record_lesson(conn, text, trigger=trigger, source=f"ep#{episode_id}")
            promoted_texts.append(text)
            if digest:
                known[digest] = None
            continue
        sections.setdefault(kind, []).append(text)
        promoted_texts.append(text)
        if digest:
            known[digest] = None

    kept, demoted = trim_to_budget(
        sections,
        counter,
        hot_budget(context_window, overrides=overrides),
        overrides=overrides,
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
        duplicates=duplicates,
    )
