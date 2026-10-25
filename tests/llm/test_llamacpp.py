import json

import httpx
import pytest

from spoolkit.llm.llamacpp import (
    LlamaCppError,
    LlamaCppGateway,
    LlamaCppTokenCounter,
    proxy_for,
)
from spoolkit.errors import ContextOverflowError
from spoolkit.llm.types import ChatRequest, Message


def _completion(text: str = '{"thought":"t","tool_calls":[],"done":true,"final":"好"}') -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 21, "completion_tokens": 9},
    }


def _gateway(handler, **kwargs) -> LlamaCppGateway:
    return LlamaCppGateway(transport=httpx.MockTransport(handler), **kwargs)


def _request(schema=None) -> ChatRequest:
    return ChatRequest(
        messages=(Message(role="user", content="任务"),),
        max_tokens=256,
        response_schema=schema,
    )


def test_一次请求记入指标且不含正文() -> None:
    from spoolkit.agent.metrics import capture_metrics, dumps_metric, metric_scope

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "答案正文"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4000, "completion_tokens": 12},
                "timings": {"prompt_n": 1000},
            },
        )

    gateway = _gateway(handler)
    with capture_metrics() as rows, metric_scope(phase="final", task_type="long", n_ctx=8192):
        gateway.chat(
            ChatRequest(
                messages=(Message(role="user", content="独特口令甲乙丙"),),
                max_tokens=200,
            )
        )
    gateway.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["phase"] == "final"
    assert row["task_type"] == "long"
    assert row["prompt_tokens"] == 4000
    assert row["prompt_n"] == 1000
    assert row["max_tokens"] == 200
    assert row["generated_tokens"] == 12
    assert row["finish_reason"] == "stop"
    assert row["cache_hit"] is True
    assert row["cache_reused_tokens"] == 3000
    assert "独特口令" not in dumps_metric(row)


def test_没有重算量时缓存命中留空() -> None:
    from spoolkit.agent.metrics import capture_metrics, metric_scope

    gateway = _gateway(lambda request: httpx.Response(200, json=_completion()))
    with capture_metrics() as rows, metric_scope(phase="control", task_type="short"):
        gateway.chat(_request())
    gateway.close()
    assert rows[0]["cache_hit"] is None
    assert rows[0]["prompt_n"] is None
    assert rows[0]["prompt_tokens"] == 21


def test_解析内容与用量() -> None:
    gateway = _gateway(lambda request: httpx.Response(200, json=_completion()))
    response = gateway.chat(_request())
    assert "final" in response.text
    assert response.prompt_tokens == 21
    assert response.completion_tokens == 9
    gateway.close()


def test_请求打到OpenAI兼容路径() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    gateway = _gateway(handler)
    gateway.chat(_request())
    assert seen["path"] == "/v1/chat/completions"
    assert seen["payload"]["messages"][0]["role"] == "user"
    assert seen["payload"]["stream"] is False
    gateway.close()


def test_有schema时通过response_format下发() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    gateway = _gateway(handler)
    gateway.chat(_request(schema))
    assert seen["payload"]["response_format"]["schema"] == schema
    gateway.close()


def test_cache_prompt总是显式下发() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    gateway = _gateway(handler)
    gateway.chat(_request())
    assert seen["payload"]["cache_prompt"] is False
    gateway.chat(
        ChatRequest(
            messages=(Message(role="user", content="终稿"),),
            max_tokens=100,
            cache_prompt=True,
        )
    )
    assert seen["payload"]["cache_prompt"] is True
    assert "response_format" not in seen["payload"]
    gateway.close()


def test_无schema时不带response_format() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    gateway = _gateway(handler)
    gateway.chat(_request())
    assert "response_format" not in seen["payload"]
    gateway.close()


