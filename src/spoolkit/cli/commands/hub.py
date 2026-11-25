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
from spoolkit.cli.term import print_err
from spoolkit.kernel.target import Target, resolve_target
from spoolkit.web.server import serve

CHANNELS = ("fake", "telegram", "wecom", "qqbot")
CHILD_WAIT = 8.0


def bridge_child_argv(args: argparse.Namespace, root: Path | None = None) -> list[str]:
    """拼出要拉起的 `spool bridge` 命令行。没给通道就空列表。

    `root` 是**内核解析出来的**工作区（`hub_command` 给）。不给就按 `args.root`
    看：空串 = 没有工作区，那就**不给子进程 `--root`**（机器级桥：每条聊天按
    自己的指针落到哪个工作区）。

    为什么不能退回 `Path(getattr(args, "root", "."))`：`--root` 的缺省是空串，
    而 `Path("")` 恰好就是 cwd——于是"服务整台机器"的 hub 把 **cwd 派生的工作区
    钉给了桥子进程**，子进程再去读 `cwd/.env` 里的模型密钥（2026-09-27 评审实测：
    hub 起的通道拿到了 `D:\\workSpace\\agents_dev\\.env` 里的凭据，而裸
    `spool bridge` 说缺凭据）。机器级入口不认 cwd，见设计 §3.2。
    """
    channel = str(getattr(args, "channel", "") or "").strip()
    if not channel:
        return []
    if root is None:
        raw = str(getattr(args, "root", "") or "").strip()
        root = Path(raw).resolve() if raw else None
    argv = [
        sys.executable,
        "-m",
        "spoolkit.cli.app",
        "bridge",
        "--channel",
        channel,
    ]
    if root is not None:
        argv += ["--root", str(root)]
    argv += list(extra_from_serve_args(args))
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


def spawn_bridge(
    args: argparse.Namespace, root: Path | None = None
) -> list[subprocess.Popen]:
    """只拉 hub 自己的孩子。llama / ssh 不在这里。"""
    argv = bridge_child_argv(args, root)
    if not argv:
        return []
    print_err(f"通道：{' '.join(argv[3:])}", markup=False)
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
        print_err(
            "未知通道：" + channel + "。可选：" + "、".join(CHANNELS),
            markup=False,
            style="bad",
        )
        return 2

    # 与 `serve` 同一条口径：**给了 `--root` = 钉住那一个工作区**（老行为逐字保留），
    # 没给 = 机器级。机器级这一路这里**不解析目标**——内核顺位（网页壳指针 →
    # 登记表）由网页壳和桥各自去走，而且都不看这个进程的 cwd：hub 站在哪个目录
    # 跟"这台机器上有哪些工作区"毫无关系（设计 §3.2）。早先这里把 cwd 派生出来的
    # 工作区**钉给了桥子进程**，于是通道读的是 `cwd/.env` 里的凭据（评审实测过）。
    target: Target | None = None
    if str(getattr(args, "root", "") or "").strip():
        target = resolve_target(
            workspace=args.root,
            session=str(getattr(args, "session", "") or ""),
            client="web",
            session_fallback="cli",
            use_cwd=False,
        )
    pinned_root = target.root if target is not None else None
    # 桥子进程：确实解析出工作区时才给它 `--root`；没解析出来就**一个都不给**
    # （裸 `spool bridge` = 服务整台机器，每条聊天按自己的指针落）。
    children = spawn_bridge(args, pinned_root)

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
        if target is not None:
            serve(
                target.root,
                host=args.host,
                port=args.port,
                session=target.session,
                extra_args=extra_from_serve_args(args),
                extra_health=extra_health,
                pinned=True,
                target=target,
            )
        else:
            serve(
                None,
                host=args.host,
                port=args.port,
                session=str(getattr(args, "session", "") or ""),
                extra_args=extra_from_serve_args(args),
                extra_health=extra_health,
                pinned=False,
            )
        return 0
    finally:
        stop_children(children)
