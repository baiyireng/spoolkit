"""启动 Web UI 壳。"""

import argparse
from pathlib import Path

from spoolkit.web.server import serve


def _web_flags(args: argparse.Namespace) -> list[str]:
    """`--web` / `--no-web`。没写 `web` 当开；旧的 `no_web` 仍认。"""
    if getattr(args, "web", True) and not getattr(args, "no_web", False):
        return ["--web"]
    return ["--no-web"]


def extra_from_serve_args(args: argparse.Namespace) -> list[str]:
    """网页壳转发给 `run` 子进程的那组参数。hub 与 serve 共用。"""
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
    # QQ / 网页 / CLI 同一套：默认开联网；关掉才转 --no-web。
    extra += _web_flags(args)
    api = str(getattr(args, "web_search_api", "") or "").strip()
    if api:
        extra += ["--web-search-api", api]
    for domain in getattr(args, "web_allow", []) or []:
        extra += ["--web-allow", domain]
    for domain in getattr(args, "web_deny", []) or []:
        extra += ["--web-deny", domain]
    return extra


def serve_command(args: argparse.Namespace) -> int:
    """把命令行 agent 包在本地 HTTP 服务里。

    服务自己起子进程跑 CLI，所以这里的那些参数是**转发**给子进程的：
    换模型、换代理、换授权策略都在这里指定，不必再去改子进程的命令行。

    `--script` 也转发：网页壳的确认流程要用假模型**带脚本**本地走一遍。
    fake 不能生成代码；没有脚本时子进程会立刻退出。
    """
    serve(
        Path(args.root).resolve(),
        host=args.host,
        port=args.port,
        session=args.session,
        extra_args=extra_from_serve_args(args),
    )
    return 0
