"""独立归纳会话。

把「这次任务里有什么值得长期记住」交给独立上下文判断，而不是让主循环
自己总结。好处是主循环的上下文不被总结过程污染，归纳可以在更聚焦的
提示下进行。这是同一个模型的另一次调用，不额外占用显存。

关于「输出放不下」的处理顺序，这里刻意把「分治」放在「缩减要求」之前：

1. 先放大输出预算重试；
2. 仍然放不下，就把输入切块、每块交给一个独立会话分别处理，再合并去重。
   每块都是一个干净上下文——这正是子智能体在提炼场景下的形态；
3. 只有在无法再切分（或达到切分深度上限）时，才退到「只保留最重要的一条」。

把「缩减要求」放在最后是有原因的：它对用户是隐形的遗忘。如果这次任务
确实有五条值得记的东西，降到一条就等于悄悄丢掉四条，而没人会知道。
分治的代价是多几次调用，换回的是内容完整。
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from agents_dev.agent.state import TaskState
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.retry import chat_with_escalation
from agents_dev.llm.types import ChatRequest, Message

KINDS = ("fact", "preference", "decision", "lesson")

# 切分深度上限。每加一层调用次数翻倍，收益递减，必须封顶。
MAX_SPLIT_DEPTH = 2
NO_CONTENT = "（无额外内容）"

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

{body}

可用分类：fact（项目客观事实，如构建命令、目录约定）、preference（稳定偏好）、
decision（做过的选择及其理由）、lesson（从失败提炼的规则）。

只提取具备跨任务复用价值的条目，宁可少也不要凑数。
过程细节、一次性步骤、显而易见的内容都不要提取。
最多 {limit} 条，没有就返回空数组。
只输出 JSON。"""


@dataclass(frozen=True)
class DistillResult:
    """归纳结果，同时说明它是怎么产出的。"""

    entries: list[tuple[str, str]]
    rounds: int
    split: bool
    truncated: bool
    raw: str = ""


GROUP_TITLES = {
    "过程": "已完成的过程",
    "已排除": "已排除的方案",
    "结论": "交付给用户的答复",
    "材料": "材料",
}


def segments_of(state: TaskState, final: str) -> list[tuple[str, str]]:
    """把任务留下的材料切成可独立处理的片段。

    切分单位必须是语义完整的单元，所以答复按**段落**切而不是按行切。
    曾经按行拆过，结果跨行的事实被拆成互不相干的碎片，模型一条都提炼
    不出来——切分把语义切碎了，比不切更糟。

    每段带来源标签，渲染时按标签分组呈现。
    """
    parts: list[tuple[str, str]] = [("过程", item) for item in state.done]
    parts.extend(("已排除", item) for item in state.excluded)
    parts.extend(
        ("结论", block.strip())
        for block in re.split(r"\n\s*\n", final)
        if block.strip()
    )
    return parts or [("材料", NO_CONTENT)]


def _prompt(goal: str, segments: list[tuple[str, str]], limit: int) -> str:
    grouped: dict[str, list[str]] = {}
    for label, text in segments:
        grouped.setdefault(label, []).append(text)
    lines: list[str] = [f"任务目标：{goal}", ""]
    for label, items in grouped.items():
        lines.append(f"{GROUP_TITLES.get(label, label)}：")
        if label == "结论":
            # 答复保持成块，不拆成列表项，否则跨行的事实会被切碎。
            lines.append("\n".join(items))
        else:
            lines.extend(f"- {item}" for item in items)
        lines.append("")
    body = "\n".join(lines).rstrip()
    return PROMPT.format(goal=goal, body=body, limit=limit)


def _request(
    goal: str, segments: list[tuple[str, str]], limit: int, max_tokens: int
) -> ChatRequest:
    return ChatRequest(
        messages=(Message(role="user", content=_prompt(goal, segments, limit)),),
        max_tokens=max_tokens,
        response_schema=DISTILL_SCHEMA,
    )


def _parse_entries(text: str, limit: int) -> list[tuple[str, str]]:
    """解析归纳结果。解析不出来就当作没有，由调用方决定是否重试。"""
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


def _merge(groups: list[list[tuple[str, str]]], limit: int) -> list[tuple[str, str]]:
    """合并多轮结果并去重，保持先出现的顺序。"""
    merged: list[tuple[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for kind, text in group:
            key = text.strip()
            if key in seen:
                continue
            seen.add(key)
            merged.append((kind, text))
            if len(merged) >= limit:
                return merged
    return merged


def _run_once(
    gateway: ModelGateway,
    goal: str,
    segments: list[tuple[str, str]],
    limit: int,
    max_tokens: int,
) -> tuple[list[tuple[str, str]], bool, str]:
    """执行一次提炼，返回（条目，是否被截断，原始输出）。"""
    response = chat_with_escalation(
        gateway, _request(goal, segments, limit, max_tokens)
    )
    return _parse_entries(response.text, limit), response.truncated, response.text


def _distill(
    gateway: ModelGateway,
    goal: str,
    segments: list[tuple[str, str]],
    limit: int,
    max_tokens: int,
    depth: int,
    stats: dict[str, Any],
) -> list[tuple[str, str]]:
    stats["rounds"] += 1
    entries, truncated, raw = _run_once(gateway, goal, segments, limit, max_tokens)
    if depth == 0:
        stats["raw"] = raw
    if entries or not truncated:
        return entries

    stats["truncated"] = True

    # 放不下就切开来做，而不是把要求降低。
    if len(segments) >= 2 and depth < MAX_SPLIT_DEPTH:
        stats["split"] = True
        middle = len(segments) // 2
        half = max(1, limit // 2)
        left = _distill(
            gateway, goal, segments[:middle], half, max_tokens, depth + 1, stats
        )
        right = _distill(
            gateway, goal, segments[middle:], half, max_tokens, depth + 1, stats
        )
        return _merge([left, right], limit)

    # 无法再分：只能退而求其次，保住最重要的一条，而不是一条都没有。
    stats["degraded"] = True
    if limit > 1:
        return _distill(gateway, goal, segments, 1, max_tokens * 2, depth + 1, stats)
    return []


def distill(
    gateway: ModelGateway,
    state: TaskState,
    final: str = "",
    limit: int = 5,
    max_tokens: int = 2048,
) -> DistillResult:
    """归纳出值得长期记住的条目。

    max_tokens 给得比直觉大：推理类模型的思考 token 也从输出预算里扣，
    预算太小会让结构化输出被拦腰截断，而截断后的 JSON 必然解析失败，
    表现为「什么都提炼不出来」这种没有报错的静默失败。
    """
    stats: dict[str, Any] = {
        "rounds": 0,
        "split": False,
        "truncated": False,
        "degraded": False,
    }
    entries = _distill(
        gateway,
        state.goal,
        segments_of(state, final),
        limit,
        max_tokens,
        0,
        stats,
    )
    return DistillResult(
        entries=entries,
        rounds=stats["rounds"],
        split=stats["split"],
        truncated=stats["truncated"],
        raw=stats.get("raw", ""),
    )
