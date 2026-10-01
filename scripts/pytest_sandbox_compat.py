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

实现**只有一份**，在 `src/spoolkit/sandbox_dirs.py`：bench 建任务工作区
（`spool bench` 的临时目录，同一个坑、同样的 WinError 5）走的是同一套函数，
所以两处不会各自长出一份、日后悄悄分叉。这里只负责「整进程装一次」。

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

import pathlib
import sys

# 这个插件是 pytest 的 `-p` 加载的，可能早于 pytest 的 `pythonpath=src`
# 生效，所以自己把仓库的 src 摆上——不然 `import spoolkit` 会看运气。
_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from spoolkit.sandbox_dirs import install  # noqa: E402

install()
