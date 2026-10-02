"""任务归档。

一次任务结束时做三件事：把过程沉淀成事件写进冷记忆、把「试过但不通」
的方案固化成失败记忆、把提炼出来的候选条目写进热记忆并做容量下沉。

提炼这一步刻意保守：能确定性提取的（比如已排除的方案）就自动提取，
需要理解语义的交给独立的归纳会话（见 distill.py）。
把过程噪声灌进每轮都要注入的热记忆，比不记更糟。

**T7 接线起：提升条目一律交给 `semantic.write_memory`**（设计稿 §5.3 的五道门禁：
脱敏 → J1–J5 → 幂等 → 去重 → 冲突）。本模块**不再**把提升条目直写旧 `memory`
表（`store.add_memory`）。理由与"教训不进 FTS"（P0 缺陷 4）同一条：门禁只有长在
唯一写入口上才不是空转，而"每个调用点各自记得过一遍"迟早会漏一个。旧 `memory`
表只剩两个**已定案**的降级/下沉出口——`_episode_sink`（`memory.kind='episode'`）
与 `state.excluded` 那条失败记忆——读路径（`search_memories` / `match_lessons` /
`search_episodes`）一字未动，旧数据照查。

写入侧的四条纪律在这里收口（设计稿 §5.3）：

- **先脱敏**：进 `memory` / `memory_fts` / `memory.md` / `semantic` 的每一段文本
  都先过 `gate.sanitize`（本模块的唯一出口是 `_clean`）。密钥一旦进了热记忆就是
  每轮注入的长期泄漏。`write_memory` 自己还会再过一遍（写入口不依赖调用方），
  但本模块的落点不止数据库那一个，所以这一道不能省。
- **判重有两层，前一层在判据之前**：本层按 `content_hash` 查热记忆文件 / 旧
  `memory` 表 / `semantic` 既有行（「同一条已经落过了」比「这条该不该收」更靠前，
  设计稿 §5.3 ④ 第 1 级）；`write_memory` 那一层再做一次（轮次幂等 + `semantic`
  的部分唯一索引）。两层都只认**精确级**：归一化后相同才算同一条，近似/语义
  去重（§5.3 ④ 第 2 级）仍未落地，本模块不假装做过。
- **判据不过 = 降级，不是丢弃**：写入口拒收的条目由本层写进
  `memory.kind='episode'`，理由记在 `ArchiveResult.downgraded` 里；任务结论另有
  `episode.summary` 兜底。
- **同一事实只落一次**：热记忆文件是整份重写的，`_merge`（distill）只管单次
  蒸馏分治内的合并，跨任务/跨蒸馏的逐字重复靠上面那层判重
  （`ArchiveResult.duplicates`）。

**提升条目的两种形状**（`promote` 的每一项）：

- 2/3 元组 `(kind, text[, trigger])` = P0 形状，**没有 `evidence` 这一列**：
  本层用 `ep#<episode_id>` 兜底——条目就是本轮 episode 的产物，P0 写进旧表的
  `source` 也是这么写的（`gate.judge` 的 `evidence` 三态里，`None` 正表示
  "这条路径没有这一列"）；
- 4 元组 `(kind, text, trigger, evidence)` = T7 起的形状，`evidence` 由蒸馏调用
  给出（`distill.DISTILL_SCHEMA` 的必填列）。**空串就是"给不出证据"**，原样交给
  `write_memory` 过 J2 → 拒 → 降级为 episode：既不静默变成 `fact`，也不丢。
"""

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from spoolkit import limits
from spoolkit.agent.state import TaskState
from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.memory.gate import content_hash, normalize_policy, sanitize
from spoolkit.memory.hot import (
    SECTION_TITLES,
    hot_budget,
    read_hot,
    trim_to_budget,
    write_hot,
)
from spoolkit.memory.lessons import record_lesson
from spoolkit.memory.semantic import (
    WriteRequest,
    get_semantic,
    touch_semantic,
    write_memory,
)
from spoolkit.memory.store import Memory, add_episode, add_memory, touch_memory

