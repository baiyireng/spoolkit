"""入口这一层要有：能报版本、重定向输出不崩。

后者是真踩过的坑：Windows 默认编码 cp936，而输出一旦被重定向或走管道
（`spool run … > log.txt`、任何子进程捕获、Web 壳），Python 就按 locale
写字节——进度块里的 `✓` 编不出来，于是
`UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'`
把整个运行打断。它在一次 50 题的长跑里真的发生过。
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_包里能拿到版本号() -> None:
    """`spoolkit.__version__` 必须存在——它是**每个子进程**的启动条件。

    踩过：`src/spoolkit/__init__.py` 被误写成一个空壳（只剩一行注释），症状是所有
    子进程都起不来（`from spoolkit import __version__` → ImportError），而当时
    唯一能发现它的测试是"起一个子进程跑 --version"——慢、且在 pytest 里排得靠后。
    这条在进程内直接看一眼，失败信息也直指病根。
    """
    import spoolkit

    assert isinstance(spoolkit.__version__, str)
    assert spoolkit.__version__


def _env_without_io_encoding() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def test_版本号能打印() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "spoolkit.cli.app", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env_without_io_encoding(),
        timeout=60,
    )
    assert proc.returncode == 0
    assert "spool" in proc.stdout


def test_输出被重定向时非GBK字符不再打断运行() -> None:
    """stdout 是管道时（重定向/子进程捕获）打印 ✓ 必须成功。"""
    code = (
        "from spoolkit.cli.app import configure_stdio;"
        "configure_stdio();"
        "print('✓ 通过')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env_without_io_encoding(),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "✓ 通过" in proc.stdout
