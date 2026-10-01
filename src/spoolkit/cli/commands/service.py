"""`spool service`：本机外接配方的快捷拉起 / 停掉 / 列出。

不写死 gguf 路径或 ssh 主机。配方在用户配置的 `[[service]]` 里。
hub 不会替你启动这些进程——那是这条命令的事。

**"要不要拉起"看的是本命令记下的 pid**，不是探针：探针只报状态。端口被别人
（Clash 之类）占着时，探针通不等于"我们有隧道"，所以 `start` 照样按 pid 记录
决定拉不拉，并把子进程启动后立刻退出的退出码与输出带出来。
细节在 `spoolkit.services`。

常见两段：`llama` 是本机语言模型；`tunnel` 是通道出网的 SOCKS（`ssh -D`），
给 `--bridge-proxy` 用，不是模型的 `--proxy`。
"""

from __future__ import annotations

import argparse
import sys

from spoolkit import settings
from spoolkit.services import render_status, start_service, stop_service


def service_command(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None) or "list"
    name = str(getattr(args, "name", "") or "").strip()
    _, problem = settings.read_config()
    if problem:
        print(problem, file=sys.stderr)
        return 2

    if action == "list":
        print(render_status())
        return 0

    items = settings.load_services()
    if action == "start":
        if name:
            names = [name]
        else:
            names = [item.name for item in items if item.command]
            if not names:
                print("没有带启动命令的配方。", file=sys.stderr)
                return 2
        fn = start_service
    else:
        names = [name] if name else [item.name for item in items]
        if not names:
            print("没有配置外接服务。先在用户配置里写 [[service]]。", file=sys.stderr)
            return 2
        fn = stop_service

    worst = 0
    for item in names:
        code, message = fn(item)
        print(message, file=sys.stderr if code else sys.stdout)
        worst = max(worst, code)
    return worst
