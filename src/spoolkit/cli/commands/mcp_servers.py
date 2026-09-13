"""看配了哪些外挂 MCP 服务；`--check` 就连一遍、报它们提供了什么工具。

为什么需要这条命令：外挂工具出错的样子是"模型说找不到工具"或者"工具老失败"，
而人不知道是配置写错了、服务没装、还是它本来就没有那个工具。
把连接结果直接摆出来，这类问题就不用猜。
"""

import argparse
import sys

from spoolkit.mcp.client import McpClient, McpError, register_mcp_tools
from spoolkit.settings import config_path, load_mcp_servers, read_config
from spoolkit.tools.registry import ToolRegistry


def mcp_servers_command(args: argparse.Namespace) -> int:
    _, problem = read_config()
    if problem:
        # 解析失败时上面的"没有配置"是假象——必须先把真正的原因说出来。
        print(problem, file=sys.stderr)
        return 2
    servers = load_mcp_servers()
    print(f"配置文件：{config_path()}")
    if not servers:
        print("没有配置外挂 MCP 服务。加一个的办法（写进上面的文件）：")
        print("")
        print("  [[mcp]]")
        print('  name = "filesystem"')
        print('  command = "npx"')
        print('  args = ["-y", "@modelcontextprotocol/server-filesystem", "D:\\\\proj"]')
        print("")
        print("注意：Windows 路径里的反斜杠在 TOML 里要写双份（D:\\\\tools\\\\x.exe），")
        print("      或者用单引号字符串（'D:\\tools\\x.exe'）；写错整份配置都会读不出来。")
        return 0

    print(f"配了 {len(servers)} 个：")
    for item in servers:
        line = f"  - {item.name}：{item.command} {' '.join(item.args)}".rstrip()
        print(line)

    if not getattr(args, "check", False):
        print("\n（加 --check 会真的连一遍，看它们提供哪些工具）")
        return 0

    print("\n连一遍：")
    failed = 0
    for config in servers:
        client = McpClient(config)
        try:
            client.start()
            tools = client.list_tools()
        except McpError as exc:
            print(f"  ✗ {config.name}：{exc}")
            failed += 1
            client.close()
            continue
        names = ", ".join(tool.name for tool in tools) or "（没有工具）"
        print(f"  ✓ {config.name}：{len(tools)} 个工具 —— {names}")
        # 顺带验一下挂进注册表之后的名字，免得真跑起来才发现重名/挂不上。
        registry = ToolRegistry()
        register_mcp_tools(registry, [client])
        print(f"      挂成：{', '.join(registry.names())}")
        client.close()
    return 1 if failed else 0
