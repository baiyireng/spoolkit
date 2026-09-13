"""入口这一层要有：能报版本、重定向输出不崩。

后者是真踩过的坑：Windows 默认编码 cp936，而输出一旦被重定向或走管道
（`agents-dev run … > log.txt`、任何子进程捕获、Web 壳），Python 就按 locale
写字节——进度块里的 `✓` 编不出来，于是
`UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'`
把整个运行打断。它在一次 50 题的长跑里真的发生过。
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _env_without_io_encoding() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def test_版本号能打印() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "agents_dev.cli.app", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env_without_io_encoding(),
        timeout=60,
    )
    assert proc.returncode == 0
    assert "agents-dev" in proc.stdout


def test_输出被重定向时非GBK字符不再打断运行() -> None:
    """stdout 是管道时（重定向/子进程捕获）打印 ✓ 必须成功。"""
    code = (
        "from agents_dev.cli.app import configure_stdio;"
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
