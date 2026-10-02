"""按授权策略处理待落盘的改动。

把「决定这些改动怎么办」收敛成一个函数，主循环路径和计划路径共用。
两处各写一遍的结果，是策略在其中一条路径上失效且不报错，
只会让人以为「我明明设了只读」。
"""

from pathlib import Path
from typing import Callable, Sequence

from spoolkit.agents.plan import out_of_scope
from spoolkit.cli.approval import apply_with_audit, review_and_apply
from spoolkit.policy import AUTO, DENY
from spoolkit.tools.edit import PendingChanges

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
    """按策略处理待落盘改动，返回（结果动作，已写入路径）。

    「没确认完的暂存重写」在任何清理**之前**取一次读数，最后统一说一句：
    deny 会把暂存一起清掉，那一句如果放到后面就永远打不出来——而它正是
    「没有静默丢弃」这条要求的落点。
    """
    note = unconfirmed_note(pending)
    outcome, written = _settle(
        pending, policy, scope, baseline_path, ask, non_interactive
    )
    if note:
        print(note)
    return outcome, written


def _settle(
    pending: PendingChanges,
    policy: str,
    scope: Sequence[str],
    baseline_path: Path | None = None,
    ask: Callable[[str], str] = input,
    non_interactive: bool = False,
) -> tuple[str, list[str]]:
    if len(pending) == 0:
        return NONE, []

    if policy == DENY:
        _show(pending)
        pending.discard()
        print("当前策略为只读（deny），这些改动已被丢弃。")
        return DENIED, []

    if policy == AUTO:
        blocked = out_of_scope([c.path for c in pending.items()], scope)
        for change in pending.items():
            if pending.is_linked(change.path) and change.path not in blocked:
                blocked.append(change.path)
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
            print(f"无人值守运行：范围外的 {len(dropped)} 处已丢弃，范围内的照常落盘。")
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
        if change.declared:
            print(f"# {change.path}：{change.declared}")
        print(change.diff)


def unconfirmed_note(pending: PendingChanges) -> str:
    """还有没确认完的暂存重写时，收尾要说的一句话（没有就返回空串）。

    为什么必须有：那些正文是模型真的交出来的东西，只是清单没交代完所以没落盘。
    进程一结束它们就没了——不吭声就是**静默丢弃**。
    """
    live = pending.staged_items()
    if not live:
        return ""
    held = "、".join(f"{record.path}（{record.lines} 行，令牌 {record.token}）" for record in live)
    return (
        f"还有 {len(live)} 份已声明的大重写没有确认完，因此没有落盘：{held}。"
        "消失清单没逐项交代（确认保留或声明有意丢弃）之前不会写入——"
        "这是有意挡住，不是失败；下次重新发起时会拿到新令牌。"
    )
