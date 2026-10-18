"""终稿前的证据门槛。

普通问题可以写明「无需外部信息」，不必为了过门去检索。
问到最新、今天、价格或版本时，summary 必须引用本轮工具结果。
这里不指定要调哪个工具。
"""

from collections.abc import Sequence
from dataclasses import dataclass

FRESH_TAGS = ("最新", "今天", "价格", "版本")
NO_EXTERNAL = "无需外部信息"


@dataclass(frozen=True)
class EvidenceDecision:
    ok: bool
    hard: bool
    reason: str


def check_evidence(
    user_text: str,
    summary: str,
    tool_texts: Sequence[str],
) -> EvidenceDecision:
    """done 之前看 summary。硬门禁只对实时信息。"""
    hard = any(tag in user_text for tag in FRESH_TAGS)
    cited = _cites(summary, tool_texts)
    if hard:
        if cited:
            return EvidenceDecision(ok=True, hard=True, reason="")
        return EvidenceDecision(
            ok=False,
            hard=True,
            reason="这是实时信息，summary 必须引用本轮工具结果。",
        )
    if cited or NO_EXTERNAL in summary:
        return EvidenceDecision(ok=True, hard=False, reason="")
    return EvidenceDecision(
        ok=False,
        hard=False,
        reason="summary 需要写上本轮工具结果，或写明「无需外部信息」。",
    )


def _cites(summary: str, tool_texts: Sequence[str]) -> bool:
    for raw in tool_texts:
        for body in _bodies(raw):
            if len(body) >= 8 and body[:8] in summary:
                return True
            if 4 <= len(body) < 8 and body in summary:
                return True
    return False


def _bodies(raw: str) -> tuple[str, ...]:
    compact = " ".join(raw.split())
    if ": " in compact:
        return (compact.split(": ", 1)[1].strip(), compact)
    return (compact,)
