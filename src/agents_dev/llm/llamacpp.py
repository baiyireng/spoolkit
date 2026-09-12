"""llama.cpp 网关。

走 llama-server 的 OpenAI 兼容接口。结构化输出用 response_format 携带
JSON Schema，由 llama.cpp 在服务端转换成 GBNF 语法约束采样——
也就是说，本项目不需要自己写 GBNF 生成器，
「按工具动态生成约束」这件事在客户端只是把 schema 传过去。

token 计数走 /tokenize，拿到的是模型真实分词结果，而不是估算。
"""

from typing import Any

import httpx

from agents_dev.llm.types import ChatRequest, ChatResponse, Message
from agents_dev.net import system_proxy
from agents_dev.errors import ContextOverflowError

# 生成时间的超时**不能是常数**：它是「让它生成多少 token」的函数。
#
# 实测踩过这一条：输出预算被抬到 30%（2457 token）之后，一次生成超过了死的
# 180 秒，httpx 抛 ReadTimeout，而循环没有接住——**整条长任务就这么崩了**，
# 检查点虽然在，那一轮的上下文全没了。
#
# 取值按「每秒 5 token」这种保守下界估：宁可多等，也不要因为算得刚刚好而崩。
# 它只是「服务是不是挂了」的安全网，不是性能参数。
SECONDS_PER_OUTPUT_TOKEN = 0.2

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_MODEL = "local"

# 本机地址不走代理。llama-server 按定义就跑在本机，把它的请求交给系统代理
# 只会绕远路——实测（Clash 开着时）直接被代理拒成 502。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def proxy_for(base_url: str) -> str | None:
    """按服务地址决定用不用系统代理。"""
    try:
        host = httpx.URL(base_url).host
    except (httpx.InvalidURL, ValueError):
        return system_proxy()
    if host in _LOCAL_HOSTS:
        return None
    return system_proxy()


class LlamaCppError(RuntimeError):
    """调用 llama.cpp 服务失败。"""


def _to_payload(request: ChatRequest, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ],
        "max_tokens": max(1, request.max_tokens),
        "temperature": 0.0,
        "stream": False,
    }
    if request.response_schema:
        # llama.cpp 会把这里转成 GBNF 语法做约束采样。
        payload["response_format"] = {
            "type": "json_object",
            "schema": request.response_schema,
        }
    return payload


class LlamaCppGateway:
    """实现 ModelGateway 协议的 llama.cpp 客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 180.0,
        proxy: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        # 这是**下限**：每请求的实际超时按 max_tokens 放大（见 _timeout_for）。
        self._timeout = timeout
        self._proxy = proxy
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            # 显式给了 transport（测试用的 MockTransport）就别再套代理：
            # 两者同时给的时候请求不走 transport，测试会去真的打网络。
            proxy=(
                None
                if transport is not None
                else (proxy if proxy is not None else proxy_for(base_url))
            ),
        )

    def chat(self, request: ChatRequest) -> ChatResponse:
        timeout = self._timeout_for(request.max_tokens)
        try:
            response = self._client.post(
                "/v1/chat/completions",
                json=_to_payload(request, self.model),
                timeout=timeout,
            )
        except httpx.ReadTimeout as exc:
            # 分开报：超时和「连不上」是两件事，混在一起会把排查方向指错。
            # 实测那条长任务崩在这里，而报错说的是「请确认服务已启动」——
            # 服务好好的，只是这一次生成比超时还长。
            raise LlamaCppError(
                f"服务端 {timeout:.0f} 秒内没返回（max_tokens={request.max_tokens}，"
                f"提示词约 {len(''.join(m.content for m in request.messages))} 字）。"
                "服务多半没挂，是这次生成太长或太慢——"
                "调小这次的输出量（把活拆开做）比调大超时更管用。"
            ) from exc
        except httpx.HTTPError as exc:
            raise LlamaCppError(
                f"无法连接 llama-server（{type(exc).__name__}），"
                "请确认服务已启动并监听正确端口"
            ) from exc

        if response.status_code != 200:
            # 必须把服务端说的话带上。只报一句「服务返回 400」，等于把唯一的
            # 线索扔掉——是超长、是语法不支持、还是参数写错，全在 body 里。
            body = " ".join((response.text or "").split())[:300]
            if "exceed_context_size" in body or "exceeds the available context" in body:
                raise ContextOverflowError(
                    f"提示词超过服务端上下文上限：{body}"
                )
            raise LlamaCppError(
                f"服务返回 {response.status_code}：{body or '（没有说明）'}"
            )

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LlamaCppError("响应中没有 choices")

        text = (choices[0].get("message") or {}).get("content") or ""
        if not text:
            raise LlamaCppError(
                "模型返回了空内容。如果这是带思考的模型（推理内容走单独的字段），"
                "很可能是推理 token 把输出预算吃光了——用 --reasoning off "
                "重启 llama-server，或者调大 max_tokens 再试"
            )

        usage = data.get("usage") or {}
        return ChatResponse(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            truncated=choices[0].get("finish_reason") in ("length", "max_tokens"),
        )

    def _timeout_for(self, max_tokens: int) -> float:
        """这次请求该等多久。

        不是常数：让它生成 2457 token 和 200 token，该等的时间差一个量级。
        构造时的 timeout 当作下限，实际值按 max_tokens 放大。
        """
        return max(self._timeout, max_tokens * SECONDS_PER_OUTPUT_TOKEN)

    def context_window(self) -> int | None:
        """向 /props 询问实际上下文长度。

        这是唯一可靠的来源：窗口由服务端启动参数决定，客户端无从推断。
        """
        try:
            response = self._client.get("/props")
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        data = response.json()
        settings = data.get("default_generation_settings") or {}
        for candidate in (settings.get("n_ctx"), data.get("n_ctx")):
            if isinstance(candidate, int) and candidate > 0:
                return candidate
        return None

    def token_counter(self):
        """这个供应商能给出**真实**分词结果的计数器。

        预算必须建立在真实计数上：估算分词器和服务端的偏差在中文/代码混排时
        可以大到让本地判定放行、服务端直接拒绝（实测提示词 9772 > 上限 8192）。
        """
        return LlamaCppTokenCounter(
            base_url=self._base_url,
            proxy=self._proxy,
        )

    def close(self) -> None:
        self._client.close()


class LlamaCppTokenCounter:
    """用服务端 /tokenize 做精确计数。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        proxy: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            proxy=proxy if proxy is not None else proxy_for(base_url),
        )

    def count(self, text: str) -> int:
        if not text:
            return 0
        response = self._client.post("/tokenize", json={"content": text})
        if response.status_code != 200:
            raise LlamaCppError(f"tokenize 返回 {response.status_code}")
        return len(response.json().get("tokens", []))

    def close(self) -> None:
        self._client.close()
