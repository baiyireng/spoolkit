"""按授权策略处理待落盘的改动。

把「决定这些改动怎么办」收敛成一个函数，主循环路径和计划路径共用。
两处各写一遍的结果，是策略在其中一条路径上失效且不报错，
只会让人以为「我明明设了只读」。
"""

from pathlib import Path
from typing import Callable, Sequence
import json

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
    header: str = "",
    out: Callable[[str], None] = print,
) -> tuple[str, list[str]]:
    """按策略处理待落盘改动，返回（结果动作，已写入路径）。

    `header` 是**打在 diff 之前**的那一段（收尾处理用）：验证未通过时，
    「最终验证：未通过 + 失败要点 + 撤销命令」必须在这一大片 diff 之上被读到，
    而不是埋在下面。顺序就是这条要求本身，所以它由这里保证，而不是靠调用方
    记得先 print 一次。

    `out` 是人读散文的出口（默认 stdout）。计划/自主那条路上 stdout 是事件
    通道（`--events` 时一行一个 JSON），所以那里由调用方把出口换成 stderr
    ——diff、留档、拒绝说明一条都不能混进事件流。

    「没确认完的暂存重写」在任何清理**之前**取一次读数，最后统一说一句：
    deny 会把暂存一起清掉，那一句如果放到后面就永远打不出来——而它正是
    「没有静默丢弃」这条要求的落点。
    """
    note = unconfirmed_note(pending)
    if header:
        out(header)
    outcome, written = _settle(
        pending, policy, scope, baseline_path, ask, non_interactive, out
    )
    if note:
        out(note)
    # **扣成果也要说出来**：给不出基线的改动没有落盘（见 `PendingChanges.apply`），
    # 静默跳过会让人以为改好了——而那是最坏的一种报告。
    refused = refused_note(pending)
    if refused:
        out(refused)
    return outcome, written


def _settle(
    pending: PendingChanges,
    policy: str,
    scope: Sequence[str],
    baseline_path: Path | None = None,
    ask: Callable[[str], str] = input,
    non_interactive: bool = False,
    out: Callable[[str], None] = print,
) -> tuple[str, list[str]]:
    if len(pending) == 0:
        return NONE, []

    if policy == DENY:
        _show(pending, out)
        pending.discard()
        out("当前策略为只读（deny），这些改动已被丢弃。")
        return DENIED, []

    if policy == AUTO:
        blocked = out_of_scope([c.path for c in pending.items()], scope)
        for change in pending.items():
            if pending.is_linked(change.path) and change.path not in blocked:
                blocked.append(change.path)
        if not blocked:
            return AUTO_APPLIED, apply_with_audit(pending, baseline_path, out=out)
        # 越界就退回确认：auto 只覆盖它被允许的范围，
        # 不等于「这次运行整体被信任」。
        out("以下改动超出允许范围，需要逐项确认：" + "、".join(blocked))
        if non_interactive:
            # 无人值守时不能弹问题。这里只丢**越界的那几处**，范围内的照落。
            #
            # 原先是整批作废，代价实测过：43 步那处**在范围内、而且是对的**
            # 修改被一起丢掉，审查据此判它「测试尚未通过」，整份计划在
            # 43/49 处中止——剩下 6 题连试都没试。谨慎不等于把对的一起扔。
            dropped = pending.drop(blocked)
            out(f"无人值守运行：范围外的 {len(dropped)} 处已丢弃，范围内的照常落盘。")
            if len(pending) == 0:
                return DENIED, []
            return AUTO_APPLIED, apply_with_audit(pending, baseline_path, out=out)

    # 无人值守时不能弹问题——没有人会回答。没有自动授权的策略下，
    # 一律拒绝，并把原因说清楚，而不是静默丢弃。
    if non_interactive:
        _show(pending, out)
        pending.discard()
        out("无人值守运行，且本次运行没有自动授权，已拒绝。")
        return DENIED, []

    applied, written = review_and_apply(
        pending, ask=ask, baseline_path=baseline_path, out=out
    )
    return (CONFIRMED if applied else DECLINED), written


def _show(pending: PendingChanges, out: Callable[[str], None] = print) -> None:
    for change in pending.items():
        if change.declared:
            out(f"# {change.path}：{change.declared}")
        out(change.diff)


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


