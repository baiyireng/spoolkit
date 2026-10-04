"""回滚命令。

两条路，粒度不同，退路是同一条（都读 `tools/edit.py` 那套 baseline）：

- 不给参数：**上一次写入的改动**（`.agent/last_change.json`），老行为一个字不改。
- `--run <id>` / `--last-run`：**整轮**——一次运行里落成的每一处都退回去。

为什么要有第二条路（真实经历）：用户接受了一份「最终验证未通过」的改动，事后
反悔，用系统外的手段（自己找备份 / 编辑器）回退了——而 `spool revert` 明明
存在。三个缺口里最要命的是粒度：一轮里常常改 N 处（多步 `replace_lines`），
只认「上一次」的话，要退回整轮得反复 revert，还得自己理清顺序。

整轮回退的三条硬要求都落在这一条路上：**按改动逆序**逐处退、**逐处报告**
（成功／跳过／冲突）、**冲突不静默**（落盘之后又被改过的那一处拒绝覆盖，
逐条列出并说明人工怎么处理）。
"""

import argparse
import sys
from pathlib import Path

from spoolkit import runlog
from spoolkit.tools.edit import load_baseline, revert


def revert_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).resolve()
    if args.run or args.last_run:
        return _revert_run(project_root, args)
    return _revert_last_write(project_root)


def _revert_last_write(project_root: Path) -> int:
    """既有行为：退回上一次写入的改动。"""
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
        print(f"已恢复：{path}")
    print(f"共恢复 {len(touched)} 个文件。")
    return 0


def _revert_run(project_root: Path, args: argparse.Namespace) -> int:
    """整轮回退：`--run <id>` 或 `--last-run`。"""
    run_id = str(args.run or "")
    if not run_id:
        run_id = runlog.latest_run_id(project_root) or ""
        if not run_id:
            print(
                "没有运行记录：这个工作区里还没有「落成过改动」的一轮。"
                "（记录写在 .agent/runs/<id>/run.json）",
                file=sys.stderr,
            )
            return 2
    result = runlog.revert_run(project_root, run_id, dry_run=bool(args.dry_run))
    if result is None:
        known = runlog.run_ids(project_root)
        hint = (
            "现有的是：" + "、".join(known[:5])
            if known
            else "这个工作区里还没有任何运行记录。"
        )
        print(f"没有这条运行记录：{run_id}。{hint}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"预演：这一轮（{run_id}）会这样做（磁盘一个字没动）：")
    else:
        print(f"回退这一轮：{run_id}")
    for step in result.steps:
        print("  " + step.render())

    print(
        f"共 {len(result.steps)} 处："
        f"恢复/删除 {result.count(runlog.DONE)}、"
        f"跳过 {result.count(runlog.SKIPPED)}、"
        f"冲突 {result.count(runlog.CONFLICT)}"
        + (f"、失败 {result.count(runlog.FAILED)}" if result.count(runlog.FAILED) else "")
        + ("（预演，没有落盘）" if result.dry_run else "")
        + "。"
    )
    if result.conflicts:
        # **冲突不静默**，而且要给出人工怎么做——只报「有冲突」等于把问题
        # 还给用户，而他现在的状态正是「不知道怎么退回去」。
        print("冲突的这几处**没有被覆盖**（它们在这一轮之后又被改过，"
              "直接写回去会冲掉后来的改动）：")
        for step in result.conflicts:
            print(f"  - {step.path}：{step.detail}")
        print(
            "人工处理：先把这些文件现在的版本备份出去（或加进版本库），"
            "再决定是保留后来的改动、还是手工把改动前的原文写回去。"
            "已经退成功的那些不受影响；处理完再跑一次同一条命令，"
            "退过的会被跳过、没退的接着退。"
        )
    if args.dry_run:
        return 0
    # 有冲突时返回非零：脚本化使用的人必须能从退出码看出「没有全部退回去」。
    return 3 if result.conflicts else 0
