"""网页搜索插头：没配要说清楚，不能去抓搜索结果页。"""

import json
from pathlib import Path
from types import SimpleNamespace

from spoolkit.cli.runtime import web_search_backend
from spoolkit.config import Config
from spoolkit.fetch.client import Fetched, WebPolicy
from spoolkit.fetch.search import SearchHit, TavilySearch, UnconfiguredSearch
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.types import ToolCall
from spoolkit.tools.webfetch import WebAccess
from spoolkit.tools.websearch import web_search_spec


def _turn(thought: str, calls=None, final=None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def test_没配搜索时工具直接说清楚() -> None:
    result = web_search_spec(UnconfiguredSearch()).handler({"query": "pathlib walk"})
    assert result.ok is False
    assert "没配搜索" in result.content
    assert "URL" in result.content


def test_空查询要拒绝() -> None:
    result = web_search_spec(UnconfiguredSearch()).handler({"query": "  "})
    assert result.ok is False
    assert "query" in result.content or "词" in result.content


class _FakeSearch:
    configured = True

    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.queries: list[str] = []

    def search(self, query: str, limit: int) -> list[SearchHit]:
        self.queries.append(query)
        return self.hits[:limit]


def test_假后端只交标题和网址不交整页() -> None:
    backend = _FakeSearch(
        [SearchHit(title="Timeouts", url="https://example.com/t", snippet="connect/read")]
    )
    result = web_search_spec(backend).handler({"query": "httpx timeout"})
    assert result.ok
    assert "https://example.com/t" in result.content
    assert "Timeouts" in result.content
    assert backend.queries == ["httpx timeout"]


def test_没key时后端仍是未配置(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SPOOLKIT_TAVILY_API_KEY", raising=False)
    backend = web_search_backend(SimpleNamespace(web_search_api="", proxy=""))
    assert backend.configured is False
    assert isinstance(backend, UnconfiguredSearch)


def test_环境变量有tavily_key就接上(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-from-env")
    backend = web_search_backend(
        SimpleNamespace(web_search_api="", proxy="socks5://127.0.0.1:1080")
    )
    assert isinstance(backend, TavilySearch)
    assert backend.api_key == "tvly-from-env"
    assert backend.proxy == "socks5://127.0.0.1:1080"


def test_工作区env里的tavily_key也能用(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SPOOLKIT_TAVILY_API_KEY", raising=False)
    (tmp_path / ".env").write_text("TAVILY_API_KEY=tvly-workspace\n", encoding="utf-8")
    backend = web_search_backend(
        SimpleNamespace(web_search_api="tavily", proxy=""), tmp_path
    )
    assert isinstance(backend, TavilySearch)
    assert backend.api_key == "tvly-workspace"


def test_配置文件里的tavily_key也能用(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SPOOLKIT_TAVILY_API_KEY", raising=False)
    from spoolkit import settings

    path = settings.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('tavily_api_key = "tvly-from-toml"\n', encoding="utf-8")
    backend = web_search_backend(SimpleNamespace(web_search_api="", proxy=""))
    assert isinstance(backend, TavilySearch)
    assert backend.api_key == "tvly-from-toml"


def test_点了别家名字即使有tavily_key也不装(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-from-env")
    backend = web_search_backend(SimpleNamespace(web_search_api="brave", proxy=""))
    assert backend.configured is False
    assert getattr(backend, "wanted", "") == "brave"


def test_主循环不挂web_search(tmp_path: Path) -> None:
    from spoolkit.cli.runtime import LoopWiring, assemble_loop

    def fake_fetch(url: str, policy: WebPolicy) -> Fetched:
        return Fetched(url=url, status=200, content_type="text/html", body=b"<p>x</p>")

    access = WebAccess(root=tmp_path, policy=WebPolicy(), fetch=fake_fetch)
    loop = assemble_loop(
        tmp_path,
        FakeModel(script=[], tokenizer=OfflineTokenCounter()),
        config=Config(project_root=tmp_path, supervise=False),
        wiring=LoopWiring(
            mcp=False, pending=PendingChanges(tmp_path), web=access
        ),
    )
    assert loop.registry.get("web_search") is None
    denied = loop.registry.invoke(ToolCall(name="web_search", arguments={"query": "x"}))
    assert denied.ok is False
    assert "未知工具" in denied.content


def test_研究会话里调web_search没配会说清楚(tmp_path: Path) -> None:
    from spoolkit.cli.runtime import LoopWiring, assemble_loop

    def fake_fetch(url: str, policy: WebPolicy) -> Fetched:
        return Fetched(url=url, status=200, content_type="text/html", body=b"<p>x</p>")

    access = WebAccess(root=tmp_path, policy=WebPolicy(), fetch=fake_fetch)
    tokenizer = OfflineTokenCounter()
    loop = assemble_loop(
        tmp_path,
        FakeModel(
            script=[
                _turn(
                    "搜",
                    [{"name": "web_search", "arguments": {"query": "pathlib walk"}}],
                ),
                _turn(
                    "交卷",
                    [],
                    final="结论：没配搜索\n引用：无\n不确定：只能给 URL\n继续：否",
                ),
            ],
            tokenizer=tokenizer,
        ),
        config=Config(
            project_root=tmp_path, supervise=False, context_window=4096
        ),
        wiring=LoopWiring(
            mcp=False, pending=PendingChanges(tmp_path), web=access
        ),
    )
    result = loop.registry.invoke(
        ToolCall(name="research", arguments={"question": "pathlib walk 默认值"})
    )
    assert result.ok
    assert "没配搜索" in result.content