def refused_note(pending: PendingChanges) -> str:
    """给不出基线的改动被扣掉了：逐条说明原因（没有就返回空串）。

    「每一处落成改动都必须有 baseline」是那条不变量；**给不出 baseline 的
    改动不许落盘**是它允许的唯一一种「扣成果」。扣掉必须有声音——否则用户
    看到的是一个「安静地少做了一处」的运行，而他会以为那处改好了。

    **这就是 T13**（§3.2.2：给不出 Baseline 或整轮回退命令）。它与
    `pending_actions_note` 那一类**不是一回事**，两句必须分开说：
    这里扣的是**成果**（东西没写出来），那里拦的是**动作**（成果已经在盘上）。
    把它们记成一个数，正是设计稿点名不许混的那两种状态（§6 对
    `blocked_by_review` 的括注）。
    """
    refused = pending.refused()
    if not refused:
        return ""
    return (
        f"有 {len(refused)} 处改动**没有落盘**（T13：给不出回退），因为给不出回滚用的基线"
        "（不许留下一个退不回去的改动）：\n"
        + "".join(f"  - {item.path}：{item.reason}\n" for item in refused)
        + "这几处要写的内容仍在这次运行的正文里，但工作区没有被它改动。"
    )


def pending_actions_note(root: Path, session: str = "") -> str:
    """挂起待批的动作清单（没有就返回空串）。

    **它落在会话目录里，不是工作区里**（`config.state_dir` 按会话分），
    所以这里按「哪个会话」去找，而不是 `.agent/` 下一个固定名字——两个
    会话共用一份挂起清单会让 A 的批复去执行 B 的动作。

    `session` **必须由调用方给**：不给就等于按默认会话找。实测（真机探针
    `--session probe`）那份清单就在 `.agent/sessions/probe/review/` 里，
    而这里原先用默认会话找 `.agent/sessions/cli/review/`——找不到就返回空串，
    于是「有 1 个动作挂起待批」那句**静默消失**，用户以为这一轮是普通收工。
    写的那一侧（`loop` 的 `pending_path`）一直是带会话的，只有读的这一侧没有。
    """
    from spoolkit import session_state
    from spoolkit.config import Config

    path = (
        Config(
            project_root=Path(root),
            session=session or session_state.DEFAULT_SESSION,
        ).state_dir
        / "review"
        / "pending-actions.json"
    )
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    actions = payload.get("pending_actions") or []
    if not actions:
        return ""
    lines = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"  - {item.get('trigger', '?')}：{item.get('action') or '（结论级）'}"
            f"（摘要指纹 {item.get('fingerprint', '?')}）"
        )
    return (
        f"有 {len(lines)} 个动作**没执行、挂起待批**（成果已经在工作区里，"
        "拦的是动作不是成果）：\n"
        + "\n".join(lines)
        + f"\n挂起清单：{path.name}（批复后从挂起点续跑；"
        "要退掉已落盘的成果，用上面的 spool revert 命令——那是两件事）"
    )


def undo_command(pending: PendingChanges) -> str:
    """收尾报告里那一行可复制的撤销命令（没有待退的改动就返回空串）。"""
    if not len(pending):
        return ""
    return f"spool revert --run {pending.run_id}"


def verification_header(
    passed: bool | None, summary: str, undo: str = "", with_undo: bool = True
) -> str:
    """收尾处理里**打在 diff 之前**的那一段（未通过时才非空）。

    它和收尾报告开头那段是同一个要求的两处落点：终端里 diff 是随后由
    `settle` 打出来的，事件流里 diff 是 DIFF 事件——两种通道都必须让结论
    排在它们前面。没有这一段，一次「接受了一份未通过验证的改动」就是
    大 diff 先入眼、判词埋在下面。

    `with_undo=False` 是 **`--events` 那条通道**用的：那里撤销命令由收尾报告
    （FINAL 事件）给，而且它排在「等你确认 / 已应用」**之后**——这一段在 diff
    之前，多带一遍就等于同一条撤销信息在事件流里出现两次（问题②的最后一处
    残余）。终端那条路照旧带着它：那里用户要在这一大片 diff 之前就看见退路。
    """
    if passed is not False:
        return ""
    line = " ".join((summary or "（验证器没有给出细节）").split())[:300]
    # 与收尾报告里那一行同一个说法（问题④的动作化）：撤销是**一次动作**，
    # 不是"请你复制这条命令"。命令本身照旧逐字给出来（既有验收里有用例把它
    # 抄下来真执行），所以 `（网页里可直接点，桥里回 y）` 只是补上动作那条路。
    return (
        "⚠ 最终验证：未通过——下面这些改动没有通过验收，先看这一行再往下看 diff。\n"
        f"失败要点：{line}\n"
        + (
            f"撤销这一轮：{undo} （网页里可直接点，桥里回 y）\n"
            if (undo and with_undo)
            else ""
        )
    )
