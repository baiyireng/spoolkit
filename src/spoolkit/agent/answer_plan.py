"""要不要走长文。模型自报优先；没有申报时才看用户消息。

问句很长本身不算长文。命中线索之后，用整段消息（含表格、架构的 Markdown）
按字数估 token，超过 1500 才兜底。
"""

from spoolkit.agent.protocol import AnswerPlan
from spoolkit.context.phase_budget import CHARS_PER_TOKEN

LONG_TOKEN_THRESHOLD = 1500

# 长的放前面，避免以后改成「取最长匹配」时被短词截走。
LONG_CUES = (
    "完整方案",
    "实施步骤",
    "报告",
    "对比",
    "架构",
    "表格",
    "详细",
)


def should_use_long_answer(user_text: str, plan: AnswerPlan | None = None) -> bool:
    """`mode=long` 必进；`mode=short` 不进；否则线索加预估 token。"""
    if plan is not None:
        if plan.mode == "long":
            return True
        if plan.mode == "short":
            return False
    return _hits_cue(user_text) and _estimate_tokens(user_text) > LONG_TOKEN_THRESHOLD


def _hits_cue(user_text: str) -> bool:
    return any(cue in user_text for cue in LONG_CUES)


def _estimate_tokens(user_text: str) -> int:
    if CHARS_PER_TOKEN <= 0:
        return 0
    return int(len(user_text) / CHARS_PER_TOKEN)
