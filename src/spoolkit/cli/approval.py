"""分级授权的确认环节。

读操作完全自由；写操作先出 diff 再问一次。这个「问一次」几乎不花时间，
但它把「模型改错代码」的代价从「你得从 git 里捞回来」降到「按一下 n」。

ask 做成可注入的，测试才能覆盖两种分支而不需要真的等待输入。
"""

from typing import Callable

from pathlib import Path

from spoolkit.tools.edit import PendingChanges, save_baseline


def review_and_apply(
    pending: PendingChanges,
    ask: Callable[[str], str] = input,
    baseline_path: Path | None = None,
) -> tuple[bool, list[str]]:
    """展示全部 diff 并询问是否应用。返回（是否应用，已写入的路径）。

    应用前先把改动前快照写进基线文件：退路必须在动手之前就准备好，
    事后补是补不出来的。
    """
    changes = pending.items()
    if not changes:
        return False, []

    for change in changes:
        _show_declared(change)
        print(change.diff)
    print(f"共 {len(changes)} 处修改待确认。")

    answer = ask("应用这些修改吗？[y/N] ").strip().lower()
    if answer in ("y", "yes"):
        if baseline_path is not None:
            save_baseline(baseline_path, pending.baseline())
        return True, pending.apply()
    pending.discard()
    return False, []


def apply_with_audit(
    pending: PendingChanges,
    baseline_path: Path | None = None,
) -> list[str]:
    """计划级授权下的自动落盘。

    自动不等于无声：diff 照样完整打印，基线照样记录。区别只在于不再等一次确认。
    授权的边界来自计划里那一步声明的 scope，而不是「这次运行整体被信任」。
    """
    changes = pending.items()
    if not changes:
        return []

    for change in changes:
        _show_declared(change)
        print(change.diff)
    if baseline_path is not None:
        save_baseline(baseline_path, pending.baseline())
    written = pending.apply()
    print(f"已自动应用 {len(written)} 处修改（计划级授权，可用 revert 回滚）。")
    return written


def _show_declared(change) -> None:
    """把「这处改动是怎么来的」打在 diff 前面。

    声明式重写的留档（为什么大改、哪些项确认保留、**哪些项被有意丢弃及理由**）
    原先只在模型的工具输出里出现过：用户看到的 diff 一模一样，看不出哪一项是
    被有意丢掉的。留痕不被读到，就不叫留痕。
    """
    if getattr(change, "declared", ""):
        print(f"# {change.path}：{change.declared}")
