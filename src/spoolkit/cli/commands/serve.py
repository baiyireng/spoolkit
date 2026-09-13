"""启动 Web UI 壳。"""

import argparse
from pathlib import Path

from spoolkit.web.server import serve


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
        ("--base-url", args.base_url),
        ("--script", args.script),
        ("--proxy", args.proxy),
        ("--policy", args.policy),
        ("--scope", args.scope),
    ):
        if value:
            extra += [flag, value]
    for root in getattr(args, "allow_read", []) or []:
        extra += ["--allow-read", root]
    # 联网取用也要转发：网页壳里点着按钮查资料时，子进程得有这两个工具。
    if getattr(args, "web", False):
        extra += ["--web"]
    for domain in getattr(args, "web_allow", []) or []:
        extra += ["--web-allow", domain]
    for domain in getattr(args, "web_deny", []) or []:
        extra += ["--web-deny", domain]

    serve(
        Path(args.root).resolve(),
        host=args.host,
        port=args.port,
        session=args.session,
        extra_args=extra,
    )
    return 0
