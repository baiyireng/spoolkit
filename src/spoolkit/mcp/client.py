"""把外部 MCP 服务当工具挂进来。

方向反过来：前面那个 `mcp` 命令是**让别人调我们**，这里是**我们调别人**——
文件系统、浏览器、数据库、公司内部系统，市面上已经有现成的 MCP 服务，
没必要每个都自己写一遍工具。

三条纪律：

1. **连不上就当没有**，但要**说出来**。悄悄降级的结果是模型找不到工具、
   开始自己造轮子，而人不知道是配置写错了。
2. **名字带来源**：`mcp__<服务名>__<工具名>`（照 Claude Code 那套惯例）。
   工具重名是迟早的事，名字里带出处才能一眼看出是谁提供的。
3. **外挂工具不进子智能体**。角色裁剪按 `role.tools` 白名单走，MCP 工具不在
   白名单里，所以审查者拿不到它们——这是有意的：外部工具的副作用范围
   我们无从判断，让它出现在"只读角色"里迟早出事。
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from spoolkit.settings import McpServer
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult, ToolSpec

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 30.0


class McpError(RuntimeError):
    """外部 MCP 服务不可用或拒绝了这个调用。"""


@dataclass(frozen=True)
class RemoteTool:
    """对方报上来的一个工具。"""

    name: str
    description: str
    schema: dict[str, Any]


class McpClient:
    """一个外部 MCP 服务的 stdio 连接。"""

    def __init__(self, config: McpServer, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.config = config
        self.timeout = timeout
        self._process: subprocess.Popen | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._next_id = 0
        self._reader: threading.Thread | None = None
        # 工具清单取一次就够。**必须缓存**：自主编排每一步都会重新装配注册表，
        # 不缓存就是每一步都跟外部服务来回一轮——几十步下来白等几十秒。
        self._tools: list[RemoteTool] | None = None

    # --- 生命周期 ---

    def start(self) -> None:
        env = dict(os.environ)
        env.update(self.config.env)
        try:
            self._process = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                bufsize=1,
            )
        except OSError as exc:
            raise McpError(f"起不来：{exc}") from exc
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "spool", "version": _version()},
            },
        )
        # 握手之后要发这条通知，对方才认为会话建立；不回话，所以不等结果。
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - 退出期的兜底
            process.kill()

    def __enter__(self) -> "McpClient":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- 协议 ---

    def list_tools(self, refresh: bool = False) -> list[RemoteTool]:
        if self._tools is not None and not refresh:
            return list(self._tools)
        result = self._request("tools/list", {})
        tools: list[RemoteTool] = []
        for item in result.get("tools") or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            tools.append(
                RemoteTool(
                    name=name,
                    description=str(item.get("description") or ""),
                    schema=item.get("inputSchema") or {"type": "object", "properties": {}},
                )
            )
        self._tools = tools
        return list(tools)

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self._request(
            "tools/call", {"name": name, "arguments": dict(arguments)}
        )
        return _render_content(result)

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        deadline = time.time() + self.timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise McpError(f"{method} 超时（{self.timeout:.0f} 秒）")
            try:
                message = self._inbox.get(timeout=remaining)
            except queue.Empty:
                raise McpError(f"{method} 超时（{self.timeout:.0f} 秒）") from None
            if message.get("id") != request_id:
                continue  # 通知或别的消息：这条连接上我们只按 id 认自己的回话
            if "error" in message:
                error = message["error"]
                raise McpError(str(error.get("message") or error))
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def _write(self, payload: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError("连接没起来")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(f"写不进去：{exc}") from exc

    def _pump(self) -> None:
        process = self._process
        if process is None or process.stdout is None:  # pragma: no cover
            return
        for line in process.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                self._inbox.put(json.loads(text))
            except json.JSONDecodeError:
                # 对方可能往 stdout 打日志。协议之外的行忽略掉，
                # 但不能因此判定"它没回话"——那会把一次可用连接判死。
                continue


def _render_content(result: dict) -> str:
    """把 MCP 的 content 数组渲染成一段文本给模型看。"""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        else:
            parts.append(f"（{item.get('type')} 内容，暂不支持渲染）")
    text = "\n".join(part for part in parts if part).strip()
    if result.get("isError"):
        return f"外部工具报错：{text or '（没有说明）'}"
    return text or "（对方没有返回内容）"


def _version() -> str:
    from spoolkit import __version__

    return __version__


def register_mcp_tools(
    registry: ToolRegistry,
    clients: list[McpClient],
    announce: Any = None,
) -> list[str]:
    """把外部工具登记进注册表，返回登记了哪些名字。"""
    registered: list[str] = []
    for client in clients:
        try:
            tools = client.list_tools()
        except McpError as exc:
            _say(announce, f"外部工具 {client.config.name} 不可用：{exc}")
            continue
        for tool in tools:
            spec = ToolSpec(
                name=f"mcp__{client.config.name}__{tool.name}",
                description=(
                    f"[外部工具 {client.config.name}] "
                    + (tool.description or tool.name)
                ),
                parameters=tool.schema,
                handler=_handler(client, tool.name, announce),
                brief=(tool.description or tool.name)[:18],
                group="外挂",
            )
            if registry.get(spec.name) is not None:  # pragma: no cover - 名字带来源
                continue
            registry.register(spec)
            registered.append(spec.name)
        if tools:
            _say(
                announce,
                f"外部工具 {client.config.name}：挂了 {len(tools)} 个"
                f"（{', '.join(tool.name for tool in tools[:6])}"
                + ("…" if len(tools) > 6 else "")
                + "）",
            )
    return registered


def _handler(client: McpClient, tool_name: str, announce: Any = None):
    def call(arguments: dict) -> ToolResult:
        try:
            return ToolResult(ok=True, content=client.call_tool(tool_name, arguments))
        except McpError as exc:
            return ToolResult(
                ok=False,
                content=(
                    f"外部工具 {client.config.name}.{tool_name} 调用失败：{exc}"
                    "。（它是外挂的 MCP 服务，不在本项目的工作区授权范围内——"
                    "必要时请人工检查它的配置。）"
                ),
            )

    return call


def _say(announce: Any, text: str) -> None:
    if announce is not None:
        announce(text)