# `write_memory` 的 `source`（§4.3 第 5 块枚举里的一个）：本模块唯一的上游。
SOURCE_DISTILL = "distill"

# 蒸馏条目的置信度与重要度：归档时这两个维度**没有可用信号**（模型没给、
# 也没有使用统计），所以取表里的中性值 0.5，不假装有依据。五维重排是另一件事。
ENTRY_CONFIDENCE = 0.5
ENTRY_IMPORTANCE = 0.5


@dataclass(frozen=True)
class WriteOutcome:
    """一条提升条目在写入路径上的结论（`write_memory` 那份结论的可读副本）。

    **同一份结论也落库**：`memory_write`（`written` / `skipped` / `detail`）与
    `memory_audit`（`reason`）。这里这一份是给调用方与人的报告用的，
    不是"只活在前端"的那一份。
    """

    text: str
    written: bool
    # 写成功 = `kind:id`（§5.4 的稳定引用）；判重命中 = 既有条目的引用名。
    ref: str = ""
    # `""`（正常写入）| `duplicate` | `denied` | `redacted` | `conflict`
    # （写入口那一套），本层再加一个 `too_long`（过了门禁但不进热记忆文件）。
    skipped: str = ""
    # 可以直接打印给模型/人看的一句中文。
    reason: str = ""
    # 判重命中时指向既有条目（`kind:id` / `memory:<id>` / `hot:<kind>`）。
    duplicate_of: str = ""
    superseded_id: int | None = None


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
    # 每条提升条目的写入结论（written / skipped / duplicate_of / 拒绝理由）。
    outcomes: list[WriteOutcome] = field(default_factory=list)

    @property
    def written(self) -> list[str]:
        """写进 `semantic` 的条目引用名 `kind:id`（§5.4 的稳定引用）。

        与 `promoted` 不是一回事：`promoted` 是"进了热记忆文件/教训库"的那一批
        （P0 口径），`written` 是"过了五道门禁、落在 `semantic` 里"的那一批——
        太长而不进热记忆的条目属于后者、不属于前者。
        """
        return [outcome.ref for outcome in self.outcomes if outcome.written]


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


@dataclass(frozen=True)
class _Hit:
    """既有条目的落点：在哪一层 + 可读引用名 + 可 touch 的行 id。

    热记忆文件里的条目**没有行 id**（它是纯 Markdown），所以 `row_id` 是 `None`。
    P0 把热记忆命中记成"list 下标"、随后拿它去 `touch_memory`——那会碰到另一条
    无关的旧表行（`UPDATE memory WHERE id=<下标>`）。把来源显式标出来之后，
    这条"碰巧没碰错"的隐式正确就变成显式的正确。
    """

    layer: str
    ref: str
    row_id: int | None = None


def _hot_hashes(
    sections: dict[str, list[str]],
    overrides: dict | None,
    policy,
) -> dict[str, _Hit]:
    """热记忆文件里已有条目的指纹 → 那一节（文件里的条目没有行可 touch）。"""
    hits: dict[str, _Hit] = {}
    for kind, items in sections.items():
        for item in items:
            digest = content_hash(item, overrides, policy)
            if digest and digest not in hits:
                hits[digest] = _Hit("hot", f"hot:{kind}")
    return hits


def _legacy_hashes(
    conn: sqlite3.Connection, overrides: dict | None
) -> dict[str, _Hit]:
    """旧 `memory` 表里已有条目的指纹 → 那一行的 id。

    P0 的 `memory` 表没有 `content_hash` 列（原则一：不改旧表），所以指纹在
    Python 侧算。库里行数在「一个工作区一个库」这个规模上不会成为瓶颈；
    真要按指纹查，是 `semantic.content_hash` 唯一索引那一条。
    """
    hits: dict[str, _Hit] = {}
    rows = conn.execute("SELECT id, text FROM memory").fetchall()
    for row in rows:
        digest = content_hash(row["text"], overrides)
        if digest and digest not in hits:
            hits[digest] = _Hit("memory", f"memory:{row['id']}", row["id"])
    return hits


