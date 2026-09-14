"""研究会话里的网页搜索工具。

只挂在研究会话：T2 量过，主循环多一个「很像查资料」的工具，27B 就会走它、
丢掉 research。没配搜索时工具仍然存在，但必须把原因说清楚。
"""

from __future__ import annotations

from spoolkit.fetch.search import SearchError, SearchHit, UnconfiguredSearch
from spoolkit.tools.types import ToolResult, ToolSpec

_UNCONFIGURED = "没配搜索，只能直接给 URL。"


def web_search_spec(backend=None, *, result_limit: int = 3) -> ToolSpec:
    source = backend if backend is not None else UnconfiguredSearch()
    limit = max(1, int(result_limit))

    def handler(args: dict) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, content="要给搜索词（query）。")
        if not getattr(source, "configured", False):
            return ToolResult(ok=False, content=_UNCONFIGURED)
        try:
            hits = source.search(query, limit)
        except SearchError as exc:
            return ToolResult(ok=False, content=str(exc) or _UNCONFIGURED)
        rows = [_render(hit) for hit in hits if getattr(hit, "url", "")]
        if not rows:
            return ToolResult(
                ok=False,
                content="搜了但没有网址。换个词，或直接把 URL 传给 research。",
            )
        return ToolResult(ok=True, content="搜索结果：\n" + "\n".join(rows))

    return ToolSpec(
        name="web_search",
        description=(
            "用搜索 API 查网页入口（标题+网址+一句摘要）。"
            "没有入口 URL、需要先搜再读时用。"
            f"最多 {limit} 条，是能力标定。"
            "没配搜索时会告诉你只能直接给 URL——"
            "不要去抓搜索引擎结果页。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
        brief="搜网页入口（要配 API）",
        group="查",
    )


def _render(hit: SearchHit) -> str:
    title = str(getattr(hit, "title", "") or "").strip() or "（无标题）"
    url = str(getattr(hit, "url", "") or "").strip()
    snippet = " ".join(str(getattr(hit, "snippet", "") or "").split())[:160]
    line = f"- {title} {url}"
    if snippet:
        line += f" — {snippet}"
    return line
