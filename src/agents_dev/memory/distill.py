"""独立归纳会话。

把「这次任务里有什么值得长期记住」交给一个独立上下文判断，而不是让
主循环自己总结。两个好处：主循环的上下文不被总结过程污染；归纳可以
在更宽松的预算与更聚焦的提示下进行。

这是同一个模型的另一次调用，不额外占用显存。

归纳产物一律只作为候选——真正决定它进不进热记忆的是归档逻辑的
置信度与容量规则，不是归纳会话自己。
"""

import json
from typing import Any

from agents_dev.agent.state import TaskState
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.types import ChatRequest, Message

KINDS = ("fact", "preference", "decision", "lesson")

DISTILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "text": {"type": "string"},
                },
                "required": ["kind", "text"],
            },
        }
    },
    "required": ["entries"],
}

PROMPT = """你在整理一个编程任务结束后值得长期记住的结论。

任务目标：{goal}
已完成：{done}
已排除（试过但不通的方案）：{excluded}

交付给用户的答复：
{final}

只提取具备跨任务复用价值的条目，宁可少也不要凑数：
- fact：项目的客观事实（如构建命令、目录约定）
- preference：用户表现出的稳定偏好
- decision：做过的选择，必须连带理由
- lesson：从失败中提炼出的可复用规则

过程细节、一次性步骤、显而易见的内容都不要提取。
最多 {limit} 条，没有就返回空数组。
只输出 JSON。"""


def distill(
    gateway: ModelGateway,
    state: TaskState,
    final: str = "",
    limit: int = 5,
    max_tokens: int = 2048,
) -> list[tuple[str, str]]:
    """让独立上下文归纳出值得长期记住的条目。

    max_tokens 给得比直觉大：推理类模型的思考 token 也从这个预算里扣，
    预算太小会导致结构化输出被拦腰截断，而截断后的 JSON 解析必然失败，
    表现为「归纳不出任何东西」这类很难查的静默失败。
    """
    prompt = PROMPT.format(
        goal=state.goal,
        done="、".join(state.done) or "无",
        excluded="、".join(state.excluded) or "无",
        final=final.strip() or "无",
        limit=limit,
    )
    response = gateway.chat(
        ChatRequest(
            messages=(Message(role="user", content=prompt),),
            max_tokens=max_tokens,
            response_schema=DISTILL_SCHEMA,
        )
    )
    return _parse_entries(response.text, limit)


def _parse_entries(text: str, limit: int) -> list[tuple[str, str]]:
    """解析归纳结果。任何异常都退化为「没有可提炼内容」，不中断任务收尾。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []

    entries: list[tuple[str, str]] = []
    for item in payload.get("entries") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        content = item.get("text")
        if kind not in KINDS or not isinstance(content, str) or not content.strip():
            continue
        entries.append((kind, content.strip()))
        if len(entries) >= limit:
            break
    return entries
