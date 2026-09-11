"""启动 Web UI 壳。"""

import argparse
from pathlib import Path

from agents_dev.web.server import serve


def serve_command(args: argparse.Namespace) -> int:
    """把命令行 agent 包在本地 HTTP 服务里。

    服务自己起子进程跑 CLI，所以这里的那些参数是**转发**给子进程的：
    换模型、换代理、换授权策略都在这里指定，不必再去改子进程的命令行。

    `--script` 也转发：没有它就没法用假模型把确认流程走一遍，
    而「看到 diff → 点应用」恰恰是这个壳最需要本地验证的一段。
    """
    extra: list[str] = []
    for flag, value in (
        ("--provider", args.provider),
        ("--model", args.model),
        ("--script", args.script),
        ("--proxy", args.proxy),
        ("--policy", args.policy),
        ("--scope", args.scope),
    ):
        if value:
            extra += [flag, value]

    serve(
        Path(args.root).resolve(),
        host=args.host,
        port=args.port,
        session=args.session,
        extra_args=extra,
    )
    return 0
