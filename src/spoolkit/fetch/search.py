"""网页搜索后端。

搜索是「先搜再读」的入口，不是把搜索引擎结果页抓回来——实测 Bing 结果页
96 KB、去标签后没有正文。所以这里只接受结构化 API，返回标题+网址+一句摘要。

没配 key 时后端是未配置状态：工具必须把这句话说清楚，而不是去抓结果页、
也不是抛未捕获异常把研究会话打崩。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from spoolkit.fetch.client import USER_AGENT

TAVILY_URL = "https://api.tavily.com/search"


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""


class SearchError(Exception):
    """后端拒绝或失败。消息会原样交给调用方。"""


def hits_from_tavily(payload: object, limit: int) -> list[SearchHit]:
    """把 Tavily 的 JSON 收成标题+网址+摘要。条数由调用方给，这里不写死。

    `answer` 若有，接到第一条摘要前面——天气这类事实问句经常这一句就够，
    不必再抓整页。没有网址的条目丢掉（工具也不会用）。
    """
    cap = max(1, int(limit))
    if not isinstance(payload, dict):
        return []
    hits: list[SearchHit] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("content") or item.get("snippet") or "").strip()
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
        if len(hits) >= cap:
            break
    answer = str(payload.get("answer") or "").strip()
    if answer and hits:
        first = hits[0]
        snippet = answer if not first.snippet else f"{answer} {first.snippet}"
        hits[0] = SearchHit(title=first.title, url=first.url, snippet=snippet)
    return hits


@dataclass(frozen=True)
class UnconfiguredSearch:
    """默认后端：没有 API、没有 key。configured 为假。"""

    wanted: str = ""
    configured: bool = False

    def search(self, query: str, limit: int) -> list[SearchHit]:
        raise SearchError("没配搜索，只能直接给 URL。")


@dataclass(frozen=True)
class TavilySearch:
    """Tavily 网页搜索。要 key；返回标题+网址+摘要，不抓结果页。"""

    api_key: str
    proxy: str | None = None
    timeout: float = 20.0
    transport: httpx.BaseTransport | None = None
    configured: bool = True

    def search(self, query: str, limit: int) -> list[SearchHit]:
        cap = max(1, int(limit))
        headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "query": query,
            "max_results": cap,
            "include_answer": True,
        }
        try:
            with httpx.Client(
                timeout=self.timeout,
                proxy=self.proxy,
                transport=self.transport,
                headers=headers,
            ) as client:
                response = client.post(TAVILY_URL, json=payload)
        except httpx.HTTPError as exc:
            raise SearchError(f"搜索连不上：{exc}") from exc
        if response.status_code in {401, 403}:
            raise SearchError("搜索 key 无效。")
        if response.status_code == 429:
            raise SearchError("搜索额度用完了，稍后再试。")
        if response.status_code >= 400:
            raise SearchError(f"搜索失败：HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SearchError("搜索返回的不是 JSON。") from exc
        return hits_from_tavily(body, cap)
