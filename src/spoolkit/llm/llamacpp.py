"""llama.cpp 网关。

走 llama-server 的 OpenAI 兼容接口。结构化输出用 response_format 携带
JSON Schema，由 llama.cpp 在服务端转换成 GBNF 语法约束采样——
也就是说，本项目不需要自己写 GBNF 生成器，
「按工具动态生成约束」这件事在客户端只是把 schema 传过去。

token 计数走 /tokenize，拿到的是模型真实分词结果，而不是估算。
"""

from typing import Any

import httpx

from spoolkit.llm.types import ChatRequest, ChatResponse, Message
from spoolkit.net import system_proxy
from spoolkit.errors import ContextOverflowError

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

# 采样温度默认 0：既有的每一条读数都是在确定性采样下取的，默认值一变，
# 历史记录就再也对不上新记录。要测「波动」必须显式把它抬起来。
DEFAULT_TEMPERATURE = 0.0

# 本机地址不走代理。llama-server 按定义就跑在本机，把它的请求交给系统代理
# 只会绕远路——实测（Clash 开着时）直接被代理拒成 502。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def proxy_for(base_url: str, explicit: str | None = None) -> str | None:
    """按服务地址决定用不用代理：**本机地址一律不走代理**，其余用显式给的或系统代理。

    为什么 `explicit` 也要过这一关：调用方（`cli/runtime.py`）会把
    `system_proxy()` 当成"显式值"传进来，而原先两个构造点写的是
    `proxy if proxy is not None else proxy_for(base_url)`——只要这个值非空，
    本机地址就照样被套上代理。实测（代理对 loopback 回 502）：
    `context_window()` 拿到 None，spool 于是报「无法连接 llama.cpp 服务：
    http://127.0.0.1:8080 …先确认服务已启动」，把一次代理配置问题说成了
    "服务没起"，排查方向被整个带偏。**本机这条规则优先于"显式"**。
    """
    try:
        host = httpx.URL(base_url).host
    except (httpx.InvalidURL, ValueError):
        return explicit or system_proxy()
    if host in _LOCAL_HOSTS:
        return None
    return explicit or system_proxy()


class LlamaCppError(RuntimeError):
    """调用 llama.cpp 服务失败。"""


def _to_payload(
    request: ChatRequest,
    model: str,
    temperature: float = DEFAULT_TEMPERATURE,
    seed: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ],
        "max_tokens": max(1, request.max_tokens),
        "temperature": float(temperature),
        "stream": False,
    }
    if seed is not None:
        # 温度 > 0 时不给种子，同一句提示词每次采样都不同——那么「这一次红」
        # 就没法跟「上一次绿」比较了。所以种子只在显式给出时才下发，
        # 默认保持「服务端说了算」（llama.cpp 自己会随机取）。
        payload["seed"] = int(seed)
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
        temperature: float = DEFAULT_TEMPERATURE,
        seed: int | None = None,
    ) -> None:
        self.model = model
        # 采样参数属于「这一次跑分用的是什么设置」的一部分，必须由构造方给进来。
        # 温度 0 时种子不起作用；温度 > 0 时给了种子，同一次跑分才可复现。
        self.temperature = float(temperature)
        self.seed = None if seed is None else int(seed)
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
            # 本机地址一律不走代理——连显式传进来的 `proxy` 也要过这一关，
            # 否则调用方把 `system_proxy()` 当显式值传进来就绕过了下面那条规则。
            proxy=(None if transport is not None else proxy_for(base_url, proxy)),
            # 代理已经由 proxy_for 自己判完了（本机地址一律不走代理），
            # 不能再让 httpx 去读环境变量：环境里只要有一个 `NO_PROXY=...,[::1]`，
            # httpx 0.28 在**构造这一步**就会抛 `InvalidURL: Invalid port: ':1]'`，
            # 本机 8080 还没碰就死了。status.py / health.py 早就是这么写的。
            trust_env=False,
        )

    def chat(self, request: ChatRequest) -> ChatResponse:
        timeout = self._timeout_for(request.max_tokens)
        try:
            response = self._client.post(
                "/v1/chat/completions",
                json=_to_payload(
                    request, self.model, self.temperature, self.seed
                ),
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
    """用服务端 /tokenize 做精确计数。

    **必须带记忆**：计数在热路径上——每装一次上下文要按区段计数，
    区段超预算时还要二分查找（一次几十个 HTTP 往返），而这些文本
    在相邻几次调用之间大量重复（系统提示、状态块、预取都逐字不变）。
    实测一次 50 题的自主编排里，未命中的计数是最主要的"没人认领的时间"。
    """

    CACHE_LIMIT = 256

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        proxy: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._cache: dict[str, int] = {}
        self._requests = 0
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            proxy=None if transport is not None else proxy_for(base_url, proxy),
            # 同上：分词器连的也是本机 llama-server，不让 httpx 读环境代理。
            trust_env=False,
        )

    def count(self, text: str) -> int:
        if not text:
            return 0
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        response = self._client.post("/tokenize", json={"content": text})
        self._requests += 1
        if response.status_code != 200:
            raise LlamaCppError(f"tokenize 返回 {response.status_code}")
        value = len(response.json().get("tokens", []))
        if len(self._cache) >= self.CACHE_LIMIT:
            # 长任务里提示词各不相同，缓存得有上界；满了就整体丢掉，
            # 不做 LRU——这段代码不值得为淘汰策略再引入一层结构。
            self._cache.clear()
        self._cache[text] = value
        return value

    @property
    def requests(self) -> int:
        """真正发出去的 /tokenize 次数（调这块时唯一能看的信号）。"""
        return self._requests

    def close(self) -> None:
        self._client.close()
