"""授权策略命令：查看或调整。"""

import argparse
from pathlib import Path

from spoolkit.cli.term import escape, print_out
from spoolkit.policy import (
    POLICIES,
    describe as describe_policy,
    load_policy,
    policy_path,
    save_policy,
)


def policy_command(args: argparse.Namespace) -> int:
    """查看或调整授权策略。调整会持久化，下一轮自动沿用。"""
    project_root = Path(args.root).resolve()
    path = policy_path(project_root)

    if args.new_policy:
        save_policy(path, args.new_policy)
        print_out(
            f"[ok]授权策略已设为 [accent]{escape(args.new_policy)}[/]：[/]"
            f"{escape(describe_policy(args.new_policy))}"
        )
        print_out(f"[muted]已保存到 {escape(str(path))}，下一轮起生效。[/]")
        return 0

    current = load_policy(path)
    print_out(
        f"[muted]当前授权策略：[/][accent]{escape(current)}[/]"
    )
    print_out(f"  {escape(describe_policy(current))}")
    print_out("[muted]可选：[/]")
    for name in POLICIES:
        mark = " [accent]← 当前[/]" if name == current else ""
        print_out(
            f"  [accent]{escape(name)}[/][muted]:[/] "
            f"{escape(describe_policy(name))}{mark}"
        )
    return 0
