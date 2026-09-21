"""结构化搜索：只接 API 返回的标题+网址+摘要，不去抓结果页。"""

import httpx
import pytest

from spoolkit.fetch.search import SearchError, SearchHit, hits_from_tavily


def test_tavily的json解成标题网址摘要() -> None:
    hits = hits_from_tavily(
        {
            "answer": "厦门今天多云，约 26℃。",
            "results": [
                {
                    "title": "厦门天气",
                    "url": "https://weather.example/xiamen",
                    "content": "未来两天阵雨",
                },
                {
                    "title": "广告",
                    "url": "",
                    "content": "没有网址的丢掉",
                },
                {
                    "title": "备选",
                    "url": "https://weather.example/fujian",
                    "content": "福建气象",
                },
            ],
        },
        limit=2,
    )
    assert [hit.url for hit in hits] == [
        "https://weather.example/xiamen",
        "https://weather.example/fujian",
    ]
    assert hits[0].title == "厦门天气"
    assert "26℃" in hits[0].snippet
    assert "阵雨" in hits[0].snippet


def test_tavily空结果就是没有命中() -> None:
    assert hits_from_tavily({"results": []}, limit=3) == []
    assert hits_from_tavily("不是对象", limit=3) == []


def test_tavily按limit截断() -> None:
    payload = {
        "results": [
            {"title": f"t{i}", "url": f"https://example.com/{i}", "content": "x"}
            for i in range(5)
        ]
    }
    hits = hits_from_tavily(payload, limit=2)
    assert len(hits) == 2
    assert hits[1].url == "https://example.com/1"


def _tavily(handler, **kwargs):
    from spoolkit.fetch.search import TavilySearch

    return TavilySearch(
        api_key="tvly-test",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_tavily搜到结构化结果() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        body = request.read()
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Timeouts",
                        "url": "https://www.python-httpx.org/advanced/timeouts/",
                        "content": "connect/read/write/pool",
                    }
                ]
            },
        )

    hits = _tavily(handler).search("httpx timeout", 3)
    assert seen["url"].startswith("https://api.tavily.com/search")
    assert seen["auth"] == "Bearer tvly-test"
    assert b"httpx timeout" in seen["body"]
    assert hits == [
        SearchHit(
            title="Timeouts",
            url="https://www.python-httpx.org/advanced/timeouts/",
            snippet="connect/read/write/pool",
        )
    ]


def test_tavily的key无效要说清楚() -> None:
    backend = _tavily(lambda request: httpx.Response(401, json={"detail": "Unauthorized"}))
    with pytest.raises(SearchError) as caught:
        backend.search("天气", 3)
    assert "key" in str(caught.value).lower() or "密钥" in str(caught.value)


def test_tavily连不上要说清楚() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    backend = _tavily(handler)
    with pytest.raises(SearchError) as caught:
        backend.search("天气", 3)
    assert "连不上" in str(caught.value)
