"""上下文装配器。

职责：把若干区段按优先级与配额拼装成最终消息序列，超限时从最低
优先级开始裁剪。裁剪只发生在区段内部，不改变区段之间的顺序。

同时报告 demand_tokens（未裁剪前的需求总量）与 total_tokens（实际装入量），
前者用于判断上下文压力，后者用于核算真实占用。
"""

from dataclasses import dataclass
from typing import Sequence

from agents_dev.context.budget import Budget
from agents_dev.context.sections import Section
from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.llm.types import Message

TRUNCATION_MARKER = "\n…（内容已裁剪）"


@dataclass(frozen=True)
class AssembleResult:
    """装配结果。

    demand_tokens: 裁剪前的需求总量，反映上下文压力
    total_tokens:  实际装入的 token 数
    dropped:       被裁剪过的区段名
    """

    messages: tuple[Message, ...]
    total_tokens: int
    demand_tokens: int
    dropped: tuple[str, ...]
    window: int

    @property
    def ratio(self) -> float:
        """实际占用占窗口的比例。"""
        return self.total_tokens / self.window

    @property
    def pressure(self) -> float:
        """需求占窗口的比例，用于 70%/90% 触发线判断。"""
        return self.demand_tokens / self.window


class Assembler:
    """按配额装配上下文。"""

    def __init__(self, tokenizer: TokenCounter, budget: Budget) -> None:
        self._tokenizer = tokenizer
        self._budget = budget

    def _fit(self, text: str, limit: int) -> str:
        """把 text 裁剪到 limit 个 token 以内。"""
        if self._tokenizer.count(text) <= limit:
            return text
        marker_cost = self._tokenizer.count(TRUNCATION_MARKER)
        room = max(0, limit - marker_cost)
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._tokenizer.count(text[:mid]) <= room:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + TRUNCATION_MARKER

    def assemble(
        self,
        sections: Sequence[Section],
        recent_turns: Sequence[Message] = (),
    ) -> AssembleResult:
        ordered = sorted(sections, key=lambda s: s.priority)
        demand = sum(self._tokenizer.count(s.text) for s in ordered)
        demand += sum(self._tokenizer.count(m.content) for m in recent_turns)

        dropped: list[str] = []
        rendered: list[str] = []

        for section in ordered:
            if section.mandatory:
                if self._tokenizer.count(section.text) > self._budget.effective():
                    raise ValueError(
                        f"必留区段 {section.name} 超出有效预算，请检查配置"
                    )
                rendered.append(section.text)
                continue

            fitted = self._fit(section.text, self._budget.quota(section.name))
            if fitted != section.text:
                dropped.append(section.name)
            rendered.append(fitted)

        used = sum(self._tokenizer.count(r) for r in rendered)
        messages = [Message(role="system", content=r) for r in rendered]

        # 最近轮次取余量。从最新往回填决定保留哪些，再还原为时间顺序。
        kept: list[Message] = []
        for message in reversed(recent_turns):
            cost = self._tokenizer.count(message.content)
            if used + cost > self._budget.effective():
                break
            used += cost
            kept.append(message)
        kept.reverse()

        return AssembleResult(
            messages=tuple(messages + kept),
            total_tokens=used,
            demand_tokens=demand,
            dropped=tuple(dropped),
            window=self._budget.window,
        )

