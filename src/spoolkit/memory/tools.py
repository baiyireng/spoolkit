"""把记忆能力包装成工具。

热记忆是每轮自动注入的，模型直接看得到，不需要主动查。但冷记忆（全部历史）
必须按需检索——没有这个工具，提示词里就不能说「你可以查历史记忆」，
否则那是一条模型做不到的指令，比不写更糟。

`search_history` 是**另一种**数据：它查的不是蒸馏过的记忆，而是原样的
聊天往来。聊天往来默认不进上下文（短上下文正是靠不塞历史才成立的），
所以「这个问题是从哪来的」只能由它主动去取——两个工具一起挂着，模型才
分得清「过去的结论」（recall）与「当时那句话」（search_history）。

它的作用域**默认只到当前会话**（`resolve_scope`）：跨会话要显式
`session="*"`，那一次结果里每条都标出来源会话。默认跨会话会让"我在 A 里
说的话"被 B 里的一次检索带出来，而人并没有答应过这件事。

`remember` 是反方向的那一个：把任务**中途**的结论写进去。它不自己判一条
记忆值不值得记——判据、脱敏、去重、幂等、冲突判定都在 `semantic.write_memory`
（写入口只有一处），这里只负责把参数递过去、把结论说成人话。
"""

import hashlib
from pathlib import Path
from typing import Callable

from spoolkit.context import templates as T
from spoolkit.kernel import sessions as kernel_sessions
from spoolkit.memory.semantic import (
    Semantic,
    WriteRequest,
    WriteResult,
    write_memory,
)
from spoolkit.memory.session import MemorySession
from spoolkit.memory.transcript import (
    USER,
    no_hits_text,
    parse_moment,
    render_history_hits,
    resolve_scope,
)
from spoolkit.tools.types import ToolResult, ToolSpec


def _recall(session: MemorySession, args: dict) -> ToolResult:
    query = args["query"]
    limit = args.get("limit", 5)
    if limit < 1:
        return ToolResult(ok=False, content="limit 必须大于等于 1")

    found = session.recall(query, limit=limit)
    if not found:
        # 明确说「没找到」而不是返回空串：小模型在信息不足时倾向于编造，
        # 给它一个可用的「不知道」出口，能减少幻觉。
        return ToolResult(ok=True, content="没有找到相关的历史记忆。")
    return ToolResult(ok=True, content=found)


def recall_spec(session: MemorySession) -> ToolSpec:
    """检索历史记忆：过去的结论、决策与失败教训。"""
    return ToolSpec(
        name="recall",
        description="按关键词检索历史记忆里的结论、决策与失败教训",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=lambda args: _recall(session, args),
        brief="检索历史记忆",
        group="记",
    )


def _search_history(
    session: MemorySession,
    args: dict,
    *,
    root: Path,
    default_limit: int,
    max_limit: int,
    snippet_chars: int,
) -> ToolResult:
    """校验参数、检索、渲染出处。失败一律走 ok=False，不抛异常。"""
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolResult(ok=False, content="query 不能是空的：给一个关键词。")

    limit = args.get("limit", default_limit)
    if limit < 1:
        return ToolResult(ok=False, content="limit 必须大于等于 1")
    if limit > max_limit:
        # 上限不是「小气」：一次多给几条，历史就整段进了上下文。
        return ToolResult(
            ok=False,
            content=f"limit 最多 {max_limit} 条（历史片段整段占上下文）。要更多就分开查。",
        )
    if snippet_chars < 1:
        return ToolResult(ok=False, content="片段长度必须是正数")

    session_id = str(args.get("session", "") or "").strip()
    # 默认**当前会话**：没给 `session` 就是这一条，不再是整个工作区。
    # 跨会话是一次显式动作（`session="*"`）——历史消息的查看范围默认收到
    # 会话一级，是用户点名的要求；工作区边界不动（跨工作区依旧不做）。
    scope = resolve_scope(session_id, session.session_id)
    cross_session = not scope.session
    try:
        since = (
            parse_moment(args["since"], label="since") if args.get("since") else None
        )
        until = (
            parse_moment(args["until"], label="until") if args.get("until") else None
        )
    except ValueError as exc:
        return ToolResult(ok=False, content=str(exc))

    hits = session.search_history(
        query,
        limit=limit,
        snippet_chars=snippet_chars,
        session=scope.session,
        since=since,
        until=until,
    )
    if not hits:
        return ToolResult(
            ok=True,
            content=no_hits_text(
                query,
                scope_said=scope.description,
                cross_session=cross_session,
            ),
        )
    titles = _titles(root) if cross_session else None
    return ToolResult(
        ok=True,
        content=render_history_hits(
            hits,
            titles,
            cross_session=cross_session,
            scope_said=scope.description,
        ),
    )


