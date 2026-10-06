"""上下文预算与分层配额。

术语：有效预算 = 上下文窗口 − 输出预留。
输出预留比例硬性为 0.15，不可被任何区段占用——上下文接近满载时
模型质量显著下降，且必须留有空间生成响应。

**这里所有的数都是能力标定值**（见设计文档 2.7 与 `spoolkit/limits.py`）：
它们的作用是在弱模型上防止退化，而同一个比例用在强模型上就是上限。所以
取值一律走 `limits`，而不是各写一份常量——默认值是按本机 27B + 8K 窗口
实测出来的，不是普适真理。
"""

from dataclasses import dataclass
from typing import Mapping

from spoolkit import limits

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
        """软触发线：达到后整理上下文，不中断任务。

        **按窗口算**（不是按有效预算）：它管的是「该整理了」，整理一次很便宜，
        早一点没有代价。硬触发那条不一样，见下。
        """
        return int(self.window * self._knob("soft_trigger_ratio"))

    def hard_limit(self) -> int:
        """硬触发线：达到后重置上下文（丢掉历史、保留状态）。

        **按有效预算算，而且不得高于它**——这一条由 P0 取证改过来，原因是
        「重置」在这套装配里错得比看上去更早：

        - 装配器对**非必留区段**每个都按配额裁剪（system 900 + hot_memory 1044
          + task_state 348 + retrieval 1044 + code 2437 + lessons 696 ≈ 6469
          token，8K 窗口下），必留的 system 另有 2557——所以「需求体积」在
          **第一步就接近 9000**，而那时模型还什么都没读过；
        - 旧口径是 `window × 0.90 = 7372`。于是历史还没装多少，重置就先触发了：
          真机对照 36 次里 **17 次**至少重置过一次，而重置把已读到的工具输出
          **整段丢掉**（`p0-turns.json` 逐回合可查：第 4 步已读到失败原文与
          `services.py`，第 5 步重置之后全部作废，第 6-8 步在重新列目录）。
          这正是「素材够了却写不出结论」的主要成因。

        为什么上界是 `effective()`：装配器**最多只能装到有效预算**，需求超过它
        本来就要裁剪。所以「真装不下了」这件事在 `demand > effective()` 时才算
        成立，拿一个比它低得多的数当重置线，只会更早地把素材丢掉。
        保留 `hard_trigger_ratio` 这个旋钮（默认 0.98）的理由：它仍然要能
        **提前**一点触发，好让裁剪发生在装配之前而不是让模型读到半截内容。
        """
        return min(
            int(self.effective() * self._knob("hard_trigger_ratio")),
            self.effective(),
        )


@dataclass(frozen=True)
class CompactDecision:
    """「这一轮的素材值不值得压一遍」的判定结果。

    它是一个**纯计算**的结果：只回答两个问题——现在压不压、压完允许留多少。
    真正去改上下文的动作在压缩器里（`agent/material_compactor.py`），这里不做
    任何 I/O，也不调用模型。这样「什么时候压」这件事可以脱离模型单独算、单独量、
    单独被变异打红。
    """

    should: bool
    reason: str
    used: int
    material: int
    effective: int
    threshold: int
    free: int
    reserve_target: int
    keep_tokens: int
    expected_saving: int


def compact_trigger(budget: Budget, *, used: int, material: int) -> CompactDecision:
    """素材压缩的动态阈值。**两个数都是 token，不是字符**。

    `used` 与预算守卫同口径：**裁剪前**的需求量（`assembled.demand_tokens`）。
    `material` 是其中「可以由压缩器改写」的那一部分（工具输出/读进来的正文），
    不是整个需求——系统提示、任务状态、检索结果都不是压缩器的地盘。

    为什么必须动态：写死的阈值会随窗口一起错。同一个「3000」在 8K 窗口下正好
    半屏，在 2K 窗口下就是全部——而这两个窗口在这套系统里都会出现（探针用
    4096/8192，`--window` 可改）。所以触发线从 `Budget.effective()` 与当前需求
    算出来，四个标定值全部走登记表（`material_compact_*`）。

    触发条件（三条，缺一不可）：

    1. **余量已经低于目标**：目标余量 = `max(有效预算 × (1 − ratio), min_free)`，
       两个下限取大。比例那一条负责「快到线了就腾地方」，绝对量那一条负责小窗口
       ——窗口 2048 时 28% 只有 487 token，而一条工具输出就可能上千，比例线保不住
       它（`material_compact_min_free_tokens` 的 note 里有这笔账）。
    2. **素材本身够长**（≥ `floor_tokens`）：一次压缩要花一次模型调用，素材太短
       时省下的还不如调用本身贵。
    3. **素材占当前需求的一半以上**（`share`）：需求大但大在系统提示与检索上时，
       压素材腾不出多少地方，那是「为压缩而压缩」。

    `keep_tokens` 是**给压缩器的预算**，不是硬砍：要求它至少砍掉缺口
    （`reserve_target − free`），但不得低于 `floor_tokens`——「一条都不要留」
    不是这里能决定的事，判据保护的那部分素材由压缩器逐字保留。
    缺口比素材还大时如实报：`free + expected_saving` 会小于 `reserve_target`，
    「压完也回不到目标」这件事在 reason 里说不清，但数字上是可查的。
    """
    # 同一个模块内取标定值：`_knob` 是私有的，但它要的就是「含本次运行覆盖的值」
    # 这一件事，而公开一个 `knob()` 只会多一个绕路。
    effective = budget.effective()
    ratio = budget._knob("material_compact_ratio")
    share = budget._knob("material_compact_share")
    floor = int(budget._knob("material_compact_floor_tokens"))
    min_free = int(budget._knob("material_compact_min_free_tokens"))

    used = max(int(used), 0)
    material = max(int(material), 0)
    free = max(effective - used, 0)
    reserve_target = max(int(effective * (1.0 - ratio)), min_free)
    threshold = max(effective - reserve_target, 0)
    deficit = reserve_target - free

    def no(reason: str) -> CompactDecision:
        return CompactDecision(
            should=False,
            reason=reason,
            used=used,
            material=material,
            effective=effective,
            threshold=threshold,
            free=free,
            reserve_target=reserve_target,
            keep_tokens=material,
            expected_saving=0,
        )

    if deficit <= 0:
        return no(
            f"还没到整理线：现用 {used}，余量 {free} ≥ 目标 {reserve_target}"
            f"（有效预算 {effective}，整理线 {threshold}）"
        )
    if material < floor:
        return no(
            f"素材只有 {material} token（< 下限 {floor}），不值得为它花一次整理"
        )
    if material < share * used:
        percent = int(round(100.0 * material / max(used, 1)))
        return no(
            f"素材 {material} token 只占当前需求 {percent}%"
            f"（< {int(round(share * 100))}%），压它腾不出多少地方"
        )

    keep = max(floor, material - deficit)
    return CompactDecision(
        should=True,
        reason=(
            f"素材该整理了：现用 {used} 已过整理线 {threshold}"
            f"（有效预算 {effective}，目标余量 {reserve_target}，现在只剩 {free}）；"
            f"素材 {material} token，压完最多留 {keep}"
        ),
        used=used,
        material=material,
        effective=effective,
        threshold=threshold,
        free=free,
        reserve_target=reserve_target,
        keep_tokens=keep,
        expected_saving=material - keep,
    )

