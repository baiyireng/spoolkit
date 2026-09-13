"""授权策略命令：查看或调整。"""

import argparse
from pathlib import Path

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
        print(f"授权策略已设为 {args.new_policy}：{describe_policy(args.new_policy)}")
        print(f"已保存到 {path}，下一轮起生效。")
        return 0

    current = load_policy(path)
    print(f"当前授权策略：{current}")
    print(f"  {describe_policy(current)}")
    print("可选：")
    for name in POLICIES:
        print(f"  {name}: {describe_policy(name)}")
    return 0