def test_温度与种子下发给服务端() -> None:
    """温度 0 下重复 N 次只能证明「可复现」，要测波动必须能把温度抬起来。

    所以这两项必须是**可下发的**，而且默认值不能变：默认温度 0 且不带 seed，
    历史读数才对得上新读数。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    gateway = _gateway(handler, temperature=0.4, seed=7)
    gateway.chat(_request())
    assert seen["payload"]["temperature"] == 0.4
    assert seen["payload"]["seed"] == 7
    gateway.close()

    default = _gateway(handler)
    default.chat(_request())
    assert seen["payload"]["temperature"] == 0.0
    assert "seed" not in seen["payload"], "不给种子就让服务端自己取"
    default.close()


def test_服务返回非200时报错() -> None:
    gateway = _gateway(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(LlamaCppError):
        gateway.chat(_request())
    gateway.close()


def test_没有choices时报错() -> None:
    gateway = _gateway(lambda request: httpx.Response(200, json={"choices": []}))
    with pytest.raises(LlamaCppError):
        gateway.chat(_request())
    gateway.close()


def test_内容为空时报错() -> None:
    payload = {"choices": [{"message": {"content": ""}}]}
    gateway = _gateway(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(LlamaCppError):
        gateway.chat(_request())
    gateway.close()


def test_空内容先重试一次成功并记下空响应() -> None:
    from spoolkit.agent.metrics import capture_metrics, metric_scope

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 8000, "completion_tokens": 0},
                },
            )
        return httpx.Response(200, json=_completion("补上了"))

    gateway = _gateway(handler, temperature=0.0)
    with capture_metrics() as rows, metric_scope(phase="section", task_type="long", n_ctx=8192):
        response = gateway.chat(_request())
    assert response.text == "补上了"
    assert calls["n"] == 2
    empty = [row for row in rows if row.get("event") == "empty_response"]
    assert empty[0]["finish_reason"] == "length"
    assert empty[0]["prompt_tokens"] == 8000
    assert empty[0]["max_tokens"] == 256
    assert empty[0]["n_ctx"] == 8192
    assert empty[0]["temperature"] == 0.0
    assert empty[0]["streaming"] is False
    assert empty[0]["retry_index"] == 0
    assert empty[0]["context_tight"] is True
    ok = [row for row in rows if row.get("phase") == "section"]
    assert ok[0]["retry_index"] == 1
    gateway.close()


def test_带重试说明时空内容换提示而不是原样重发() -> None:
    """第二节请求必须多一段「必须输出正文」，不能把第一次的消息原样再发一遍。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 0},
                },
            )
        return httpx.Response(200, json=_completion("正文写出来了"))

    note = "必须输出正文，至少 100 字。没有材料时基于目标写，不要留空。"
    gateway = _gateway(handler)
    response = gateway.chat(
        ChatRequest(
            messages=(
                Message(role="system", content="目标：写复盘"),
                Message(role="user", content="只写第 1 节"),
            ),
            max_tokens=256,
            empty_retry=note,
        )
    )
    assert response.text == "正文写出来了"
    assert len(bodies) == 2
    assert bodies[1]["messages"][:-1] == bodies[0]["messages"]
    assert bodies[1]["messages"][-1] == {"role": "user", "content": note}
    gateway.close()


def test_空内容重试仍空才报错() -> None:
    from spoolkit.llm.llamacpp import EmptyContentError

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            },
        )

    gateway = _gateway(handler)
    with pytest.raises(EmptyContentError) as caught:
        gateway.chat(_request())
    assert calls["n"] == 2
    assert caught.value.context_tight is False
    gateway.close()


def test_连不上服务时给出可操作的提示() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    gateway = _gateway(handler)
    with pytest.raises(LlamaCppError) as excinfo:
        gateway.chat(_request())
    assert "llama-server" in str(excinfo.value)
    gateway.close()


def test_生成超时按输出预算放大() -> None:
    """超时不能是常数：生成 2457 token 和 200 token，该等的时间差一个量级。

    实测踩过：输出预算被抬到 30% 之后，一次生成超过了死的 180 秒，
    ReadTimeout 把整条长任务打崩——而它本来只是个「服务是不是挂了」的安全网。
    """
    gateway = _gateway(lambda request: httpx.Response(200, json=_completion()))
    assert gateway._timeout_for(200) == 180.0  # 构造时给的下限
    assert gateway._timeout_for(2000) == 400.0  # 2000 × 0.2
    gateway.close()


