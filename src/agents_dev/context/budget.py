"""上下文预算与分层配额。

术语：有效预算 = 上下文窗口 − 输出预留。
输出预留比例硬性为 0.15，不可被任何区段占用——上下文接近满载时
模型质量显著下降，且必须留有空间生成响应。
"""

OUTPUT_RESERVE_RATIO = 0.15

# 固定配额：与窗口大小无关。系统提示与工具描述长度稳定，按绝对量给定。
FIXED_QUOTAS: dict[str, int] = {
    "system": 900,
}

# 固定额度也要封顶。同一个 900，在 32K 窗口下只占有效预算的 3%，
# 在 1K 窗口下却能吃掉全部——固定值本身并不固定，它必须随窗口伸缩。
FIXED_QUOTA_CAP = 0.20

# 小窗口模式：窗口越小，「最近轮次」这种模糊内容的性价比越低，
# 而精确取用的代码片段价值越高。所以把配额从历史挪向代码。
COMPACT_THRESHOLD = 3000
COMPACT_CODE_BOOST = 0.10

# 比例配额：占有效预算的百分比。未列出的区段配额为 0。
# recent_turns 不设配额，它取所有配额之外的余量。
FLEX_QUOTAS: dict[str, float] = {
    "hot_memory": 0.15,
    "task_state": 0.05,
    "retrieval": 0.15,
    "code": 0.35,
}

SOFT_TRIGGER_RATIO = 0.70
HARD_TRIGGER_RATIO = 0.90


class Budget:
    """一次请求的上下文预算。"""

    def __init__(self, window: int) -> None:
        if window <= 0:
            raise ValueError("上下文窗口必须为正数")
        self.window = window

    def output_reserve(self) -> int:
        """为模型输出保留的 token 数。"""
        return int(self.window * OUTPUT_RESERVE_RATIO)

    def effective(self) -> int:
        """可供上下文装配使用的有效预算。"""
        return self.window - self.output_reserve()

    def quota(self, name: str) -> int:
        """区段 name 的配额。固定配额优先，其次比例配额，未定义则为 0。"""
        effective = self.effective()
        if name in FIXED_QUOTAS:
            return min(FIXED_QUOTAS[name], int(effective * FIXED_QUOTA_CAP))
        ratio = FLEX_QUOTAS.get(name)
        if ratio is None:
            return 0
        if name == "code" and effective < COMPACT_THRESHOLD:
            ratio += COMPACT_CODE_BOOST
        return int(effective * ratio)

    def allocation(self) -> dict[str, int]:
        """当前实际生效的分配表，用于观察与调参。"""
        names = list(FIXED_QUOTAS) + list(FLEX_QUOTAS)
        return {name: self.quota(name) for name in names}

    def soft_limit(self) -> int:
        """软触发线：达到后整理上下文，不中断任务。"""
        return int(self.window * SOFT_TRIGGER_RATIO)

    def hard_limit(self) -> int:
        """硬触发线：达到后重置上下文，保留状态。"""
        return int(self.window * HARD_TRIGGER_RATIO)

