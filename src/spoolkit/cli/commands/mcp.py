"""`spool mcp`：把 agent 挂成 MCP 服务，交给别的 agent 调用。

用法（在 Codex / Claude Code / Cursor 的 MCP 配置里）：

    {"command": "spool", "args": ["mcp", "--scope", "**"]}

`--root` **可选**：不给就是"服务整台机器"——每次 `delegate_task` 可以带
`workspace` 点名一个已登记的工作区（`list_workspaces` 先看有什么），不给就用
`mcp` 的指向；给了 `--root` 就把这条 MCP 会话限定在那一个工作区里（想收紧时用）。

子进程仍旧按 `--policy` / `--scope` 跑：外部 agent 的确认等于用户确认，
边界靠作用域收窄，不靠协议层拦。
"""

import argparse
from pathlib import Path

from spoolkit.cli.commands.serve import extra_from_serve_args
from spoolkit.mcp.server import serve_stdio


def mcp_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).resolve() if str(args.root or "").strip() else None
    extra = extra_from_serve_args(args)

    # 不在这里建一个固定 Runner：工作区是**按调用**定的，所以 Runner 由服务端
    # 按 (工作区, 会话) 各建一个（见 `McpServer._runner_for`）。
    serve_stdio(
        project_root,
        None,
        mode="task",
        extra_args=tuple(extra),
    )
    return 0