def test_超时和连不上分开报() -> None:
    """混在一起会把排查方向指错：服务好好的，只是这次生成太长。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    gateway = _gateway(handler)
    with pytest.raises(LlamaCppError) as excinfo:
        gateway.chat(_request())
    message = str(excinfo.value)
    assert "没返回" in message
    assert "请确认服务已启动" not in message
    gateway.close()


def test_token计数走服务端tokenize() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"tokens": [1, 2, 3, 4]})

    counter = LlamaCppTokenCounter(transport=httpx.MockTransport(handler))
    assert counter.count("一段文本") == 4
    assert seen["path"] == "/tokenize"
    counter.close()


def test_空文本不请求服务端() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("空文本不应发起请求")

    counter = LlamaCppTokenCounter(transport=httpx.MockTransport(handler))
    assert counter.count("") == 0
    counter.close()


def test_本机地址不走代理() -> None:
    """llama-server 就在本机，把它的请求交给系统代理只会绕远路。

    实测踩过：系统代理开着时，发往 127.0.0.1 的请求被代理拒成 502——
    而这只在「用户的代理是开着的」时候才出现，最容易漏掉。
    """
    assert proxy_for("http://127.0.0.1:8080") is None
    assert proxy_for("http://localhost:8080") is None
    assert proxy_for("http://[::1]:8080") is None


def test_非本机地址仍按系统代理走() -> None:
    """不写死「永远不用代理」——远程的 llama-server 该走代理还是走。"""
    import spoolkit.llm.llamacpp as module

    original = module.system_proxy
    module.system_proxy = lambda: "http://127.0.0.1:7890"
    try:
        assert proxy_for("http://192.168.1.9:8080") == "http://127.0.0.1:7890"
    finally:
        module.system_proxy = original


def test_显式代理也要过本机这一关() -> None:
    """**显式传进来的代理同样不能套到本机地址上**——这才是真出事的那个口子。

    `cli/runtime.py` 把 `system_proxy()` 当"显式值"传给网关，而构造点原先写的是
    `proxy if proxy is not None else proxy_for(base_url)`：只要这个值非空，本机地址
    就绕过判定被套上代理。实测（代理对 loopback 回 502）→ `context_window()` 拿到
    None → spool 报「无法连接 llama.cpp 服务…先确认服务已启动」，把代理问题说成了
    "服务没起"。
    """
    assert proxy_for("http://127.0.0.1:8080", "http://127.0.0.1:7890") is None
    assert proxy_for("http://[::1]:8080", "http://127.0.0.1:7890") is None
    assert (
        proxy_for("https://api.example.com", "http://127.0.0.1:7890")
        == "http://127.0.0.1:7890"
    )


def test_网关构造时本机地址不带代理(monkeypatch) -> None:
    """把规则钉在**构造点**上：光有 `proxy_for` 判对了不够，调用点绕过它才是病根。"""
    import httpx as httpx_module

    import spoolkit.llm.llamacpp as module

    captured: dict = {}
    real = httpx_module.Client

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "Client", spy)
    module.LlamaCppGateway("http://127.0.0.1:8080", proxy="http://127.0.0.1:7890")
    assert captured["proxy"] is None
    assert captured["trust_env"] is False

    captured.clear()
    module.LlamaCppTokenCounter("http://127.0.0.1:8080", proxy="http://127.0.0.1:7890")
    assert captured["proxy"] is None


def test_上下文超长单独成类() -> None:
    """它和「测试没通过」那种失败不是一回事：请求根本没进去。

    实测这条曾经直接把整个运行打断——本地用估算分词判定放行，
    服务端用真实分词拒绝（提示词 9772 > 上限 8192）。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "request (9772 tokens) exceeds the available "
                    "context size (8192 tokens), try increasing it",
                    "type": "exceed_context_size_error",
                }
            },
        )

    gateway = _gateway(handler)
    with pytest.raises(ContextOverflowError):
        gateway.chat(ChatRequest(messages=(Message("user", "任务"),), max_tokens=64))


def test_其它400带上服务端的说明() -> None:
    """只报一句「服务返回 400」等于把唯一的线索扔掉。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "schema 不支持某个字段"}})

    gateway = _gateway(handler)
    with pytest.raises(LlamaCppError) as caught:
        gateway.chat(ChatRequest(messages=(Message("user", "任务"),), max_tokens=64))
    assert "schema 不支持某个字段" in str(caught.value)


def test_网关提供真实分词计数器() -> None:
    """预算是支点，不能建立在估算上。"""
    gateway = _gateway(lambda request: httpx.Response(200, json=_completion()))
    counter = gateway.token_counter()
    assert counter is not None
    counter.close()
