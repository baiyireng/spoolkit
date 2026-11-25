"""沙箱试跑。

授权模型要求「先看 diff 再落盘」，而验证循环要求「改完立刻跑测试」——
两者对「什么时候写到磁盘」的要求正好相反。结果是：模型刚提出的修改还没落盘，
它去跑测试，测到的是旧代码，于是误判自己的修复无效，继续去改一个已经改对的函数。

这一层把矛盾解开：把待授权的改动应用到一份临时副本上，在副本里跑。
真实工作区在整个过程中一动不动，只有最终确认之后才会被写入。

副本排除虚拟环境与缓存目录——它们体积大、且与原目录等价，复制它们既慢又没有意义。
运行测试时仍然使用原目录的解释器，所以依赖照样找得到。
"""

import os
import shutil
import subprocess
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

# pytest 在 basetemp 下租的目录名（`_pytest.pathlib.make_numbered_dir`）。
# 建的时候写死 0o700，在受限环境里连复制都过不去——见 `_ignore` 的注释。
PYTEST_TEMP_PREFIX = "pytest-cache-files-"

#: 复制时跳过、但**用链接接回副本**的依赖目录。
#:
#: 为什么要多这一层：排除虚拟环境/依赖目录是为了省时间，可"排除"对 Python 只是少一层壳
#: （解释器与 site-packages 在全局，`python -m pytest` 照样找得到依赖），对 Node 却是
#: **把依赖整个拿掉**——`npm run build`、`node node_modules/vite/bin/vite.js` 在副本里
#: 必然失败。实测（Vue 项目）：模型看到 `Cannot find module '<试跑副本>\…\node_modules\vite'`
#: 后判成"依赖没装"，接着 `npm install`（装进临时副本，随即被清理）、`npx vite build`
#: （拉了个与项目不同版本的 vite），白烧 6 步预算，最后把"构建失败"写进了结论。
#: 链接不占空间、不复制，副本里的相对路径照旧成立，`node_modules/.bin` 也照旧能用。
LINKED_DIRS = (".venv", "venv", "node_modules")


def _link_dir(source: Path, target: Path) -> str | None:
    """把 source 接进 target（符号链接优先，Windows 上退回目录联接）。

    返回失败原因；成功返回 None。失败**不抛**：接不上就退回"副本里没有它"的老行为，
    但要把这件事记下来交给调用方说出去（见 `exec.py::_with_trial_notice`）。
    """
    try:
        os.symlink(source, target, target_is_directory=True)
        return None
    except OSError as exc:
        first = f"{type(exc).__name__}: {exc}"
    if os.name == "nt":
        # 没有开发者模式/建符号链接特权时 symlink 会被拒；目录联接不需要那些特权。
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if done.returncode == 0:
            return None
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        return f"{first}；mklink: {detail[-1] if detail else done.returncode}"
    return first


def _unlink_dir(path: Path) -> None:
    """摘掉一个链接/junction，**不跟着往里删**。

    实测确认过：`os.rmdir` 对符号链接目录与目录联接都只摘链接本身，真实目录与内容
    一个字节都不动（这是清理副本时最要紧的一条——把 `node_modules` 顺着链接删了，
    等于替用户删掉他的依赖）。
    """
    try:
        os.rmdir(path)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass


def _link_dependency_dirs(root: Path, copy_root: Path) -> tuple[list[str], list[str]]:
    """把依赖目录接进副本。返回（已接入, 没接上）。"""
    linked: list[str] = []
    skipped: list[str] = []
    for name in LINKED_DIRS:
        source = root / name
        if not source.is_dir():
            continue
        target = copy_root / name
        if target.exists():
            continue
        if _link_dir(source, target) is None:
            linked.append(name)
        else:
            skipped.append(name)
    return linked, skipped


def _ignore(directory: str, names: list[str]) -> set[str]:
    """复制时跳过虚拟环境、缓存与**pytest 自己租的临时目录**。

    最后那一条（`pytest-cache-files-*`）是实测踩出来的：受限环境里
    `tempfile.mkdtemp` 建出来的目录是 `0o700`，`shutil.copytree` 复制到它时
    直接 `[WinError 5] 拒绝访问`——于是自动验证报的是「代码跑不起来」，
    而真实原因是「副本里有两个别人建的、权限受限的空目录」。它们是与
    `.pytest_cache` 同类的东西：**上一轮跑测试留下的痕迹，不是项目内容**，
    复制它们既没有意义，又会让整次验证失败。
    """
    return {
        name
        for name in names
        if name in EXCLUDED_DIRS or name.startswith(PYTEST_TEMP_PREFIX)
    }


def copy_project(root: Path, target: Path) -> None:
    """把项目复制到 target，跳过虚拟环境与缓存。"""
    shutil.copytree(root, target, ignore=_ignore)


@contextmanager
def trial_workspace(
    root: Path,
    changes: Sequence[PendingChange],
    *,
    report: dict | None = None,
) -> Iterator[Path]:
    """产出应用了待授权改动的临时项目，退出时清理。

    `report` 给了就把这次试跑的关键事实写进去（`root` / `linked` / `skipped`）——
    调用方要把"命令跑在副本里、依赖目录接没接上"说给模型听（见 `exec.py`），
    否则模型会把副本缺依赖误判成"工作区没装依赖"。
    """
    # 建目录这一步在受限环境（Windows + 文件沙箱）里是**会炸的**：mkdtemp 写死
    # 0o700，建出来的目录当场不可访问，下面的 copytree 连 makedirs 都过不去，
    # 自动验证于是报「环境问题」——而这条路的成败正是回归集要量的东西。
    # 抬 mode 的实现与 bench、pytest 插件共一份，见 spoolkit.sandbox_dirs。
    base = sandbox_dirs.make_accessible_temp_dir("spool-trial-")
    copy_root = base / "project"
    made_links: list[Path] = []
    try:
        copy_project(root, copy_root)
        linked, skipped = _link_dependency_dirs(root, copy_root)
        made_links = [copy_root / name for name in linked]
        if report is not None:
            report.update({"root": copy_root, "linked": linked, "skipped": skipped})
        for change in changes:
            if Path(change.path).is_absolute():
                # 关联目录的改动不进试跑副本：副本只代表主工作区。
                continue
            target = copy_root / change.path
            if getattr(change, "deleted", False):
                # 试跑副本要反映「现在文件已经没了」：留着它，模型跑测试时
                # 测到的是一份它在自己那边已经删掉的文件。
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.new_text, encoding="utf-8")
        yield copy_root
    finally:
        # 先摘链接再删目录树：顺着链接删下去会删到真实依赖。
        for link in made_links:
            _unlink_dir(link)
        shutil.rmtree(base, ignore_errors=True)

