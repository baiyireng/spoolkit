"""`spool mcp`：把 agent 挂成 MCP 服务，交给别的 agent 调用。

用法（在 Codex / Claude Code / Cursor 的 MCP 配置里）：

    {"command": "spool", "args": ["mcp", "--scope", "**"]}

`--root` **可选**：不给就是"服务整台机器"——每次 `delegate_task` 可以带
`workspace` 点名一个已登记的工作区（`list_workspaces` 先看有什么），不给就用
`mcp` 的指向；给了 `--root` 就把这条 MCP 会话**限定**在那一个工作区里（想收紧时用），
这时点名别的工作区会**报错**——`--root` 是主人给的范围限制，它得真的压得住。

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
    # `--limit` 只有这条入口转发：它管的是自主编排**拆几步**（`run --autonomous --limit`），
    # 别的入口没有同名参数，从 `serve` 的参数表里顺手带过去会串味。
    limit = getattr(args, "limit", None)
    if limit:
        extra += ["--limit", str(limit)]

    # 不在这里建一个固定 Runner：工作区是**按调用**定的，所以 Runner 由服务端
    # 按 (工作区, 会话) 各建一个（见 `McpServer._runner_for`）。
    serve_stdio(
        project_root,
        None,
        mode="task",
        extra_args=tuple(extra),
    )
    return 0
