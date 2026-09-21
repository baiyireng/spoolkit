"""`spool mcp`：把 agent 挂成 MCP 服务，交给别的 agent 调用。

用法（在 Codex / Claude Code / Cursor 的 MCP 配置里）：

    {"command": "spool", "args": ["mcp", "--root", "D:\\proj", "--scope", "**"]}

子进程仍旧按 `--policy` / `--scope` 跑：外部 agent 的确认等于用户确认，
边界靠作用域收窄，不靠协议层拦。
"""

import argparse
from pathlib import Path

from spoolkit.cli.commands.serve import extra_from_serve_args
from spoolkit.mcp.server import WorkspaceRunner, serve_stdio


def mcp_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).resolve()
    extra = extra_from_serve_args(args)

    runner = WorkspaceRunner(
        project_root,
        session=args.session,
        extra_args=extra,
        mode="task",
    )
    serve_stdio(project_root, runner)
    return 0
