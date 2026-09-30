"""把记忆能力包装成工具。

热记忆是每轮自动注入的，模型直接看得到，不需要主动查。但冷记忆（全部历史）
必须按需检索——没有这个工具，提示词里就不能说「你可以查历史记忆」，
否则那是一条模型做不到的指令，比不写更糟。

`search_history` 是**另一种**东西：它查的不是蒸馏过的记忆，而是原样的
聊天往来。聊天往来默认不进上下文（短上下文正是靠不塞历史才成立的），
所以「这个问题是从哪来的」只能由它主动去取——两个工具一起挂着，模型才
分得清「过去的结论」（recall）与「当时那句话」（search_history）。
"""

from pathlib import Path

from spoolkit.kernel import sessions as kernel_sessions
from spoolkit.memory.session import MemorySession
from spoolkit.memory.transcript import (
    no_hits_text,
    parse_moment,
    render_history_hits,
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
        session=session_id,
        since=since,
        until=until,
    )
    if not hits:
        return ToolResult(ok=True, content=no_hits_text(query, session=session_id))
    return ToolResult(ok=True, content=render_history_hits(hits, _titles(root)))


def _titles(root: Path) -> dict[str, str]:
    """会话 id → 标题。标题在 `.agent/sessions/<id>/meta.json` 里，不在库里。

    只在真有命中时才读：没有人命名的会话（跑过一次就再没碰过）本来就没有标题，
    而每次检索都去翻一遍所有会话目录是白花的钱。
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

    名字取 `search_history`：与 `search_code` 同族，一眼看出是「在历史里搜」
    而不是「翻记忆」——后者已经有 `recall` 了。三个数字都是标定值，从
    `limits.py` 传进来，不在工具里写死。
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
            "按关键词检索本工作区的历史消息（聊天往来），返回片段与会话/时间/"
            "角色出处；默认跨本工作区的全部会话。历史默认不进上下文，"
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
                    "description": "只看这一条会话；不给就是本工作区的全部会话",
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


__all__ = ["recall_spec", "search_history_spec"]
