"""解释器选择：一处实现，两处使用（工具执行与 bench 验收）。

为什么值得单独一层：裸 `python` / `python3` / `pytest` 到底该指向哪个解释器，
在「模型跑命令」和「bench 跑验收」里问的是**同一个问题**。而这个问题答错一次，
代价是整批结论全反——验收跑在缺测试依赖的解释器里，报告上只会多出几十个
「失败」，看起来像模型不行。实测烧掉过一整轮 50 题回归（uv tool 装的 spool
跑整批 0/50，同一个任务用仓库 .venv 跑是 50/50）。

顺序是「能用优先」，候选根由调用方按场景给：

1. 调用方给的第一个根（工具执行＝当前工作区；验收＝任务工作区）；
2. 调用方给的后续根（验收会补上 spoolkit 检出目录——uv tool / pipx 装的 spool
   自带一个缺测试依赖的解释器，而这个包的检出目录里通常有能跑测试的 `.venv`）；
3. 都没有就退回**当前解释器**：旧行为，任何环境下都还能用。

**候选的顺序就是优先级**，所以「谁在名单里、谁排前面」是各调用点自己的决定，
选择与映射的实现共用这一份。
"""

import sys
from collections.abc import Iterable
from pathlib import Path

# 虚拟环境里解释器的两种布局：Windows 是 .venv/Scripts/python.exe，
# POSIX 是 .venv/bin/python。
VENV_LAYOUTS = (
    ("Scripts", "python.exe"),
    ("bin", "python"),
)

# 需要换算成解释器的程序名。验收命令与模型写的命令用的是同一张表。
PYTHON_PROGRAMS = ("python", "python3", "pytest")


def venv_python(root: Path) -> Path | None:
    """这个根目录下的 `.venv` 解释器；没有就返回 None。"""
    for parts in VENV_LAYOUTS:
        candidate = root.joinpath(".venv", *parts)
        if candidate.exists():
            return candidate
    return None


def choose_interpreter(roots: Iterable[Path]) -> str:
    """按给定顺序挑第一个带虚拟环境的根；都没有就用当前解释器。

    刻意**不检查**候选解释器里有没有 pytest：那是「这道题能不能验收」的问题，
    由 `spoolkit.bench` 在整批开跑前单独探一次（见那里的 `probe_pytest`）。
    混在一起的话，工具执行会因为一个与它无关的原因换解释器。
    """
    for root in roots:
        found = venv_python(root)
        if found is not None:
            return str(found)
    return sys.executable


def resolve_python_argv(argv: list[str], roots: Iterable[Path]) -> list[str]:
    """把 argv 里的 python / python3 / pytest 换算成选中的解释器。

    映射本身不动（这是任务文件与模型都不该操心的环境细节）：
    `python <args>` → `<解释器> <args>`；`pytest <args>` → `<解释器> -m pytest <args>`。
    不是这三者的命令原样返回。
    """
    if not argv or argv[0] not in PYTHON_PROGRAMS:
        return argv
    interpreter = choose_interpreter(roots)
    if argv[0] == "pytest":
        return [interpreter, "-m", "pytest", *argv[1:]]
    return [interpreter, *argv[1:]]


def spoolkit_checkout_root() -> Path:
    """spoolkit 包所在的检出根：`spoolkit/` → `src/` → 检出根。

    `Path(spoolkit.__file__).resolve()` 是 `…/src/spoolkit/__init__.py`，
    往上两级就是检出根。

    非源码安装（躺在 site-packages 里）时这里推出来的多半不是检出根——
    那也没关系：`venv_python` 在那儿查不到 `.venv`，于是自然退回当前解释器。
    只是少一个候选，不会选错。
    """
    import spoolkit  # 局部导入：包初始化阶段不牵扯这个模块

    package = Path(spoolkit.__file__).resolve().parent
    return package.parent.parent