def _titles(root: Path) -> dict[str, str]:
    """会话 id → 标题。标题在 `.agent/sessions/<id>/meta.json` 里，不在库里。

    只在**跨会话**且真有命中时才读：只看当前会话时那条的标题是多余的（模型已经
    知道自己在哪条会话里），而每次检索都去翻一遍所有会话目录是白花的钱。
    """
    try:
        return {
            item.id: item.title
            for item in kernel_sessions.list_sessions(root, include_archived=True)
            if item.title
        }
    except Exception:  # 标题只是锦上添花，读不到就不给——不该因此让检索失败
        return {}


def search_history_spec(
    session: MemorySession,
    root: Path,
    *,
    default_limit: int,
    max_limit: int,
    snippet_chars: int,
) -> ToolSpec:
    """检索聊天往来（历史消息）：判断「这个问题是从哪来的」。

    名字取 `search_history`：与 `search_code` 同族，名字即表明「在历史里搜」
    而不是「翻记忆」——后者已经有 `recall` 了。三个数字都是标定值，从
    `limits.py` 传进来，不在工具里写死。

    作用域默认收在**当前会话**（`session.session_id`）：跨会话要显式
    `session="*"`，那一次结果里每条都会标出来源会话。
    """

    def handler(args: dict) -> ToolResult:
        return _search_history(
            session,
            args,
            root=root,
            default_limit=default_limit,
            max_limit=max_limit,
            snippet_chars=snippet_chars,
        )

    return ToolSpec(
        name="search_history",
        description=(
            "按关键词检索历史消息（聊天往来），返回片段与会话/时间/角色出处。"
            "**默认只查当前会话**；要跨会话得显式给 session=\"*\"（本工作区的"
            "全部会话），那时结果里会标出每条来自哪条会话。历史默认不进上下文，"
            "要判断「这个问题是从哪来的」时才查它"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "关键词；多个词用空格分开，命中词多的排前面",
                },
                "limit": {
                    "type": "integer",
                    "description": f"最多几条（默认 {default_limit}，上限 {max_limit}）",
                },
                "session": {
                    "type": "string",
                    "description": (
                        "作用域：不给 = **只查当前会话**；"
                        '"*" = 本工作区的全部会话（跨会话，结果会标来源）；'
                        "别的值 = 只看这一条会话"
                    ),
                },
                "since": {
                    "type": "string",
                    "description": "只看这个时间起（含）：2026-09-27 或 2026-09-27 14:03",
                },
                "until": {
                    "type": "string",
                    "description": "只看这个时间止（含）；只给日期按当天 00:00 算",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
        brief="检索历史消息",
        group="记",
    )


# ---- remember：模型可调用的**中途**写入口（P1 / T10，设计稿 §5.2）------------
#
# 三条纪律，都在下面代码里可查：
#
# 1. **只走写入口**：判据（J1–J5）、脱敏、去重、幂等、冲突判定**一份都不在这里**
#    ——全部由 `semantic.write_memory` 给（§5.3「判据只许有这一份」）。工具层只做
#    两件事：把参数变成 `WriteRequest`，把 `WriteResult` 渲染成模型能照做的中文。
#    源码级用例 `test_工具源码只走write_memory不直插` 钉住"以后多一个直插口就红"。
# 2. **不碰热记忆**：`memory.md` 是注入层的产物（决议 6：仍可手改），这个工具只写
#    `semantic`。用例 `test_已有热记忆文件时一个字节都不改` 钉住这条。
# 3. **拒绝不是异常**：五条拒绝路径都是 `ToolResult(ok=False, content=…)`，理由
#    **逐字复用** `WriteResult.reason`，另加一句「下一步怎么办」——小模型知道为什么
#    没记住，比静默丢弃或静默记住都好（§5.2 末段）。

# 结论的分类标签：**从 `WriteResult.skipped` 派生**，不是在这里再判一遍（判据仍然
# 只有写入口那一份）。标签只在提示词/结果里说人话，不影响任何落库行为。
_LABELS: dict[str, tuple[str, str]] = {
    "denied": (
        "判据不过",
        "按理由把这条改成跨任务仍有用的一句话再写一次；改不动就别记它。",
    ),
    "redacted": ("脱敏", "密钥形状的值不许落库：换成占位符之后重写。"),
    "duplicate": (
        "重复",
        "不用重写：库里已有同一条（只记了一次使用）；要更新就换一种说法写，"
        "它会走取代链。",
    ),
    "conflict": (
        "冲突",
        "两条都留着、标了待复核：先确认哪条对，再写一条把它取代掉（旧行不删）。",
    ),
}
# 账本（③ 幂等）挡住的那一条：与"内容重复"（④ 判重）不是一回事，理由也不一样。
_LABEL_SAME_ROUND = (
    "同一轮",
    "同一轮里同一条只写一次：下一轮再写，或先把这一轮要说的话整理成一条。",
)
# J2（缺出处）单独给一个标签：它是判据里**唯一**一条"改一个字段就能过"的，
# 标签直接从 `judge` 给的理由前缀读（判据还是 gate.py 那一份，这里只取名）。
_LABEL_NO_EVIDENCE = (
    "缺出处",
    "补上出处（file:line / 命令 / 工具输出摘要）再写一次。",
)
# `scope` 的取值（设计稿 §4.3 第 1 块的枚举）。`machine` 在 P1 一律拒绝。
_REMEMBER_SCOPES: tuple[str, ...] = ("session", "workspace", "machine")
_REMEMBER_KINDS: tuple[str, ...] = (
    "fact",
    "preference",
    "decision",
    "lesson",
    "skill",
)


def _turn_marker(session: MemorySession) -> str:
    """这一轮的标识：本会话最后一条**用户**消息的 id；取不到就返回空串。

    为什么取用户消息而不是"最后一条消息"：一次运行里用户消息在开跑前落库、
    assistant 在收尾才写（`cli/commands/chat.py`、`run.py`），所以它整轮不变；
    万一以后中途也开始记消息，取用户消息这一条仍然稳。
    """
    row = session._conn.execute(
        "SELECT id FROM transcript WHERE session_id = ? AND role = ?"
        " ORDER BY id DESC LIMIT 1",
        (session.session_id, USER),
    ).fetchone()
    return "" if row is None else str(row["id"])


def _round_key(session: MemorySession, text: str) -> str:
    """这一条在**这一轮**里的幂等键（§5.3 ③ 的 `sha1(...|step)` 形态）。

    末位（`step` = "这一轮里的哪一步"）用**这一条的内容**，理由与
    `archive._round_key` 逐条相同：整轮共用一个键，同一轮里的第二条会被
    `idx_write_round` 当成"重跑"静默挡掉（那是丢条目，不是幂等）。

    中间那段是轮次标识（`_turn_marker`）：**取不到就返回空串**，
    `write_memory` 会退回一次性键 → ③ 不参与，只剩判重（④）那条路。
    这是有意的：没有轮次就不断言"同一轮"（那句话是关于轮次的）。
    """
    marker = _turn_marker(session)
    if not marker:
        return ""
    raw = f"{session.session_id}|remember|{marker}|{text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _render_write(result: WriteResult) -> str:
    """把写入口的结论渲染成模型能照做的一段中文（理由逐字复用，不另写判据）。"""
    if result.written:
        pair = _LABELS.get(result.skipped)
        if pair is None:  # 正常写入：没有 skipped
            return f"已记住：{result.reason}"
        return f"已记住（{pair[0]}）：{result.reason}\n下一步：{pair[1]}"
    if result.skipped == "duplicate" and result.duplicate_of is None:
        # 账本挡住的：库里没有对应的既有行，所以这不是"内容重复"。
        pair = _LABEL_SAME_ROUND
    elif result.skipped == "denied" and result.reason.startswith("J2"):
        pair = _LABEL_NO_EVIDENCE
    else:
        pair = _LABELS.get(result.skipped) or _LABELS["denied"]
    return f"没记住（{pair[0]}）：{result.reason}\n下一步：{pair[1]}"


def _remember(
    session: MemorySession,
    args: dict,
    *,
    overrides: dict | None,
    decide: Callable[[Semantic, WriteRequest], str] | None,
) -> ToolResult:
    """把一次工具调用变成一次 `write_memory`。失败一律走 `ok=False`，不抛异常。"""
    text = str(args.get("text", "") or "").strip()
    evidence = str(args.get("evidence", "") or "")
    kind = str(args.get("kind", "") or "").strip() or "fact"
    trigger = str(args.get("trigger", "") or "")
    scope = str(args.get("scope", "") or "").strip() or "workspace"
    if scope == "machine":
        # **不静默降级成 workspace**（决议 8 / §13.2：工作区是信任边界）：
        # 机器级记忆要显式授权，P1 不做，说清并让模型改范围重写。
        return ToolResult(
            ok=False,
            content=(
                "没记住（scope='machine' 在 P1 不做）：记忆以工作区为信任边界，"
                "跨工作区要显式授权（设计稿 §13.2/§8.1），这里不会把它当成 "
                "workspace 收下。下一步：改成 scope='workspace'（本工作区）再写。"
            ),
        )

    result = write_memory(
        session._conn,
        WriteRequest(
            kind=kind,
            text=text,
            evidence=evidence,
            trigger=trigger,
            scope=scope,
            # session 级带上会话 id（设计稿 §4.3：scope_id 是"session 级时为
            # 会话 id，否则 ''"）。
            scope_id=session.session_id if scope == "session" else "",
            # 出处这一列写清是工具这条路（账本与审计都按它归因）。
            source="tool:remember",
            round_key=_round_key(session, text),
        ),
        decide=decide,
        overrides=overrides,
    )
    return ToolResult(ok=result.written, content=_render_write(result))


def remember_spec(
    session: MemorySession,
    *,
    overrides: dict | None = None,
    decide: Callable[[Semantic, WriteRequest], str] | None = None,
) -> ToolSpec:
    """记下一条跨任务仍有用的事实（模型可调用的中途写入口）。

    与 `recall` / `search_history` 的分工：那两条是"取出来"，这条是"写进去"。
    写入走的是与自动蒸馏**同一套门禁**（`semantic.write_memory`），所以工具层
    不需要、也不许有第二份判据；被拒时把写入口给的理由原样交回，让模型知道
    "为什么没记住"。

    `decide` 是冲突判定（§5.3 ⑤）的注入点，与 `write_memory(decide=…)` 同一个
    口子：P1 的装配层没有"独立上下文的判定调用"（`cli/runtime.py` 不传），所以
    工具路径默认走"两条都 active + needs_review"那一支——**不静默选一个**。

    参数里 `text` 与 `evidence` 必填；`kind` 默认 `fact`，`trigger` 给教训/技能，
    `scope` 默认 `workspace`。热记忆文件不由它写（决议 6）。
    """

    def handler(args: dict) -> ToolResult:
        return _remember(
            session, args, overrides=overrides, decide=decide
        )

    return ToolSpec(
        name="remember",
        description=T.REMEMBER_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "一句话：跨任务仍有用的事实/偏好/决策/教训",
                },
                "evidence": {
                    "type": "string",
                    "description": "出处：file:line / 命令 / 工具输出摘要",
                },
                "kind": {
                    "type": "string",
                    "enum": list(_REMEMBER_KINDS),
                    "description": "选填，默认 fact；lesson/skill 还要给 trigger",
                },
                "trigger": {
                    "type": "string",
                    "description": "什么时候该想起它（lesson/skill 必填）",
                },
                "scope": {
                    "type": "string",
                    "enum": list(_REMEMBER_SCOPES),
                    "description": "选填，默认 workspace；machine 在 P1 被拒",
                },
            },
            "required": ["text", "evidence"],
            "additionalProperties": False,
        },
        handler=handler,
        brief=T.REMEMBER_BRIEF,
        group="记",
    )


__all__ = ["recall_spec", "search_history_spec", "remember_spec"]
