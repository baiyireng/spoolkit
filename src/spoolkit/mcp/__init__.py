"""把 agent 当成一个 MCP 工具暴露出去。

存在的理由：市面上的 agent（Codex / Claude Code / Cursor 之类）已经会说话，
缺的是一个它们能调的"手"。它们扮演用户下发编排任务，这个 agent 在工作区里
实施，再把事件与结论交回去。
"""

from spoolkit.mcp.server import serve_stdio

__all__ = ["serve_stdio"]
