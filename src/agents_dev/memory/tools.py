"""把记忆能力包装成工具。

热记忆是每轮自动注入的，模型直接看得到，不需要主动查。但冷记忆（全部历史）
必须按需检索——没有这个工具，提示词里就不能说「你可以查历史记忆」，
否则那是一条模型做不到的指令，比不写更糟。
"""

from agents_dev.memory.session import MemorySession
from agents_dev.tools.types import ToolResult, ToolSpec


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
    )