def _semantic_hashes(conn: sqlite3.Connection) -> dict[str, _Hit]:
    """`semantic` 里既有的 `active` 行（T7 接线起加的第三层）。

    为什么要它：同一轮的**重跑**（进程被杀后重收尾、`--resume`）在热记忆文件
    还没被重写时，前两层都是空的。有了这一层，重跑在判重阶段就有可读结论
    （不是靠"账本恰好还在"）；`write_memory` 的轮次账本与唯一索引仍是最后一道。
    """
    return {
        row["content_hash"]: _Hit(
            "semantic", f"{row['kind']}:{row['id']}", row["id"]
        )
        for row in conn.execute(
            "SELECT id, kind, content_hash FROM semantic WHERE status = 'active'"
        )
    }


def _touch_hit(conn: sqlite3.Connection, hit: _Hit) -> None:
    """命中既有条目时记一次使用（打分里的 `use` 维要它）。

    热记忆文件里的条目没有行可 touch（P0 的注释原话），所以那一层只报判重，
    不凭空造行。
    """
    if hit.row_id is None:
        return
    if hit.layer == "memory":
        touch_memory(conn, hit.row_id)
    elif hit.layer == "semantic":
        touch_semantic(conn, hit.row_id)


def _episode_sink(
    conn: sqlite3.Connection, episode_id: int, text: str, trigger: str = ""
) -> Memory:
    """降级/下沉的**唯一**出口：写 `memory.kind='episode'`（设计稿 §5.3 ② 的落点）。

    **这是本模块仅有的两个写旧表的出口之一**（另一个是 `state.excluded` 那条
    失败记忆）：`semantic` 的写入全部由 `write_memory` 负责，提升条目一条都
    不再直写旧表。为什么降级仍落旧表：`episode` 的既有读路径
    （`search_episodes` / `load_memory` 的召回）读的就是它，P0 已定案的降级
    落点也是被用例钉住的那一个；把它搬进 `semantic` 是迁移线的事（§9.3），
    不在本次接线面上。
    """
    return add_memory(
        conn,
        kind="episode",
        text=text,
        source=f"ep#{episode_id}",
        confidence=0.4,
        trigger=trigger,
    )


def _round_key(session_id: str, state: TaskState, final: str, text: str) -> str:
    """本轮归档里**这一条**的幂等键（§5.3 ③ 的 `sha1(...|step)` 形态）。

    末位（`step` 的语义 = "这一轮里的哪一步"）用**这一条的内容**，不是"整轮共用
    一个键"、也不是"按批内序号"：

    - 整轮共用一个键：一次蒸馏会产出多条，第二条起会被 `idx_write_round` 当成
      "同一轮重跑"**静默挡掉**——那是丢条目，不是幂等；
    - 按批内序号：同一轮重跑时模型把同一批换个顺序、或者换了其中一条内容，
      序号会把"确实不同的一条"也一起挡掉（用例 `test_去重口径可被工作区覆盖`
      就是这一形态：同一份 state 归档两次、第二条内容不同）。

    用内容做 `step` 之后：同一轮重跑同一批 → 逐条同一个键 → 逐条被挡（幂等）；
    同一轮里的不同条目 → 各有各的键。**跨轮次的同一事实不归它管**，那是判重
    （④）的事——这正是 `write_memory._adhoc_round_key` 注释里划的那条界线
    （幂等管轮次、判重管内容）。
    """
    raw = f"{session_id}|{state.task_id}|{state.goal}|{final}|{text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _audit_hot_demote(
    conn: sqlite3.Connection, kind: str, text: str, reason: str
) -> None:
    """热记忆层的去向记一行审计（§4.3 第 6 块）。

    为什么非记不可：`write_memory` 的账本说"收下了"，而这一条**没进**热记忆文件
    （太长，或者按容量被淘汰）。这段"账本说收下、文件里没有"的差如果不落库，
    就只剩进程里的报告能看——而报告随进程消失（本仓口径："结论要能被人看见，
    不要只在前端存着"）。
    """
    conn.execute(
        "INSERT INTO memory_audit(at, actor, action, target, before, after, reason)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            time.time(),
            SOURCE_DISTILL,
            "demote",
            f"hot:{kind}",
            "",
            text,
            reason,
        ),
    )
    conn.commit()


