"""分级授权的确认环节。

读操作完全自由；写操作先出 diff 再问一次。这个「问一次」几乎不花时间，
但它把「模型改错代码」的代价从「你得从 git 里捞回来」降到「按一下 n」。

ask 做成可注入的，测试才能覆盖两种分支而不需要真的等待输入。
"""

from typing import Callable

from pathlib import Path

from agents_dev.tools.edit import PendingChanges, save_baseline


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
        print(change.diff)
    print(f"共 {len(changes)} 处修改待确认。")

    answer = ask("应用这些修改吗？[y/N] ").strip().lower()
    if answer in ("y", "yes"):
        if baseline_path is not None:
            save_baseline(baseline_path, pending.baseline())
        return True, pending.apply()
    pending.discard()
    return False, []
