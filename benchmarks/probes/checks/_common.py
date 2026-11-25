"""探针判据的公共部分（只用标准库）。

每个判据都从命令行拿一个 `--root`（沙箱副本），自己决定跑什么、看什么，
最后打一行 `CHECK-OK` 或 `CHECK-FAIL: ...` 并以 0 / 1 退出。

**判据必须是能证伪的**：同一份判据在未修改的种子上要红、在人工修好的版本上
要绿。`tests/test_probe_runner.py` 逐条钉住这件事，所以判据里不许写
「看起来像做完了」这种无法判定的话。
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# 判据自己的输出必须编码得出来：Windows 控制台默认 cp936，遇到 `¥` 这类字符
# 会直接抛 UnicodeEncodeError——那会把「判据不成立」变成「判据崩了」。两者都是
# 红的，但原因完全不同，报告里要分得清。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - 被包装过的流
        pass

OK_PREFIX = "CHECK-OK"
FAIL_PREFIX = "CHECK-FAIL"

# 扫描文本时要跳过的目录：探针沙箱里的 .agent 是 harness 自己写的，
# __pycache__ 是字节码，都不算「源码里还留着旧名字」。
SKIP_DIRS = {".agent", "__pycache__", ".git", ".pytest_cache", ".mypy_cache"}

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".rst",
    ".json",
    ".jsonl",
    ".toml",
    ".cfg",
    ".ini",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".ts",
    ".yaml",
    ".yml",
    ".sh",
    ".ps1",
}


def parse_args(description=""):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--root", required=True, help="探针沙箱副本的根目录")
    return parser.parse_args()


def child_env(root):
    """给判据里派生出去的 python 子进程准备的运行环境。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    # 中文输出在 Windows 上默认走 cp936，会把判据证据糊成乱码。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run(argv, cwd, timeout=180, env=None):
    """跑一条命令，把两个流都收成文本。超时不抛异常，原样交回结果。"""
    try:
        done = subprocess.run(
            [str(item) for item in argv],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return done.returncode, done.stdout or "", done.stderr or "", False
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return None, stdout, stderr, True


def iter_text_files(root):
    """沙箱里所有值得扫一遍的文本文件。"""
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            yield path


class Checker:
    """收集「哪几条不成立」，最后统一判定。"""

    def __init__(self, name):
        self.name = name
        self.failures = []
        self.evidence = []

    def note(self, line):
        self.evidence.append(f"  · {line}")

    def require(self, condition, message):
        if condition:
            self.note(f"通过：{message}")
        else:
            self.failures.append(message)
        return bool(condition)

    def finish(self):
        for line in self.evidence:
            print(line)
        if self.failures:
            for line in self.failures:
                print(f"{FAIL_PREFIX}: {line}")
            print(f"{FAIL_PREFIX}: 共 {len(self.failures)} 条不成立")
            return 1
        print(f"{OK_PREFIX}: {self.name}")
        return 0
