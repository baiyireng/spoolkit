"""`spool status`：一眼看清这台机器上的 spoolkit 服务，以及怎么起 / 怎么停。

这一层只做三件事：把参数变成 **秒数 / 端口号**、把配置读坏的情况挡在探活之前
（读不出来时那张表全是假象）、把报告打到 stdout 并返回有意义的退出码。
探测与渲染都在 `spoolkit.status` 里，那条路单独测得了。
"""

from __future__ import annotations

import argparse
import sys

from spoolkit import settings
from spoolkit import status as status_report


def _seconds(raw: object, flag: str, default: float) -> float | None:
    """`--timeout` / `--mcp-timeout` 的取值。给了不合形状的就**说出来**，别静默用默认值。"""
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(f"{flag} 要是秒数（例如 0.5），现在拿到的是 {raw!r}。", file=sys.stderr)
        return None
    if value <= 0:
        print(f"{flag} 要大于 0 秒，现在是 {value:g}。", file=sys.stderr)
        return None
    return value


def _ports(raw: list[int]) -> list[int] | None:
    """`--web-port` 给的端口。越界就报错——探一个不存在的端口只会得到误导人的"没起"。"""
    ports: list[int] = []
    for item in raw or []:
        try:
            port = int(item)
        except (TypeError, ValueError):
            print(f"--web-port 要是个端口号，现在拿到的是 {item!r}。", file=sys.stderr)
            return None
        if not 1 <= port <= 65535:
            print(f"--web-port 要在 1..65535 之间，现在是 {port}。", file=sys.stderr)
            return None
        ports.append(port)
    return ports


def status_command(args: argparse.Namespace) -> int:
    timeout = _seconds(
        getattr(args, "timeout", None), "--timeout", status_report.DEFAULT_TIMEOUT
    )
    mcp_timeout = _seconds(
        getattr(args, "mcp_timeout", None),
        "--mcp-timeout",
        status_report.DEFAULT_MCP_TIMEOUT,
    )
    ports = _ports(list(getattr(args, "web_port", []) or []))
    if timeout is None or mcp_timeout is None or ports is None:
        return 2

    _, problem = settings.read_config()
    if problem:
        # 配置读不出来时上面那张表**全是假象**（provider / 配方 / 通道都读不到），
        # 所以先把真正的原因说出来。与 `spool service` / `spool mcp-servers` 同一条口径。
        print(problem, file=sys.stderr)
        return 2

    report = status_report.build_report(
        timeout=timeout,
        web_ports=ports,
        probe_mcp=bool(getattr(args, "probe", False)),
        mcp_timeout=mcp_timeout,
    )
    if getattr(args, "json", False):
        print(status_report.render_json(report))
    else:
        print(status_report.render_human(report))
    return status_report.status_exit_code(report)
