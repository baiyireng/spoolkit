"""分词计数要带记忆。

计数在热路径上：每装一次上下文按区段计数，区段超预算还要二分查找
（一次几十个 HTTP 往返）；而这些文本在相邻调用之间大量重复——
系统提示、状态块、预取大多逐字不变。一次 50 题的自主编排里，
这部分是最主要的"没人认领的时间"。
"""

import json

import httpx

from agents_dev.llm.llamacpp import LlamaCppTokenCounter


class _Spy:
    """假的 /tokenize 服务：数一数到底被问了几次。"""

    def __init__(self) -> None:
        self.calls = 0
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        payload = json.loads(request.content.decode("utf-8"))
        # 一个粗糙但确定的"分词"：按空白切
        return httpx.Response(200, json={"tokens": payload["content"].split()})


def _counter(spy: _Spy) -> LlamaCppTokenCounter:
    return LlamaCppTokenCounter(transport=spy.transport)


def test_同一段文本只问一次() -> None:
    spy = _Spy()
    counter = _counter(spy)
    assert counter.count("你好 世界") == 2
    assert counter.count("你好 世界") == 2
    assert spy.calls == 1


def test_不同文本各问一次() -> None:
    spy = _Spy()
    counter = _counter(spy)
    counter.count("甲")
    counter.count("乙")
    assert spy.calls == 2
    assert counter.requests == 2


def test_空文本不问() -> None:
    spy = _Spy()
    assert _counter(spy).count("") == 0
    assert spy.calls == 0


def test_缓存有上界_不会无界增长() -> None:
    spy = _Spy()
    counter = _counter(spy)
    for index in range(LlamaCppTokenCounter.CACHE_LIMIT + 50):
        counter.count(f"第 {index} 段")
    assert len(counter._cache) <= LlamaCppTokenCounter.CACHE_LIMIT


def test_组装一次上下文省下的往返() -> None:
    """同一批区段重复计数时，缓存把往返次数压到"不同文本的个数"。"""
    spy = _Spy()
    counter = _counter(spy)
    sections = ["系统提示 一段", "系统提示 一段", "状态块 一段", "状态块 一段"]
    for text in sections:
        counter.count(text)
    assert spy.calls == 2
