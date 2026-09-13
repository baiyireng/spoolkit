"""联网取用的地基：正文抽取、切块、缓存、以及"不许抓什么"。

这里不测真网络——真网络进测试就是给自己埋一个偶发失败。抓取器统一注入假货，
只有"内网拦截"那几条直接调判定函数。
"""

import http.server
import threading
from pathlib import Path

import pytest

from spoolkit.fetch import catalog, extract, load, read_chunk, save
from spoolkit.fetch.client import WebError, WebPolicy, check, fetch_text

PAGE = """<html><head><title>安装指南</title>
<style>body{color:red}</style><script>var x=1;</script></head>
<body>
<h1>安装</h1><p>先装依赖。</p>
<ul><li>uv</li><li>python</li></ul>
<h2>验证</h2><pre>uv run pytest -q</pre>
<footer>© 2026</footer>
</body></html>"""


def test_抽取丢掉脚本样式_留下标题与代码() -> None:
    title, text = extract(PAGE)
    assert title == "安装指南"
    assert "var x=1" not in text and "color:red" not in text
    assert "# 安装" in text and "## 验证" in text
    assert "uv run pytest -q" in text
    assert "- uv" in text          # 列表项


def test_按标题切块_目录带小标题(tmp_path: Path) -> None:
    entry = save(tmp_path, "https://example.com/guide", "安装指南", extract(PAGE)[1],
                 chunk_chars=60)
    assert entry.count >= 2
    headings = [chunk.heading for chunk in entry.chunks]
    assert "验证" in headings
    text = catalog(entry)
    assert "安装指南" in text and "web_read" in text
    assert entry.tokens > 0


def test_缓存按_URL_定位_不存索引表(tmp_path: Path) -> None:
    entry = save(tmp_path, "https://example.com/a", "A", "# A\n正文")
    again = load(tmp_path, "https://example.com/a")
    assert again is not None and again.key == entry.key
    # 用 key 也能定位（工具会把 key 交给模型）
    assert load(tmp_path, entry.key) is not None
    assert read_chunk(tmp_path, entry.key, 0) is not None
    assert read_chunk(tmp_path, entry.key, 99) is None


def test_没抓过就返回空(tmp_path: Path) -> None:
    assert load(tmp_path, "https://example.com/never") is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://127.0.0.1:8080/props",
        "http://localhost/x",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
    ],
)
def test_内网与本地方案一律拦掉(url: str) -> None:
    """这个工具是给会写文件、会跑命令的 agent 用的，不能让它顺手探测内网。"""
    with pytest.raises(WebError):
        check(url, WebPolicy())


def test_黑白名单由调用方给(monkeypatch) -> None:
    monkeypatch.setattr("spoolkit.fetch.client._is_public", lambda host: True)
    policy = WebPolicy(allow=("example.com",))
    assert check("https://docs.example.com/x", policy)
    with pytest.raises(WebError, match="不在白名单"):
        check("https://other.com/x", policy)

    deny = WebPolicy(deny=("ads.example.com",))
    with pytest.raises(WebError, match="黑名单"):
        check("https://ads.example.com/x", deny)


def test_超过大小上限就中止(tmp_path: Path, monkeypatch) -> None:
    """上限按**读到的字节**算，不看 Content-Length（那可以撒谎）。"""
    monkeypatch.setattr("spoolkit.fetch.client._is_public", lambda host: True)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - 标准库命名
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"x" * 50_000)

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        policy = WebPolicy(max_bytes=1000, proxy=None)
        with pytest.raises(WebError, match="上限"):
            fetch_text(f"http://127.0.0.1:{server.server_port}/big", policy)
    finally:
        server.shutdown()
