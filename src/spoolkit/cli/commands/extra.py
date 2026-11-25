"""`spool extra`：在本机把关联目录写进配置。

远程聊天里不能授权（小模型分不清危险操作）。要让 agent 碰工作区外的目录，
只能在跑桥的那台机器上显式执行这条命令。名单在 `.agent/read-roots.json`。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from spoolkit import readroots
from spoolkit.cli.term import print_err, print_out


def extra_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    action = getattr(args, "action", None) or "list"
    target = str(getattr(args, "path", "") or "").strip()

    if action in ("add", "remove") and not target:
        print_err(
            f"spool extra {action} 需要一个目录。",
            markup=False,
            style="bad",
        )
        return 2

    if action == "add":
        before = readroots.load(root)
        roots, message = readroots.add(root, target)
        style = "ok" if "已关联" in message or "已经在列表里" in message else "bad"
        print_out(message, markup=False, style=style)
        return 0 if roots != before or "已经在列表里" in message else 2
    if action == "remove":
        before = readroots.load(root)
        roots, message = readroots.remove(root, target)
        style = "ok" if roots != before else "bad"
        print_out(message, markup=False, style=style)
        return 0 if roots != before else 2
    print_out(readroots.render(root))
    return 0
