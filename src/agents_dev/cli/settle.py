"""按授权策略处理待落盘的改动。

把「决定这些改动怎么办」收敛成一个函数，主循环路径和计划路径共用。
两处各写一遍的结果，是策略在其中一条路径上悄悄失效——那种 bug 不会报错，
只会让人以为「我明明设了只读」。
"""

from pathlib import Path
from typing import Callable, Sequence

from agents_dev.agents.plan import out_of_scope
from agents_dev.cli.approval import apply_with_audit, review_and_apply
from agents_dev.policy import AUTO, DENY
from agents_dev.tools.edit import PendingChanges

NONE = "none"
AUTO_APPLIED = "auto"
CONFIRMED = "confirmed"
DECLINED = "declined"
DENIED = "denied"
SETTLED = "settled"


def settle(
    pending: PendingChanges,
    policy: str,
    scope: Sequence[str],
    baseline_path: Path | None = None,
    ask: Callable[[str], str] = input,
    non_interactive: bool = False,
) -> tuple[str, list[str]]:
    """按策略处理待落盘改动，返回（结果动作，已写入路径）。"""
    if len(pending) == 0:
        return NONE, []

    if policy == DENY:
        _show(pending)
        pending.discard()
        print("当前策略为只读（deny），这些改动已被丢弃。")
        return DENIED, []

    if policy == AUTO:
        blocked = out_of_scope([c.path for c in pending.items()], scope)
        if not blocked:
            return AUTO_APPLIED, apply_with_audit(pending, baseline_path)
        # 越界就退回确认：auto 只覆盖它被允许的范围，
        # 不等于「这次运行整体被信任」。
        print("以下改动超出允许范围，需要逐项确认：" + "、".join(blocked))
        if non_interactive:
            # 无人值守时不能弹问题。这里只丢**越界的那几处**，范围内的照落。
            #
            # 原先是整批作废，代价实测过：43 步那处**在范围内、而且是对的**
            # 修改被一起丢掉，审查据此判它「测试尚未通过」，整份计划在
            # 43/49 处中止——剩下 6 题连试都没试。谨慎不等于把对的一起扔。
            dropped = pending.drop(blocked)
            print(f"无人值守运行：范围外的 {len(dropped)} 处已丢弃，范围内的照落。")
            if len(pending) == 0:
                return DENIED, []
            return AUTO_APPLIED, apply_with_audit(pending, baseline_path)

    # 无人值守时不能弹问题——没有人会回答。没有自动授权的策略下，
    # 一律拒绝，并把原因说清楚，而不是静默丢弃。
    if non_interactive:
        _show(pending)
        pending.discard()
        print("无人值守运行，且本次运行没有自动授权，已拒绝。")
        return DENIED, []

    applied, written = review_and_apply(
        pending, ask=ask, baseline_path=baseline_path
    )
    return (CONFIRMED if applied else DECLINED), written


def _show(pending: PendingChanges) -> None:
    for change in pending.items():
        print(change.diff)
