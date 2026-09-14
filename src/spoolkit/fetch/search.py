"""网页搜索后端。

搜索是「先搜再读」的入口，不是把搜索引擎结果页抓回来——实测 Bing 结果页
96 KB、去标签后没有正文。所以这里只接受结构化 API，返回标题+网址+一句摘要。

没配 key 时后端是未配置状态：工具必须把这句话说清楚，而不是去抓结果页、
也不是抛未捕获异常把研究会话打崩。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""


class SearchError(Exception):
    """后端拒绝或失败。消息会原样交给调用方。"""


@dataclass(frozen=True)
class UnconfiguredSearch:
    """默认后端：没有 API、没有 key。configured 为假。"""

    wanted: str = ""
    configured: bool = False

    def search(self, query: str, limit: int) -> list[SearchHit]:
        raise SearchError("没配搜索，只能直接给 URL。")
