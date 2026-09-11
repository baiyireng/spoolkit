"""回滚命令：把上一次写入的文件恢复到改动前。"""

import argparse
import sys
from pathlib import Path

from agents_dev.tools.edit import load_baseline, revert


def revert_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).resolve()
    baseline_path = project_root / ".agent" / "last_change.json"
    baseline = load_baseline(baseline_path)
    if baseline is None:
        print("没有可回滚的记录。", file=sys.stderr)
        return 2

    touched = revert(project_root, baseline)
    # 回滚后消费掉基线：留着它会让下一次 revert 重复恢复同一批文件，
    # 把之后的手工改动一起冲掉。
    baseline_path.unlink()
    for path in touched:
        print(f"已恢复: {path}")
    print(f"共恢复 {len(touched)} 个文件。")
    return 0

