"""我们当客户端：把外部 MCP 服务挂成工具。

测试用的是**真的** stdio 子进程（一个几十行的假 MCP 服务），不是打桩函数：
这条链的价值全在协议与进程边界上，打桩就把要验的东西绕过去了。
"""

import sys
from pathlib import Path

import pytest

from spoolkit.mcp.client import McpClient, McpError, register_mcp_tools
from spoolkit.settings import McpServer, load_mcp_servers
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall

STUB = '''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if message.get("id") is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "stub", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "echo", "description": "回显输入",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}},
                             "required": ["text"]}},
            {"name": "boom", "description": "总是报错",
             "inputSchema": {"type": "object", "properties": {}}},
        ]}
    elif method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") == "boom":
            result = {"content": [{"type": "text", "text": "它坏了"}], "isError": True}
        else:
            text = (params.get("arguments") or {}).get("text", "")
            result = {"content": [{"type": "text", "text": "echo: " + str(text)}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}),
          flush=True)
'''

SILENT = '''
import sys, time
for line in sys.stdin:
    time.sleep(60)
'''


def _server(tmp_path: Path, body: str = STUB, name: str = "stub") -> McpServer:
    path = tmp_path / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return McpServer(name=name, command=sys.executable, args=(str(path),))


def test_列出对方的工具(tmp_path: Path) -> None:
    with McpClient(_server(tmp_path)) as client:
        tools = client.list_tools()
    assert [tool.name for tool in tools] == ["echo", "boom"]
    assert tools[0].schema["required"] == ["text"]


def test_调用对方的工具(tmp_path: Path) -> None:
    with McpClient(_server(tmp_path)) as client:
        assert client.call_tool("echo", {"text": "你好"}) == "echo: 你好"


def test_对方报错时不当成成功(tmp_path: Path) -> None:
    with McpClient(_server(tmp_path)) as client:
        text = client.call_tool("boom", {})
    assert "外部工具报错" in text


def test_挂成工具时名字带来源(tmp_path: Path) -> None:
    """工具重名是迟早的事，名字里带出处才能一眼看出是谁提供的。"""
    registry = ToolRegistry()
    with McpClient(_server(tmp_path)) as client:
        registered = register_mcp_tools(registry, [client])
        assert registered == ["mcp__stub__echo", "mcp__stub__boom"]
        result = registry.invoke(ToolCall("mcp__stub__echo", {"text": "x"}))
        assert result.ok is True
        assert "echo: x" in result.content
    index = registry.describe()
    assert "外挂" in index
    assert "mcp__stub__echo" in index


def test_连接关掉之后调用给一句明白话(tmp_path: Path) -> None:
    """外部服务没了不该表现成工具异常——那会让人以为是我们这边坏了。"""
    registry = ToolRegistry()
    client = McpClient(_server(tmp_path))
    client.start()
    register_mcp_tools(registry, [client])
    client.close()
    result = registry.invoke(ToolCall("mcp__stub__echo", {"text": "x"}))
    assert result.ok is False
    assert "连接没起来" in result.content


def test_对方回的错误内容仍算调用成功(tmp_path: Path) -> None:
    registry = ToolRegistry()
    with McpClient(_server(tmp_path)) as client:
        register_mcp_tools(registry, [client])
        result = registry.invoke(ToolCall("mcp__stub__boom", {}))
    assert result.ok is True
    assert "外部工具报错" in result.content


def test_起不来时报清楚(tmp_path: Path) -> None:
    client = McpClient(McpServer(name="bad", command="definitely-not-a-command-xyz"))
    with pytest.raises(McpError) as exc:
        client.start()
    assert "起不来" in str(exc.value)


def test_超时要报出来而不是一直等(tmp_path: Path) -> None:
    client = McpClient(_server(tmp_path, SILENT, "silent"), timeout=0.6)
    with pytest.raises(McpError) as exc:
        client.start()
    assert "超时" in str(exc.value)
    client.close()


def test_工具清单会缓存(tmp_path: Path) -> None:
    """自主编排每一步都重新装配注册表；不缓存就是每步都来回一轮。"""
    with McpClient(_server(tmp_path)) as client:
        first = client.list_tools()
        second = client.list_tools()
    assert [tool.name for tool in first] == [tool.name for tool in second]


def test_读用户配置里的_mcp_段(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                'provider = "llamacpp"',
                "",
                "[[mcp]]",
                'name = "fs"',
                'command = "npx"',
                'args = ["-y", "server-filesystem"]',
                "",
                "[[mcp]]",
                'name = ""',
                'command = "x"',
            ]
        ),
        encoding="utf-8",
    )
    servers = load_mcp_servers(path)
    assert [(item.name, item.command, item.args) for item in servers] == [
        ("fs", "npx", ("-y", "server-filesystem"))
    ]


def test_没有_mcp_段时是空(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('provider = "llamacpp"\n', encoding="utf-8")
    assert load_mcp_servers(path) == []
