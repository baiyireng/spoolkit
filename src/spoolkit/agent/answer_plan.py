"""要不要走长文。

结构线索本身就是要分节，不看题目字数，也不被模型漏报或报成 short 否决。
用户明确要短时优先走短答。
"""

from spoolkit.agent.protocol import AnswerPlan

LONG_CUES = (
    "完整方案",
    "实施步骤",
    "方案",
    "报告",
    "对比",
    "架构",
    "表格",
    "详细",
)
SHORT_CUES = (
    "简短",
    "一句话",
    "简要",
    "不用展开",
    "只要结论",
)


def should_use_long_answer(user_text: str, plan: AnswerPlan | None = None) -> bool:
    """结构线索进长文。用户说短则不进。不再用题目长短当门槛。"""
    if plan is not None and plan.mode == "long":
        return True
    if any(cue in user_text for cue in SHORT_CUES):
        return False
    if any(cue in user_text for cue in LONG_CUES):
        return True
    if plan is not None and plan.mode == "short":
        return False
    return False