def _duplicate_ref(conn: sqlite3.Connection, semantic_id: int | None) -> str:
    """`WriteResult.duplicate_of`（一行 `semantic` 的 id）→ §5.4 的 `kind:id`。"""
    if semantic_id is None:
        return ""
    item = get_semantic(conn, semantic_id)
    return "" if item is None else f"{item.kind}:{item.id}"


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
    # 它**不**走 `write_memory`：`failure` 不在 `semantic.kind` 的枚举里
    # （fact|preference|decision|lesson|skill），而这条读路径（`list_memories` /
    # 教训的推送池）读的一直是旧表。它属于 P0 已定案的第二个出口，不是提升条目。
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
    # （T7 接线起 `doing` 再也进不来：它不是 `semantic.kind`，写入口直接拒它，
    # 这里不会把它 `setdefault` 回来——"不许顺手复活 doing 分节"。）
    sections.pop("doing", None)
    known: dict[str, _Hit] = {}
    known.update(_hot_hashes(sections, overrides, policy))
    known.update(_semantic_hashes(conn))
    # 旧表放最后：同一个指纹在多层都有时，让旧表那一层拿到这次 touch
    # （P0 的 `use_count` 语义在旧表上，`search_memories` 读的是它）。
    known.update(_legacy_hashes(conn, overrides))

    promoted_texts: list[str] = []
    downgraded: list[tuple[str, str]] = []
    duplicates: list[str] = []
    outcomes: list[WriteOutcome] = []
    hot_item_chars = int(limits.resolve("hot_item_chars", overrides)[0])
    for entry in promote:
        kind = entry[0]
        text = _clean(entry[1], redacted).strip()
        trigger = _clean(entry[2], redacted).strip() if len(entry) > 2 else ""
        # 4 元组 = 调用方给了 `evidence` 这一列（空串 = 给不出证据，J2 会拒）；
        # 2/3 元组 = P0 形状，没有这一列，用本轮 episode 引用兜底（见模块 docstring）。
        if len(entry) > 3:
            evidence = _clean(entry[3] or "", redacted).strip()
        else:
            evidence = f"ep#{episode_id}"
        if not text:
            continue
        digest = content_hash(text, overrides, policy)
        hit = known.get(digest) if digest else None
        if hit is not None:
            # 本层的结构性去重（设计稿 §5.3 ④ 第 1 级）：同一条不再新增，
            # 也不重复上报。库里已有行就 touch 记一次使用；热记忆里没有行可
            # touch，所以只记判重、不凭空造行。
            _touch_hit(conn, hit)
            duplicates.append(text)
            outcomes.append(
                WriteOutcome(
                    text=text,
                    written=False,
                    skipped="duplicate",
                    reason=f"同一条已经落过：命中 {hit.ref}，只记一次使用、不新增行",
                    duplicate_of=hit.ref,
                )
            )
            continue

        # 提升条目**唯一**的写点：五道门禁（脱敏 → J1–J5 → 幂等 → 去重 → 冲突）
        # 全在 `write_memory` 里，本层不预判、也不另写一套判据。
        result = write_memory(
            conn,
            WriteRequest(
                kind=kind,
                text=text,
                evidence=evidence,
                trigger=trigger,
                source=SOURCE_DISTILL,
                confidence=ENTRY_CONFIDENCE,
                importance=ENTRY_IMPORTANCE,
                round_key=_round_key(session_id, state, final, text),
            ),
            overrides=overrides,
        )
        if not result.written:
            duplicate_of = ""
            if result.skipped == "duplicate":
                # 写入口那一层的判重/幂等：既有行在 `semantic` 里，或者这一轮
                # 已经烧掉了（账本撞唯一索引）。两种情况都不是"丢"。
                duplicates.append(text)
                duplicate_of = _duplicate_ref(conn, result.duplicate_of)
            else:
                # 判据不过 = 降级为 episode，不是丢弃（§5.3 ②）：写入口只把
                # "这条被拒了、为什么"落进账本与 audit，降级出口在本层。
                row = _episode_sink(conn, episode_id, text, trigger)
                downgraded.append((text, result.reason))
                if digest:
                    known[digest] = _Hit("memory", f"memory:{row.id}", row.id)
            outcomes.append(
                WriteOutcome(
                    text=text,
                    written=False,
                    ref=result.ref,
                    skipped=result.skipped,
                    reason=result.reason,
                    duplicate_of=duplicate_of,
                    superseded_id=result.superseded_id,
                )
            )
            continue

        # 写成功。它在本层还有一次去向判定（热记忆文件 / 教训库 / 太长不收），
        # 结论与写入口的结论合成同一条给人看的记录。
        skipped = result.skipped
        reason = result.reason
        if len(text) > hot_item_chars:
            # 单条超过 hot_item_chars（设计稿 §4.3 定案 200 字）不进热记忆：
            # 一条占半屏的条目会把同节里几条真事实挤出去。它**已经过了五道门禁**，
            # 所以落在 `semantic` 里、检索得到，只是不进每轮注入的那一层；
            # 去向另记一行审计（否则"账本说收下、文件里没有"这段差只在报告里）。
            skipped = "too_long"
            reason = (
                f"too_long 超过 hot_item_chars={hot_item_chars}："
                "热记忆只收短条目（设计稿 §4.3 定案的 hot_item_chars）"
            )
            downgraded.append((text, reason))
            _audit_hot_demote(conn, kind, text, reason)
        elif kind == "lesson":
            # 教训不进热记忆文件：它需要触发词、置信度和统计，那是数据库形状的。
            # 塞进 Markdown 就再也拿不出来了，也就永远推不到该出现的场景。
            record_lesson(conn, text, trigger=trigger, source=f"ep#{episode_id}")
            promoted_texts.append(text)
        elif kind in SECTION_TITLES:
            sections.setdefault(kind, []).append(text)
            promoted_texts.append(text)
        else:
            # 热记忆文件里没有这个分节的标题（`skill` 就是）：写进 `sections`
            # 的话 `render_hot` 会**静默不渲染**它，而它照样白占 token 预算、
            # 挤掉同批里该留下的条目。它已经在 `semantic` 里了（检索得到），
            # 所以这里只是"不进文件"，不是丢。
            promoted_texts.append(text)
        outcomes.append(
            WriteOutcome(
                text=text,
                written=True,
                ref=result.ref,
                skipped=skipped,
                reason=reason,
                superseded_id=result.superseded_id,
            )
        )
        if digest:
            known[digest] = _Hit("semantic", result.ref, result.semantic_id)

    kept, demoted = trim_to_budget(
        sections,
        counter,
        hot_budget(context_window, overrides=overrides),
        overrides=overrides,
    )
    for text in demoted:
        _episode_sink(conn, episode_id, text)

    write_hot(hot_path, kept)
    return ArchiveResult(
        episode_id=episode_id,
        promoted=promoted_texts,
        demoted=demoted,
        downgraded=downgraded,
        redacted=redacted,
        duplicates=duplicates,
        outcomes=outcomes,
    )
