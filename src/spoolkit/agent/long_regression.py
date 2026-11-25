"""长文回归夹具。

二十条题目覆盖方案、报告、对比、架构、表格。脚本模型按固定五节大纲作答，
用来检查组装是否把标题留全。它不是真机时延，也不是截断率。
"""

import re
from dataclasses import dataclass

from spoolkit.agent.sections import assemble, write_sections
from spoolkit.agent.writing_state import WritingState, request_outline
from spoolkit.llm.types import ChatRequest, ChatResponse, Message

_OUTLINE = (
    ("背景", "说明现状"),
    ("问题", "指出缺口"),
    ("方案", "给出做法"),
    ("步骤", "列出顺序"),
    ("风险", "写明代价"),
)
_SECTION = re.compile(r"只写第 \d+ 节「(.+?)」")


@dataclass(frozen=True)
class LongPrompt:
    category: str
    text: str


@dataclass(frozen=True)
class ScriptedTurn:
    category: str
    completed: bool
    fallback_short: bool
    sections_total: int


def _prompts(category: str, texts: tuple[str, ...]) -> tuple[LongPrompt, ...]:
    return tuple(LongPrompt(category, text) for text in texts)


LONG_PROMPTS: tuple[LongPrompt, ...] = (
    *_prompts(
        "方案",
        (
            "写一份把本地模型接进客服的完整方案。",
            "给仓库拆分写一份迁移方案，含实施步骤。",
            "给权限系统写一份改造方案。",
            "给发布流程写一份回滚方案。",
        ),
    ),
    *_prompts(
        "报告",
        (
            "写一份本周故障复盘报告。",
            "写一份上下文窗口占用的调查报告。",
            "写一份依赖升级的风险报告。",
            "写一份测试缺口的盘点报告。",
        ),
    ),
    *_prompts(
        "对比",
        (
            "对比单次终稿和分节写作。",
            "对比缓存命中和整段重算。",
            "对比 JSON 终稿和纯文本终稿。",
            "对比短答和长文两条路径。",
        ),
    ),
    *_prompts(
        "架构",
        (
            "说明控制回合和终稿回合的架构。",
            "说明大纲、逐节、组装的架构。",
            "说明预算计算在各阶段的架构。",
            "说明事件从内核到网页的架构。",
        ),
    ),
    *_prompts(
        "表格",
        (
            "用表格列出各阶段的输出上限。",
            "用表格对比五类长文题目。",
            "用表格写出截断之后的处理。",
            "用表格列出回归要看的指标。",
        ),
    ),
)


def run_scripted(prompts: tuple[LongPrompt, ...] | list[LongPrompt]) -> tuple[ScriptedTurn, ...]:
    return tuple(_run(prompt, cut=False) for prompt in prompts)


def run_cut_short() -> ScriptedTurn:
    return _run(LONG_PROMPTS[0], cut=True)


def _run(prompt: LongPrompt, *, cut: bool) -> ScriptedTurn:
    gateway = _Scripted(cut=cut)
    state = request_outline(
        gateway,
        (Message(role="user", content=prompt.text),),
        goal=prompt.text,
        token_budget=8000,
    )
    if not isinstance(state, WritingState):
        return ScriptedTurn(prompt.category, False, True, 0)
    drafts = write_sections(gateway, state, facts="工具结果：无额外材料。")
    text = assemble(state, drafts)
    if not isinstance(text, str):
        return ScriptedTurn(prompt.category, False, True, len(state.outline))
    fallback = "继续" in text
    missing = [item.title for item in state.outline if f"## {item.title}" not in text]
    return ScriptedTurn(
        category=prompt.category,
        completed=not missing and not fallback,
        fallback_short=fallback,
        sections_total=len(state.outline),
    )


class _Scripted:
    def __init__(self, *, cut: bool) -> None:
        self.cut = cut
        self.calls = 0

    def chat(self, request: ChatRequest) -> ChatResponse:
        blob = "\n".join(item.content for item in request.messages)
        if "不是控制层" in blob:
            text = "\n".join(f"{title} | {points} | 400" for title, points in _OUTLINE)
            return ChatResponse(text=text, prompt_tokens=30, completion_tokens=40)
        self.calls += 1
        if self.cut and self.calls % 2 == 1:
            return ChatResponse(
                text="半截", prompt_tokens=40, completion_tokens=8, truncated=True
            )
        if self.cut:
            return ChatResponse(text="   ", prompt_tokens=40, completion_tokens=1)
        matched = _SECTION.search(blob)
        title = matched.group(1) if matched else "本节"
        return ChatResponse(
            text=f"这一节写{title}。",
            prompt_tokens=40,
            completion_tokens=12,
        )
