"""`spool help [子命令]`：等价于 `spool --help` / `spool <子命令> --help`。

用户敲的就是 `spool help`——而它原先不在子命令表里，于是 argparse 直接回一句
`invalid choice: 'help'`。补法**不新造一套帮助系统**：照 argparse 自己的习惯来，
把这条命令转发成一次带 `--help` 的重新解析，`SystemExit` 的码原样当退出码
（成功是 0，认不出的子命令还是 argparse 那套 2 与它自己的报错）。

为什么不在这里自己 `print_help()`：那要求这条命令拿到**顶层 parser 与子 parser
表**（`argparse` 没有公开 API 取它们，只能翻 `_actions`）。重新解析一次用的是
同一份参数定义，帮助文本与 `spool <子命令> --help` **逐字相同**——两处不会漂移。
"""

from __future__ import annotations

import argparse


def help_command(args: argparse.Namespace) -> int:
    topic = str(getattr(args, "topic", "") or "").strip()
    # 在函数里 import：`cli.app` 是**调用方**，模块级 import 会成环。
    from spoolkit.cli.app import main

    argv = [topic, "--help"] if topic else ["--help"]
    try:
        main(argv)
    except SystemExit as exc:  # argparse 打印完帮助就用 SystemExit 收场
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    return 0
