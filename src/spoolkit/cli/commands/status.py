"""`spool status`：本机 spoolkit 相关服务的现状，以及起停方式。

这一层只做三件事：把参数变成 **秒数 / 端口号**、把配置读坏的情况挡在探活之前
（读不出来时那张表全是假象）、把报告打到 stdout 并返回有意义的退出码。
探测与渲染都在 `spoolkit.status` 里，那条路单独测得了。
"""

from __future__ import annotations

import argparse

from spoolkit import settings
from spoolkit import status as status_report
from spoolkit.cli.term import print_err, print_out


def _seconds(raw: object, flag: str, default: float) -> float | None:
    """`--timeout` / `--mcp-timeout` 的取值。给了不合形状的就**说出来**，不得静默沿用默认值。"""
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print_err(
            f"{flag} must be seconds (e.g. 0.5), got {raw!r}.",
            markup=False,
        )
        return None
    if value <= 0:
        print_err(
            f"{flag} must be > 0 seconds, got {value:g}.",
            markup=False,
        )
        return None
    return value


def _ports(raw: list[int]) -> list[int] | None:
    """Validate `--web-port` values."""
    ports: list[int] = []
    for item in raw or []:
        try:
            port = int(item)
        except (TypeError, ValueError):
            print_err(
                f"--web-port must be a port number, got {item!r}.",
                markup=False,
            )
            return None
        if not 1 <= port <= 65535:
            print_err(
                f"--web-port must be in 1..65535, got {port}.",
                markup=False,
            )
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
        print_err(problem, markup=False)
        return 2

    report = status_report.build_report(
        timeout=timeout,
        web_ports=ports,
        probe_mcp=bool(getattr(args, "probe", False)),
        mcp_timeout=mcp_timeout,
    )
    if getattr(args, "json", False):
        # Machine-readable: never colorize.
        print(status_report.render_json(report))
    else:
        print_out(status_report.render_human(report))
    return status_report.status_exit_code(report)
