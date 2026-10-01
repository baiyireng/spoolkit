"""沙箱试跑。

授权模型要求「先看 diff 再落盘」，而验证循环要求「改完立刻跑测试」——
两者对「什么时候写到磁盘」的要求正好相反。结果是：模型刚提出的修改还没落盘，
它去跑测试，测到的是旧代码，于是误判自己的修复无效，继续去改一个已经改对的函数。

这一层把矛盾解开：把待授权的改动应用到一份临时副本上，在副本里跑。
真实工作区在整个过程中一动不动，只有最终确认之后才会被写入。

副本排除虚拟环境与缓存目录——它们体积大、且与原目录等价，复制它们既慢又没有意义。
运行测试时仍然使用原目录的解释器，所以依赖照样找得到。
"""

import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from spoolkit import sandbox_dirs
from spoolkit.tools.edit import PendingChange

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".agent",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".idea",
        ".vscode",
    }
)


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in EXCLUDED_DIRS}


def copy_project(root: Path, target: Path) -> None:
    """把项目复制到 target，跳过虚拟环境与缓存。"""
    shutil.copytree(root, target, ignore=_ignore)


@contextmanager
def trial_workspace(root: Path, changes: Sequence[PendingChange]) -> Iterator[Path]:
    """产出应用了待授权改动的临时项目，退出时清理。"""
    # 建目录这一步在受限环境（Windows + 文件沙箱）里是**会炸的**：mkdtemp 写死
    # 0o700，建出来的目录当场不可访问，下面的 copytree 连 makedirs 都过不去，
    # 自动验证于是报「环境问题」——而这条路的成败正是回归集要量的东西。
    # 抬 mode 的实现与 bench、pytest 插件共一份，见 spoolkit.sandbox_dirs。
    base = sandbox_dirs.make_accessible_temp_dir("spool-trial-")
    copy_root = base / "project"
    try:
        copy_project(root, copy_root)
        for change in changes:
            if Path(change.path).is_absolute():
                # 关联目录的改动不进试跑副本：副本只代表主工作区。
                continue
            target = copy_root / change.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.new_text, encoding="utf-8")
        yield copy_root
    finally:
        shutil.rmtree(base, ignore_errors=True)

