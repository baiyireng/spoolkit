"""`spool init`：把目录做成工作区。

工作区 = 一个有 `.agent/` 的目录。那个目录里装的是这个项目的记忆、索引、
检查点与策略——所以"成为工作区"这件事要**轻**（一个 mkdir），但必须**可见**：
做完要告诉人刚刚发生了什么、下一步敲什么。

两个细节：

- **已经在工作区里就别再套一层**。在工作区的子目录里敲 `spool init`，十有八九
  是"我以为这里还不是"，而不是"我要在这个子目录里再建一个"。套起来的结果是
  记忆分叉，且极难看出来。所以往上找到 `.agent/` 时只登记、不新建。
- **`.gitignore` 要提一句**。`.agent/` 里是运行时数据，不该进版本库；但擅自
  改别人的 `.gitignore` 也不合适——有那份文件才追加，没有就只说一句。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from spoolkit import workspace

IGNORE_LINE = ".agent/"


def init_command(args: argparse.Namespace) -> int:
    target = Path(getattr(args, "path", None) or ".").resolve()
    if not target.is_dir():
        print(f"没有这个目录：{target}")
        return 2

    existing = workspace.find_root(target)
    if existing is not None:
        workspace.register(existing)
        if existing == target:
            print(f"这个目录已经是工作区：{target}")
        else:
            print(
                f"{target} 已经在工作区 {existing} 里面了，不再套一层。\n"
                f"（.agent 在 {existing / workspace.MARK}）"
            )
        _report_next()
        return 0

    (target / workspace.MARK).mkdir(parents=True, exist_ok=True)
    workspace.register(target)
    print(f"已初始化工作区：{target}")
    print(f"  状态目录：{target / workspace.MARK}（记忆、索引、检查点、策略）")
    print(f"  已登记到 {workspace.registry_path()}（所以在别的目录也能用 `spool approve`）")
    _note_gitignore(target)
    _report_next()
    return 0


def _note_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    if not path.is_file():
        print(f"  提示：这个目录没有 .gitignore；把 `{IGNORE_LINE}` 加进版本库忽略项")
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    if any(line.strip() in (IGNORE_LINE, ".agent") for line in text.splitlines()):
        return
    try:
        with path.open("a", encoding="utf-8") as handle:
            if text and not text.endswith("\n"):
                handle.write("\n")
            handle.write("\n# spoolkit 的运行时数据\n" + IGNORE_LINE + "\n")
    except OSError:
        return
    print(f"  已把 `{IGNORE_LINE}` 追加到 {path}")


def _report_next() -> None:
    print("下一步：`spool chat`（对话式），或 `spool run --goal \"...\"`（跑一个任务）。")
