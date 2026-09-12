"""上下文预算与分层配额。

术语：有效预算 = 上下文窗口 − 输出预留。
输出预留比例硬性为 0.15，不可被任何区段占用——上下文接近满载时
模型质量显著下降，且必须留有空间生成响应。

**这里所有的数都是能力标定值**（见设计文档 2.7 与 `agents_dev/limits.py`）：
它们的作用是在弱模型上防止退化，而同一个比例用在强模型上就是上限。所以
取值一律走 `limits`，而不是各写一份常量——默认值是按本机 27B + 8K 窗口
实测出来的，不是普适真理。
"""

from typing import Mapping

from agents_dev import limits

OUTPUT_RESERVE_RATIO = 0.15

# 被截断过一次之后，这一次任务里的输出预算抬到这里。
#
# 截断是一个**关于这个任务的事实**：它的输出形态就是偏大（要交一份长文件、
#
# 或者要写一段较长的脚本）。与其每一轮都撞一次再翻倍重试，不如把这个事实
# 记在这次任务里。
#
# 不抬更高的理由：抬的是输出的钱，付的是输入的预算——窗口 8192 时 30% 是
# 2457，留给上下文装配的只剩 5735。再往上就该换代更大的窗口，而不是继续
# 从这个窗口里切。
OUTPUT_RESERVE_BOOST_RATIO = 0.30

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
    # 教训是「以前踩过的坑」，在动手前看到它最有用。
    # 给它一块固定额度，避免它和别的检索内容抢位置而被裁掉。
    "lessons": 0.10,
}

SOFT_TRIGGER_RATIO = 0.70
HARD_TRIGGER_RATIO = 0.90


# 区段名 → 登记表里的名字。放在这里是为了让「这个名字对应哪个标定值」
# 只有一处：改配额就是改表，不用在几条分支里找。
_FIXED_KNOBS = {"system": "system_quota"}
_FLEX_KNOBS = {
    "hot_memory": "hot_memory_ratio",
    "task_state": "task_state_ratio",
    "retrieval": "retrieval_ratio",
    "code": "code_ratio",
    "lessons": "lessons_ratio",
}


class Budget:
    """一次请求的上下文预算。

    overrides 是本次运行的能力标定覆盖（`.agent/limits.json` + 命令行）。
    取值一律经 `_knob()`，这样「现在是多少、从哪来」答得出来。
    """

    def __init__(
        self,
        window: int,
        output_ratio: float | None = None,
        overrides: Mapping[str, float] | None = None,
    ) -> None:
        if window <= 0:
            raise ValueError("上下文窗口必须为正数")
        self.window = window
        self._overrides = overrides or {}
        self._output_ratio = (
            output_ratio
            if output_ratio is not None
            else self._knob("output_reserve_ratio")
        )

    def _knob(self, name: str) -> float:
        value, _ = limits.resolve(name, self._overrides)
        return value

    def output_reserve(self) -> int:
        """为模型输出保留的 token 数。"""
        return int(self.window * self._output_ratio)

    def boost_output(self, ratio: float | None = None) -> int:
        """把输出预算抬高。返回抬高之后的额度（没变就返回原值）。

        调用点只有一个：这次任务的某次请求被输出预算截断了。
        """
        target = self._knob("output_reserve_boost_ratio") if ratio is None else ratio
        self._output_ratio = max(self._output_ratio, target)
        return self.output_reserve()

    def effective(self) -> int:
        """可供上下文装配使用的有效预算。"""
        return self.window - self.output_reserve()

    def quota(self, name: str) -> int:
        """区段 name 的配额。固定配额优先，其次比例配额，未定义则为 0。"""
        effective = self.effective()
        if name in FIXED_QUOTAS:
            cap = int(effective * self._knob("fixed_quota_cap"))
            return min(int(self._knob(_FIXED_KNOBS[name])), cap)
        if name not in FLEX_QUOTAS:
            return 0
        ratio = self._knob(_FLEX_KNOBS[name])
        if name == "code" and effective < self._knob("compact_threshold"):
            ratio += self._knob("compact_code_boost")
        return int(effective * ratio)

    def allocation(self) -> dict[str, int]:
        """当前实际生效的分配表，用于观察与调参。"""
        names = list(FIXED_QUOTAS) + list(FLEX_QUOTAS)
        return {name: self.quota(name) for name in names}

    def soft_limit(self) -> int:
        """软触发线：达到后整理上下文，不中断任务。"""
        return int(self.window * self._knob("soft_trigger_ratio"))

    def hard_limit(self) -> int:
        """硬触发线：达到后重置上下文，保留状态。"""
        return int(self.window * self._knob("hard_trigger_ratio"))

