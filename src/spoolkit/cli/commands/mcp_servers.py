"""看配了哪些外挂 MCP 服务；`--check` 就连一遍、报它们提供了什么工具。

为什么需要这条命令：外挂工具出错的样子是"模型说找不到工具"或者"工具老失败"，
而人不知道是配置写错了、服务没装、还是它本来就没有那个工具。
把连接结果直接摆出来，这类问题就不用猜。
"""

import argparse

from spoolkit.cli.term import escape, print_err, print_out
from spoolkit.mcp.client import McpClient, McpError, register_mcp_tools
from spoolkit.settings import config_path, load_mcp_servers, read_config
from spoolkit.tools.registry import ToolRegistry


def mcp_servers_command(args: argparse.Namespace) -> int:
    _, problem = read_config()
    if problem:
        # 解析失败时上面的"没有配置"是假象——必须先把真正的原因说出来。
        print_err(problem, markup=False, style="bad")
        return 2
    servers = load_mcp_servers()
    print_out(f"[muted]配置文件：[/]{escape(str(config_path()))}")
    if not servers:
        print_out("[muted]未配置外挂 MCP 服务。添加方式（写入上面的文件）：[/]")
        print_out("")
        for line in (
            "  [[mcp]]",
            '  name = "filesystem"',
            '  command = "npx"',
            '  args = ["-y", "@modelcontextprotocol/server-filesystem", "D:\\\\proj"]',
        ):
            print_out(f"[accent]{escape(line)}[/]")
        print_out("")
        print_out(
            "[muted]"
            + escape(
                "注意：Windows 路径中的反斜杠在 TOML 里要写两份（D:\\\\tools\\\\x.exe），"
            )
            + "[/]"
        )
        print_out(
            "[muted]"
            + escape(
                "      或改用单引号字符串（'D:\\tools\\x.exe'）；写错会导致整份配置无法读取。"
            )
            + "[/]"
        )
        return 0

    print_out(f"[muted]配了 [/][accent]{len(servers)}[/][muted] 个：[/]")
    for item in servers:
        rest = f"{item.command} {' '.join(item.args)}".rstrip()
        print_out(
            f"  - [accent]{escape(item.name)}[/]  {escape(rest)}"
        )

    if not getattr(args, "check", False):
        print_out(
            "[muted]\n加 --check 会实际连接一次，列出它们提供的工具。[/]"
        )
        return 0

    print_out("\n[muted]连接一遍：[/]")
    failed = 0
    for config in servers:
        client = McpClient(config)
        try:
            client.start()
            tools = client.list_tools()
        except McpError as exc:
            print_out(
                f"  [bad]✗ {escape(config.name)}：{escape(str(exc))}[/]"
            )
            failed += 1
            client.close()
            continue
        names = ", ".join(tool.name for tool in tools) or "（没有工具）"
        print_out(
            f"  [ok]✓ {escape(config.name)}：{len(tools)} 个工具：[/]"
            f"{escape(names)}"
        )
        registry = ToolRegistry()
        register_mcp_tools(registry, [client])
        print_out(
            f"      [muted]挂成：{escape(', '.join(registry.names()))}[/]"
        )
        client.close()
    return 1 if failed else 0
