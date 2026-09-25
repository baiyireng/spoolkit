"""`spool hub`：统一起网页壳，声明了通道才再拉 bridge。

产品内核只干这两件事。llama-server、SOCKS 隧道是本机外接，不在这里启动，
也不写死某台机器的地址。退出时只杀 hub 自己拉起的孩子。

用户配置里常见两段 `[[service]]`：`llama` 是本机语言模型（按 `provider` /
`base_url` 探活）；`tunnel` 是通道出网的 SOCKS，给 `--bridge-proxy` 用，
不是模型的 `--proxy`。拉起它们是 `spool service` 的事。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from spoolkit.cli.commands.serve import extra_from_serve_args
from spoolkit.kernel.target import resolve_target
from spoolkit.web.server import serve

CHANNELS = ("fake", "telegram", "wecom", "qqbot")
CHILD_WAIT = 8.0


def bridge_child_argv(args: argparse.Namespace) -> list[str]:
    """拼出要拉起的 `spool bridge` 命令行。没给通道就空列表。"""
    channel = str(getattr(args, "channel", "") or "").strip()
    if not channel:
        return []
    root = Path(getattr(args, "root", ".")).resolve()
    argv = [
        sys.executable,
        "-m",
        "spoolkit.cli.app",
        "bridge",
        "--channel",
        channel,
        "--root",
        str(root),
        *extra_from_serve_args(args),
    ]
    access = str(getattr(args, "access", "") or "").strip()
    if access:
        argv += ["--access", access]
    proxy = str(getattr(args, "bridge_proxy", "") or "").strip()
    if proxy:
        argv += ["--bridge-proxy", proxy]
    if getattr(args, "sandbox", False):
        argv += ["--sandbox"]
    for user in getattr(args, "allow_user", []) or []:
        if str(user).strip():
            argv += ["--allow-user", str(user)]
    for flag, attr in (("--token", "token"), ("--appid", "appid"), ("--secret", "secret")):
        value = str(getattr(args, attr, "") or "").strip()
        if value:
            argv += [flag, value]
    if getattr(args, "notify_disconnect", False):
        argv += ["--notify-disconnect"]
    return argv


def spawn_bridge(args: argparse.Namespace) -> list[subprocess.Popen]:
    """只拉 hub 自己的孩子。llama / ssh 不在这里。"""
    argv = bridge_child_argv(args)
    if not argv:
        return []
    print(f"通道：{' '.join(argv[3:])}", file=sys.stderr)
    return [subprocess.Popen(argv)]


def stop_children(children: list[subprocess.Popen]) -> None:
    """退出时只杀自己拉起的进程。"""
    for proc in children:
        if proc.poll() is not None:
            continue
        proc.terminate()
        try:
            proc.wait(timeout=CHILD_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=CHILD_WAIT)


def hub_command(args: argparse.Namespace) -> int:
    channel = str(getattr(args, "channel", "") or "").strip()
    if channel and channel not in CHANNELS:
        print(
            "未知通道：" + channel + "。可选：" + "、".join(CHANNELS),
            file=sys.stderr,
        )
        return 2

    # 与 `serve` 同一条口径：会话由内核定，解析结果显式传给网页壳与子进程。
    target = resolve_target(
        workspace=args.root, session=args.session, client="web", session_fallback="cli"
    )
    children = spawn_bridge(args)

    def extra_health() -> dict:
        if not children:
            return {}
        proc = children[0]
        alive = proc.poll() is None
        return {
            "bridge": {
                "ok": alive,
                "name": "bridge",
                "target": channel,
                "detail": "在跑" if alive else "已退出",
            }
        }

    try:
        serve(
            target.root,
            host=args.host,
            port=args.port,
            session=target.session,
            extra_args=extra_from_serve_args(args),
            extra_health=extra_health,
        )
        return 0
    finally:
        stop_children(children)
