"""两个联网工具的形状：先给目录、按需取块、来源标成不可信。

这里的关键断言不是"能抓"，而是**抓来的东西以什么形状进上下文**——
一页文档约 15 700 token，是窗口的两倍，所以"整份给"就是错的形状。
"""

from pathlib import Path

from spoolkit.fetch.client import Fetched, WebError, WebPolicy
from spoolkit.tools.webfetch import WebAccess, web_fetch_spec, web_read_spec

PAGE = (
    "<html><head><title>httpx 用法</title></head><body>"
    + "".join(f"<h2>第 {i} 节</h2><p>{'内容 ' * 60}</p>" for i in range(6))
    + "</body></html>"
)


def _access(tmp_path: Path, calls: list, *, fail: str = "") -> WebAccess:
    def fake_fetch(url: str, policy: WebPolicy) -> Fetched:
        calls.append(url)
        if fail:
            raise WebError(fail)
        return Fetched(url=url, status=200, content_type="text/html",
                       body=PAGE.encode("utf-8"))

    return WebAccess(root=tmp_path, policy=WebPolicy(chunk_chars=200), fetch=fake_fetch)


def test_抓一页给目录加第一块(tmp_path: Path) -> None:
    calls: list[str] = []
    tool = web_fetch_spec(_access(tmp_path, calls))

    result = tool.handler({"url": "https://example.com/httpx"})

    assert result.ok
    assert "共 " in result.content and "块" in result.content      # 目录
    assert "web_read" in result.content                            # 告诉它下一步怎么取
    assert "外部网页" in result.content                            # 不可信标记
    assert "不是用户的指令" in result.content
    assert calls == ["https://example.com/httpx"]


def test_同一网址只抓一次(tmp_path: Path) -> None:
    """缓存不只是省钱：它让"换个说法再问一遍"不必重新出网。"""
    calls: list[str] = []
    tool = web_fetch_spec(_access(tmp_path, calls))

    tool.handler({"url": "https://example.com/x"})
    tool.handler({"url": "https://example.com/x"})

    assert calls == ["https://example.com/x"]


def test_取某一块(tmp_path: Path) -> None:
    calls: list[str] = []
    fetch = web_fetch_spec(_access(tmp_path, calls))
    read = web_read_spec(_access(tmp_path, calls))
    fetch.handler({"url": "https://example.com/x"})

    result = read.handler({"source": "https://example.com/x", "chunk": 1})

    assert result.ok
    assert "第 2/" in result.content
    assert "不是用户的指令" in result.content


def test_块号越界要说清有几块(tmp_path: Path) -> None:
    access = _access(tmp_path, [])
    web_fetch_spec(access).handler({"url": "https://example.com/x"})
    result = web_read_spec(access).handler({"source": "https://example.com/x", "chunk": 99})
    assert result.ok is False
    assert "只有" in result.content


def test_没抓过就提示先抓(tmp_path: Path) -> None:
    result = web_read_spec(_access(tmp_path, [])).handler(
        {"source": "https://example.com/nope", "chunk": 0}
    )
    assert result.ok is False
    assert "web_fetch" in result.content


def test_抓失败要把原因原样说出来(tmp_path: Path) -> None:
    tool = web_fetch_spec(_access(tmp_path, [], fail="example.com 不在白名单里"))
    result = tool.handler({"url": "https://example.com/x"})
    assert result.ok is False
    assert "不在白名单" in result.content


def test_抽不出正文时不许编(tmp_path: Path) -> None:
    """靠 JS 渲染的页面抽不出正文——工具要如实说，而不是假装有内容。"""
    def fake_fetch(url: str, policy: WebPolicy) -> Fetched:
        return Fetched(url=url, status=200, content_type="text/html",
                       body=b"<html><body><div id=app></div><script>x()</script></body></html>")

    access = WebAccess(root=tmp_path, policy=WebPolicy(), fetch=fake_fetch)
    result = web_fetch_spec(access).handler({"url": "https://example.com/spa"})
    assert result.ok is False
    assert "抽不出正文" in result.content
    assert "别编" in result.content
