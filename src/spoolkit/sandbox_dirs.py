"""受限环境（Windows + 文件沙箱）里建目录的共用兜底。

## 症状

带文件沙箱的 Windows 上，**用 `mode=0o700` 建出来的目录当场就不可访问**：

    p = Path(tmp) / "x"
    p.mkdir(mode=0o700)                      # 成功
    list(os.scandir(p))                      # PermissionError: [WinError 5] 拒绝访问

而且**事后抬权限也救不回来**——`os.chmod(p, 0o777)` 同样是 WinError 5。
所以只能**创建那一刻**就把 mode 抬上去。

`tempfile.mkdtemp`（`TemporaryDirectory` 走它）和 pytest 的 tmpdir 都写死
`0o700`，于是目录建出来就不能用，撞在 `copytree` 之类的地方：

    shutil.copytree(source, workdir)
    PermissionError: [WinError 5] 拒绝访问。: '...\\spool-bench-xxxx\\project'

## 这一招

把 `mkdir` / `mkdtemp` 的 mode 统一抬成 0o777（Windows 上本来就不看这个位，
这里只是别让沙箱把目录锁死）。**同一份实现，三处共用**：

- `scripts/pytest_sandbox_compat.py`：pytest 的创建点不由我们调，整进程装一次；
- `spoolkit.bench`：只在建任务工作区那一下装（`make_accessible_temp_dir`），
  建完就还原——理由见 `relaxed_mode`；
- `spoolkit.tools.trial`：自动验证的试跑副本，同一个坑。

关键在 `relaxed_os_mkdir`：`tempfile.mkdtemp` 内部调的就是 `os.mkdir`，
把 `os.mkdir` 抬了，等于把所有 `mkdtemp` 建出来的目录一起抬了。

## 怎么用

    from spoolkit.sandbox_dirs import make_accessible_temp_dir
    import shutil

    root = make_accessible_temp_dir("spool-bench-")   # 建出来就能遍历、能读写
    try:
        ...
    finally:
        # 清理必须用 shutil.rmtree 的 ignore_errors，不能靠 TemporaryDirectory：
        # 里面一旦有个碰不得的 0o700 子目录（pytest 自己就会留下这种），
        # TemporaryDirectory 的处理器会去 chmod 它，而 chmod 同样被拒 → 照抛。
        shutil.rmtree(root, ignore_errors=True)

整进程生效（pytest 插件那条路）：

    from spoolkit import sandbox_dirs

    sandbox_dirs.install()
"""

import contextlib
import os
import pathlib
import tempfile
from collections.abc import Iterator

# Windows 不看这个位，抬它只是为了别让沙箱把目录锁死。
RELAXED_DIR_MODE = 0o777

# 装补丁之前的那一份。测试要「摘掉插件装的那层补丁」时拿它，不必另抄一份实现。
raw_path_mkdir = pathlib.Path.mkdir
raw_os_mkdir = os.mkdir
raw_mkdtemp = tempfile.mkdtemp


def relaxed_path_mkdir(  # noqa: ANN001
    self, mode: int = RELAXED_DIR_MODE, parents: bool = False, exist_ok: bool = False
):
    """`pathlib.Path.mkdir` 的替身：mode 一律抬到 0o777。"""
    return raw_path_mkdir(self, RELAXED_DIR_MODE, parents, exist_ok)


def relaxed_os_mkdir(path, mode: int = RELAXED_DIR_MODE, *, dir_fd=None):  # noqa: ANN001
    """`os.mkdir` 的替身。**这个才是把手**：`tempfile.mkdtemp` 内部走它，
    抬了它，所有 `mkdtemp` 建出来的目录就跟着能用了。"""
    return raw_os_mkdir(path, RELAXED_DIR_MODE, dir_fd=dir_fd)


def relaxed_mkdtemp(suffix=None, prefix=None, dir=None):  # noqa: ANN001
    """`tempfile.mkdtemp` 的替身：它自己没有 mode 参数，真正抬权限的是
    上面的 `os.mkdir`。留着它只为把「谁被改过」摆明白。"""
    return raw_mkdtemp(suffix, prefix, dir)


def install() -> None:
    """整进程生效，可重复调（幂等）。"""
    pathlib.Path.mkdir = relaxed_path_mkdir
    os.mkdir = relaxed_os_mkdir
    tempfile.mkdtemp = relaxed_mkdtemp


@contextlib.contextmanager
def relaxed_mode() -> Iterator[None]:
    """只在 with 块内生效，出块还原。

    刻意**只还原自己装的那一份**：别的补丁（比如另一个加载器）在这期间
    改了同一个名字，就不该被我们踩掉。
    """
    saved = (pathlib.Path.mkdir, os.mkdir, tempfile.mkdtemp)
    install()
    try:
        yield
    finally:
        if pathlib.Path.mkdir is relaxed_path_mkdir:
            pathlib.Path.mkdir = saved[0]
        if os.mkdir is relaxed_os_mkdir:
            os.mkdir = saved[1]
        if tempfile.mkdtemp is relaxed_mkdtemp:
            tempfile.mkdtemp = saved[2]


def make_accessible_temp_dir(prefix: str) -> pathlib.Path:
    """建一个临时目录：**创建那一刻**就把 mode 放宽。清理由调用方自己收。

    裸用 `tempfile.mkdtemp` 在沙箱里同样是建出来就不能用（它写死 0o700）。
    这里刻意**不**返回 `TemporaryDirectory`：它的 `ignore_cleanup_errors=True`
    在本沙箱里挡不住清理错误（详见模块开头）。清理请用
    `shutil.rmtree(path, ignore_errors=True)`。
    """
    with relaxed_mode():
        return pathlib.Path(tempfile.mkdtemp(prefix=prefix))
