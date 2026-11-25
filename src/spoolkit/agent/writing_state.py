"""长文的大纲和写作状态。

逐节正文不放在这里。状态只记目标、已完成的节、以及跨节约束，
方便存下来再接着写。
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from spoolkit.agent.metrics import metric_scope
from spoolkit.context.phase_budget import HARD_CAP
from spoolkit.llm.types import ChatRequest, ChatResponse, Message

MIN_SECTIONS = 3
MAX_SECTIONS = 7
DECISION_CHARS = 40
DECISIONS_PER_SECTION = 3
DECISION_CAP = 20
# 终稿阶段的用户插话默认不打断当前节，排到下一轮控制。
INTERRUPT_FINAL = False
_DECISION_LINE = re.compile(r"^决定：\s*(.*)$")

OUTLINE_INSTRUCTION = (
    "你现在是写作层，不是控制层：不要输出 thought/tool_calls/done/final，"
    "不要包 JSON 外壳。\n"
    "直接输出 3–7 行，每行：标题 | 一句话要点 | 目标 tokens。\n"
    "总 tokens 不超过 {remaining}。"
)

_LINE = re.compile(r"^(.+?)\s*\|\s*(.+?)\s*\|\s*(\d+)\s*$")


@dataclass(frozen=True)
class OutlineSection:
    title: str
    points: str
    target_tokens: int


@dataclass(frozen=True)
class OutlineFailure:
    reason: str


@dataclass(frozen=True)
class WritingState:
    goal: str
    outline: tuple[OutlineSection, ...]
    completed: tuple[int, ...] = ()
    decisions: tuple[str, ...] = ()
    next_section: int = 0

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "outline": [
                {
                    "title": item.title,
                    "points": item.points,
                    "target_tokens": item.target_tokens,
                }
                for item in self.outline
            ],
            "completed": list(self.completed),
            "decisions": list(self.decisions),
            "next_section": self.next_section,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WritingState":
        outline = tuple(
            OutlineSection(
                title=str(item["title"]),
                points=str(item["points"]),
                target_tokens=int(item["target_tokens"]),
            )
            for item in payload["outline"]
        )
        return cls(
            goal=str(payload["goal"]),
            outline=outline,
            completed=tuple(int(item) for item in payload.get("completed", ())),
            decisions=tuple(str(item) for item in payload.get("decisions", ())),
            next_section=int(payload.get("next_section", 0)),
        )


@dataclass(frozen=True)
class StopEffect:
    completed: tuple[int, ...]
    incomplete: int | None
    queued_note: str
    cancel_current: bool


def apply_user_stop(
    state: WritingState,
    *,
    phase: Literal["section", "final"],
    note: str,
) -> StopEffect:
    """取消当前请求。已提交的节留下。终稿插话只排队，不把某一节标成未完成。"""
    if phase == "final" and not INTERRUPT_FINAL:
        return StopEffect(
            completed=state.completed,
            incomplete=None,
            queued_note=note,
            cancel_current=True,
        )
    incomplete = (
        None if state.next_section in state.completed else state.next_section
    )
    return StopEffect(
        completed=state.completed,
        incomplete=incomplete,
        queued_note="",
        cancel_current=True,
    )


def split_section_decisions(text: str) -> tuple[str, tuple[str, ...]]:
    """节末「决定：」行抽出来。正文留在节里，不进约束。"""
    lines = text.splitlines()
    taken: list[str] = []
    while lines:
        matched = _DECISION_LINE.match(lines[-1].strip())
        if matched is None:
            break
        lines.pop()
        item = matched.group(1).strip()
        if item:
            taken.append(item)
    while lines and not lines[-1].strip():
        lines.pop()
    taken.reverse()
    return "\n".join(lines).strip(), tuple(taken[:DECISIONS_PER_SECTION])


def merge_decisions(existing: Sequence[str], found: Sequence[str]) -> tuple[str, ...]:
    """去重后追加。单节最多 3 条，每条不超过 40 字，总数不超过 20。"""
    kept = [item.strip() for item in existing if item and item.strip()]
    seen = {_decision_key(item) for item in kept}
    added = 0
    for raw in found:
        if added >= DECISIONS_PER_SECTION or len(kept) >= DECISION_CAP:
            break
        item = raw.strip()
        if not item or len(item) > DECISION_CHARS:
            continue
        key = _decision_key(item)
        if key in seen:
            continue
        kept.append(item)
        seen.add(key)
        added += 1
    return tuple(kept[:DECISION_CAP])


def _decision_key(text: str) -> str:
    return " ".join(text.split())


def parse_outline(text: str, *, goal: str, token_budget: int) -> WritingState | OutlineFailure:
    """收下 3–7 节。节数越界直接失败；总 token 超窗则从末尾删，删不动再缩小目标。"""
    sections = _read_sections(text)
    if isinstance(sections, OutlineFailure):
        return sections
    if len(sections) < MIN_SECTIONS:
        return OutlineFailure(reason=f"大纲只有 {len(sections)} 节，需要 3–7 节")
    if len(sections) > MAX_SECTIONS:
        return OutlineFailure(reason=f"大纲有 {len(sections)} 节，需要 3–7 节")
    fitted = _fit_budget(sections, token_budget)
    if isinstance(fitted, OutlineFailure):
        return fitted
    return WritingState(goal=goal, outline=tuple(fitted))


def fallback_outline(goal: str, budget: int) -> WritingState:
    """大纲没收下时用固定三节，保证还能分节写。"""
    share = max(1, budget // 4)
    return WritingState(
        goal=goal,
        outline=(
            OutlineSection("背景与目标", "问题、约束、成功标准", share),
            OutlineSection("方案与结构", "核心设计、关键取舍", max(1, budget // 2)),
            OutlineSection("风险与下一步", "边界、成本、行动项", share),
        ),
    )


def request_outline(
    gateway: Any,
    prefix: Sequence[Message],
    *,
    goal: str,
    token_budget: int,
    sections: int | None = None,
    correction: str = "",
) -> WritingState | OutlineFailure:
    """用控制回合的前缀要一份大纲。纯文本，不带 schema。"""
    content = OUTLINE_INSTRUCTION.format(remaining=token_budget)
    if sections is not None and sections > 0:
        content += f"\n必须正好 {sections} 节，不能多也不能少。"
    if correction.strip():
        content += "\n" + correction.strip()
    request = ChatRequest(
        messages=tuple(prefix) + (
            Message(
                role="user",
                content=content,
            ),
        ),
        max_tokens=HARD_CAP["outline"],
        response_schema=None,
        cache_prompt=True,
    )
    with metric_scope(phase="outline", task_type="long"):
        response: ChatResponse = gateway.chat(request)
    return parse_outline(response.text, goal=goal, token_budget=token_budget)


def _read_sections(text: str, _depth: int = 0) -> list[OutlineSection] | OutlineFailure:
    stripped = _strip_fence(text.strip())
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return OutlineFailure(reason="大纲 JSON 无法解析")
        if isinstance(payload, dict) and isinstance(payload.get("final"), str):
            if _depth >= 2:
                return OutlineFailure(reason="大纲 JSON 嵌套过深")
            return _read_sections(payload["final"], _depth + 1)
        return _rows_from_payload(payload)
    found: list[OutlineSection] = []
    for raw in stripped.splitlines():
        line = raw.strip()
        if not line:
            continue
        matched = _LINE.match(line)
        if matched is None:
            continue
        title, points, tokens = matched.groups()
        if not title.strip() or not points.strip():
            continue
        found.append(OutlineSection(title.strip(), points.strip(), int(tokens)))
    if not found:
        return OutlineFailure(reason="大纲里没有「标题 | 要点 | 目标 tokens」这样的行")
    return found


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _rows_from_payload(payload: Any) -> list[OutlineSection] | OutlineFailure:
    rows = payload.get("outline") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return OutlineFailure(reason="大纲 JSON 需要节数组")
    found: list[OutlineSection] = []
    for item in rows:
        if not isinstance(item, dict):
            return OutlineFailure(reason="大纲 JSON 的每一节必须是对象")
        title = item.get("title")
        points = item.get("points")
        tokens = item.get("target_tokens")
        if not isinstance(title, str) or not title.strip():
            return OutlineFailure(reason="大纲节缺少标题")
        if not isinstance(points, str) or not points.strip():
            return OutlineFailure(reason="大纲节缺少要点")
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            return OutlineFailure(reason="大纲节的目标 tokens 必须是正整数")
        found.append(OutlineSection(title.strip(), points.strip(), tokens))
    return found


def _fit_budget(
    sections: list[OutlineSection], budget: int
) -> list[OutlineSection] | OutlineFailure:
    kept = list(sections)
    while len(kept) > MIN_SECTIONS and sum(item.target_tokens for item in kept) > budget:
        kept.pop()
    total = sum(item.target_tokens for item in kept)
    if total <= budget:
        return kept
    if budget < len(kept):
        return OutlineFailure(reason="窗口不够放下 3 节")
    scaled: list[int] = [item.target_tokens * budget // total for item in kept]
    scaled = [max(1, item) for item in scaled]
    drift = budget - sum(scaled)
    if drift:
        largest = max(range(len(scaled)), key=lambda index: scaled[index])
        scaled[largest] += drift
    if any(item < 1 for item in scaled):
        return OutlineFailure(reason="窗口不够放下 3 节")
    return [
        OutlineSection(item.title, item.points, tokens)
        for item, tokens in zip(kept, scaled, strict=True)
    ]
