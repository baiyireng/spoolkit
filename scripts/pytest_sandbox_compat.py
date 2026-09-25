"""pytest 插件：在带文件沙箱的 Windows 上把 tmpdir 救回来。

## 症状

沙箱里跑 `pytest`，全套 1400+ 条**全部 error**，看着像代码全坏了：

    PermissionError: [WinError 5] 拒绝访问。: '...\\pytest-of-<用户>'

而且那个目录之后连 PowerShell 的 `Remove-Item` 都删不掉。

## 根因（2026-09-25 定位）

**用 `mode=0o700` 建出来的目录，之后 `os.scandir` / 往下建子目录一律被拒。**
最小复现（Python 3.14 / Windows + 沙箱）：

    p = Path(tmp) / "x"; p.mkdir(mode=0o700)     # 成功
    list(os.scandir(p))                          # PermissionError WinError 5

同一个目录用默认 mode 建出来就完全正常。而 pytest 的 tmpdir 机制**处处写死
0o700**（`_pytest/tmpdir.py` 的 `mkdir(mode=0o700)`、`make_numbered_dir(...,
mode=0o700)`，`tempfile.mkdtemp` 也是 0o700），于是每条用例都在 setup 阶段就炸。

## 这个插件做什么

把所有 `mkdir` / `mkdtemp` 的 mode 统一抬成 0o777（Windows 上本来就不看这个
位，这里只是别让沙箱把目录锁死）。**不改测试、不改产品代码。**

## 怎么用

用 `scripts/pytest.ps1`（推荐，它把下面几条都设好了）：

    scripts\\pytest.ps1 -Repo .worktrees\\ws-session-kernel

手动等价写法：

    $env:PYTHONPATH="<仓库>\\src;<仓库>\\scripts"
    $env:PYTHONPATH="$env:PYTHONPATH"       # 注意：不是仓库内的 .tmp，原因见 pytest.ps1
    $env:PYTEST_DEBUG_TEMPROOT="<仓库外、刚新建的空目录>"
    $env:PYTEST_ADDOPTS="-p pytest_sandbox_compat"
    python -m pytest -q -p no:cacheprovider -p pytest_sandbox_compat

`PYTEST_ADDOPTS` 那一条不能省：bench 的用例会**再派生 pytest 子进程**去验收
（`tests/bench/test_task_integrity.py`，50 条），子进程只有靠环境变量才带得上
这个插件，否则那 50 条照样全红。
"""

import os
import pathlib
import tempfile

_orig_path_mkdir = pathlib.Path.mkdir
_orig_os_mkdir = os.mkdir
_orig_mkdtemp = tempfile.mkdtemp


def _path_mkdir(self, mode=0o777, parents=False, exist_ok=False):  # noqa: ANN001
    return _orig_path_mkdir(self, 0o777, parents, exist_ok)


def _os_mkdir(path, mode=0o777, *, dir_fd=None):  # noqa: ANN001
    return _orig_os_mkdir(path, 0o777, dir_fd=dir_fd)


def _mkdtemp(suffix=None, prefix=None, dir=None):  # noqa: ANN001
    return _orig_mkdtemp(suffix, prefix, dir)


pathlib.Path.mkdir = _path_mkdir
os.mkdir = _os_mkdir
tempfile.mkdtemp = _mkdtemp
