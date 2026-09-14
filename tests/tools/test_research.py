"""独立研究会话：主循环调一个动作，拿回压好的短结论。

它不是每轮必经的阶段。验收盯三件事：① 工具表里没有写工具；② 多页是
地图-归并（每页一个干净上下文，合并时不把五页正文一起塞）；③ 交回主循环
的文本有预算，由调用方给，不写死。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.agents.runtime import RESEARCHER, restrict
from spoolkit.config import Config
from spoolkit.fetch.client import Fetched, WebPolicy
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.tools.exec import run_command_spec
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.research import research_spec
from spoolkit.tools.types import ToolCall
from spoolkit.tools.webfetch import WebAccess, web_fetch_spec, web_read_spec


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


def _page(title: str, body: str) -> bytes:
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1><p>{body}</p></body></html>"
    ).encode("utf-8")


def _access(tmp_path: Path, pages: dict[str, bytes]) -> WebAccess:
    def fake_fetch(url: str, policy: WebPolicy) -> Fetched:
        body = pages.get(url)
        if body is None:
            return Fetched(url=url, status=404, content_type="text/html", body=b"")
        return Fetched(url=url, status=200, content_type="text/html", body=body)

    return WebAccess(root=tmp_path, policy=WebPolicy(chunk_chars=400), fetch=fake_fetch)


def _parent_registry(tmp_path: Path, access: WebAccess) -> ToolRegistry:
    """主循环那种「什么都有」的注册表——用来证明裁剪真的把写工具拿掉了。"""
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    registry.register(write_file_spec(tmp_path, pending))
    registry.register(run_command_spec(tmp_path, pending))
    registry.register(web_fetch_spec(access))
    registry.register(web_read_spec(access))
    return registry


def test_裁剪后研究会话调用写工具会被拒绝(tmp_path: Path) -> None:
    access = _access(tmp_path, {})
    limited = restrict(_parent_registry(tmp_path, access), RESEARCHER)
    names = limited.names()
    assert "web_fetch" in names and "web_read" in names
    assert "write_file" not in names
    assert "run_command" not in names
    result = limited.invoke(
        ToolCall(name="write_file", arguments={"path": "a.py", "content": "x"})
    )
    assert result.ok is False
    assert "未知工具" in result.content


def test_没给问题不能研究(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    gateway = FakeModel(script=[], tokenizer=tokenizer)
    access = _access(tmp_path, {})
    registry = _parent_registry(tmp_path, access)
    tool = research_spec(
        gateway, registry, Config(project_root=tmp_path, supervise=False), tokenizer
    )
    result = tool.handler({"question": "  "})
    assert result.ok is False
    assert "问题" in result.content
    assert gateway.requests == []


def test_一页研究只把短结论交回主循环(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    report = (
        "结论：exist_ok 默认 False\n"
        "引用：https://example.com/mkdir#0\n"
        "不确定：无\n"
        "继续：否"
    )
    gateway = FakeModel(
        script=[_turn("读完了", [], final=report)],
        tokenizer=tokenizer,
    )
    pages = {"https://example.com/mkdir": _page("mkdir", "exist_ok defaults to false. " * 40)}
    access = _access(tmp_path, pages)
    registry = _parent_registry(tmp_path, access)
    config = Config(project_root=tmp_path, supervise=False, context_window=4096)
    tool = research_spec(gateway, registry, config, tokenizer)
    result = tool.handler(
        {
            "question": "exist_ok 默认值？",
            "urls": ["https://example.com/mkdir"],
        }
    )
    assert result.ok
    assert "exist_ok 默认 False" in result.content
    assert "https://example.com/mkdir#0" in result.content
    assert result.usage is not None and result.usage.calls >= 1
    # 正文很长，但交回主循环的不应把整页搬过去
    assert "exist_ok defaults to false" not in result.content


def test_没配搜索时不给urls仍从问题里查(tmp_path: Path) -> None:
    """T2 路径：题面里带 URL、没配搜索。不能因为没搜索就整段 research 拒绝。"""
    tokenizer = OfflineTokenCounter()
    gateway = FakeModel(
        script=[_turn("有地址", [], final="结论：3.13\n引用：https://example.com/#0\n不确定：无\n继续：否")],
        tokenizer=tokenizer,
    )
    registry = _parent_registry(tmp_path, _access(tmp_path, {}))
    tool = research_spec(
        gateway,
        registry,
        Config(project_root=tmp_path, supervise=False, context_window=4096),
        tokenizer,
    )
    result = tool.handler({"question": "版本？页面：https://example.com/"})
    assert result.ok
    assert "3.13" in result.content


def test_配了搜索且没给urls就先搜再按页读(tmp_path: Path) -> None:
    from spoolkit.fetch.search import SearchHit

    class FakeSearch:
        configured = True

        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query: str, limit: int) -> list[SearchHit]:
            self.queries.append(query)
            return [
                SearchHit(title="A", url="https://a.example/", snippet="alpha"),
            ][:limit]

    tokenizer = OfflineTokenCounter()
    gateway = FakeModel(
        script=[
            _turn(
                "读A",
                [],
                final="结论：A要点\n引用：https://a.example/#0\n不确定：无\n继续：否",
            ),
        ],
        tokenizer=tokenizer,
    )
    pages = {"https://a.example/": _page("A", "ALPHA_UNIQUE_BODY " * 40)}
    registry = _parent_registry(tmp_path, _access(tmp_path, pages))
    search = FakeSearch()
    tool = research_spec(
        gateway,
        registry,
        Config(project_root=tmp_path, supervise=False, context_window=4096),
        tokenizer,
        search=search,
    )
    result = tool.handler({"question": "A 说什么"})
    assert result.ok
    assert "A要点" in result.content
    assert "ALPHA_UNIQUE_BODY" not in result.content
    assert search.queries == ["A 说什么"]


def test_多页是每页一个上下文再合并_不把五页正文一起塞(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    long_a = "ALPHA_UNIQUE_BODY " * 80
    long_b = "BETA_UNIQUE_BODY " * 80
    gateway = FakeModel(
        script=[
            _turn("A页", [], final="结论：A\n引用：https://a.example/#0\n不确定：无\n继续：否"),
            _turn("B页", [], final="结论：B\n引用：https://b.example/#0\n不确定：无\n继续：否"),
            _turn(
                "合并",
                [],
                final="结论：A 且 B\n引用：https://a.example/#0；https://b.example/#0\n不确定：无\n继续：否",
            ),
        ],
        tokenizer=tokenizer,
    )
    access = _access(
        tmp_path,
        {
            "https://a.example/": _page("A", long_a),
            "https://b.example/": _page("B", long_b),
        },
    )
    registry = _parent_registry(tmp_path, access)
    tool = research_spec(
        gateway,
        registry,
        Config(project_root=tmp_path, supervise=False, context_window=4096),
        tokenizer,
    )
    result = tool.handler(
        {
            "question": "A 和 B 各说什么",
            "urls": ["https://a.example/", "https://b.example/"],
        }
    )
    assert result.ok
    assert "A 且 B" in result.content
    # 三次独立会话：两页地图 + 一次合并
    assert len(gateway.requests) == 3
    merge_user = "\n".join(
        m.content for m in gateway.requests[-1].messages if m.role == "user"
    )
    assert "ALPHA_UNIQUE_BODY" not in merge_user
    assert "BETA_UNIQUE_BODY" not in merge_user


def test_交回主循环的长度走标定值(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    huge = "结论：" + ("很长的话 " * 200) + "\n引用：https://example.com/#0\n不确定：无\n继续：否"
    gateway = FakeModel(script=[_turn("读完了", [], final=huge)], tokenizer=tokenizer)
    access = _access(tmp_path, {"https://example.com/": _page("X", "hello")})
    registry = _parent_registry(tmp_path, access)
    config = Config(
        project_root=tmp_path,
        supervise=False,
        context_window=4096,
        overrides={"research_output_budget": 40},
    )
    tool = research_spec(gateway, registry, config, tokenizer)
    result = tool.handler(
        {"question": "说什么", "urls": ["https://example.com/"]}
    )
    assert result.ok
    assert tokenizer.count(result.content) <= 40


def test_页数上限由调用方给(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    gateway = FakeModel(
        script=[_turn("只读第一页", [], final="结论：只要第一页\n引用：https://a.example/#0\n不确定：无\n继续：是")],
        tokenizer=tokenizer,
    )
    access = _access(
        tmp_path,
        {
            "https://a.example/": _page("A", "a"),
            "https://b.example/": _page("B", "b"),
            "https://c.example/": _page("C", "c"),
        },
    )
    registry = _parent_registry(tmp_path, access)
    config = Config(
        project_root=tmp_path,
        supervise=False,
        overrides={"research_page_limit": 1},
    )
    tool = research_spec(gateway, registry, config, tokenizer)
    result = tool.handler(
        {
            "question": "三页都看",
            "urls": ["https://a.example/", "https://b.example/", "https://c.example/"],
        }
    )
    assert result.ok
    # 超限的页要说出来，好让主循环决定要不要再调一次
    assert "https://b.example/" in result.content or "继续" in result.content
    assert len(gateway.requests) == 1


def test_没开web就不挂research工具(tmp_path: Path) -> None:
    from spoolkit.cli.runtime import LoopWiring, assemble_loop

    tokenizer = OfflineTokenCounter()
    loop = assemble_loop(
        tmp_path,
        FakeModel(script=[], tokenizer=tokenizer),
        config=Config(project_root=tmp_path, supervise=False),
        wiring=LoopWiring(mcp=False, pending=PendingChanges(tmp_path)),
    )
    assert loop.registry.get("research") is None
    assert loop.registry.get("web_fetch") is None


def test_开了web才挂research工具(tmp_path: Path) -> None:
    from spoolkit.cli.runtime import LoopWiring, assemble_loop

    tokenizer = OfflineTokenCounter()
    access = _access(tmp_path, {})
    loop = assemble_loop(
        tmp_path,
        FakeModel(script=[], tokenizer=tokenizer),
        config=Config(project_root=tmp_path, supervise=False),
        wiring=LoopWiring(
            mcp=False, pending=PendingChanges(tmp_path), web=access
        ),
    )
    assert loop.registry.get("research") is not None
    assert loop.registry.get("web_read") is not None
    # 主循环不挂 web_fetch：T2 实测写进提示词它仍走 web_fetch，
    # 主循环 token 降不下来。抓取只留在研究会话里。
    assert loop.registry.get("web_fetch") is None
    assert loop.registry.get("web_search") is None
    from spoolkit.tools.types import ToolCall

    denied = loop.registry.invoke(
        ToolCall(name="web_fetch", arguments={"url": "https://example.com/"})
    )
    assert denied.ok is False
    assert "未知工具" in denied.content


def test_主循环有磁盘缓存也不能直接web_read(tmp_path: Path) -> None:
    """同一工作区下一轮：缓存还在，但本轮还没 research，核对工具必须拒绝。"""
    from spoolkit.cli.runtime import LoopWiring, assemble_loop
    from spoolkit.tools.types import ToolCall

    previous = _access(tmp_path, {"https://example.com/": _page("T", "cached body")})
    web_fetch_spec(previous).handler({"url": "https://example.com/"})
    this_run = _access(tmp_path, {})
    tokenizer = OfflineTokenCounter()
    loop = assemble_loop(
        tmp_path,
        FakeModel(script=[], tokenizer=tokenizer),
        config=Config(project_root=tmp_path, supervise=False),
        wiring=LoopWiring(
            mcp=False, pending=PendingChanges(tmp_path), web=this_run
        ),
    )
    result = loop.registry.invoke(
        ToolCall(
            name="web_read",
            arguments={"source": "https://example.com/", "chunk": 0},
        )
    )
    assert result.ok is False
    assert "research" in result.content
    assert "cached body" not in result.content


def test_research本轮抓过之后主循环才能核对缓存(tmp_path: Path) -> None:
    from spoolkit.cli.runtime import LoopWiring, assemble_loop
    from spoolkit.tools.types import ToolCall

    url = "https://example.com/"
    pages = {url: _page("T", "cached body from disk")}
    previous = _access(tmp_path, pages)
    web_fetch_spec(previous).handler({"url": url})
    this_run = _access(tmp_path, pages)
    tokenizer = OfflineTokenCounter()
    gateway = FakeModel(
        script=[
            _turn(
                "抓",
                [{"name": "web_fetch", "arguments": {"url": url}}],
            ),
            _turn(
                "交卷",
                [],
                final="结论：T\n引用：https://example.com/#0\n不确定：无\n继续：否",
            ),
        ],
        tokenizer=tokenizer,
    )
    loop = assemble_loop(
        tmp_path,
        gateway,
        config=Config(
            project_root=tmp_path, supervise=False, context_window=4096
        ),
        wiring=LoopWiring(
            mcp=False, pending=PendingChanges(tmp_path), web=this_run
        ),
    )
    researched = loop.registry.invoke(
        ToolCall(name="research", arguments={"question": "T 说什么", "urls": [url]})
    )
    assert researched.ok
    result = loop.registry.invoke(
        ToolCall(name="web_read", arguments={"source": url, "chunk": 0})
    )
    assert result.ok
    assert "cached body from disk" in result.content
