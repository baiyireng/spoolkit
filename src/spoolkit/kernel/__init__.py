"""工作区与会话内核。

四个入口（CLI / 聊天通道 / 网页壳 / MCP）都从这里拿"现在该在哪儿干活"，不再各自
解析 `--root`。实现细节在各子模块里；这里只做出口，免得调用方到处 import 路径。
"""

from spoolkit.kernel.workspaces import WorkspaceError

__all__ = ["WorkspaceError"]
