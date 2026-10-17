"""模型网关协议。"""

import contextvars
import threading
import time
from typing import Any, Callable, Protocol

from spoolkit.llm.types import ChatRequest, ChatResponse


class ModelGateway(Protocol):
    """所有模型供应商必须满足的接口。

    真实实现走 llama.cpp HTTP；测试与离线开发使用 FakeModel。
    上层代码只依赖本协议，不依赖具体实现。
    """

    def chat(self, request: ChatRequest) -> ChatResponse:
        """发送请求并返回完整响应。"""
        ...

    def context_window(self) -> int | None:
        """查询该模型实际可用的上下文长度。

        这个值不该由使用者猜：同一个配置文件下，换模型或换 KV cache 量化，
        窗口大小都会变。查不到时返回 None，由上层退回默认值。
        """
        ...


class CountingGateway:
    """把网关包一层，数清某一段工作花了多少。

    只拦 chat：其余属性（context_window / token_counter）原样透传，
    因为被包的代码也需要它们。

    它存在的理由和「时间拆成模型/工具两笔」一样：**账算不清就调不动**。
    拆解那一段原先只有墙钟能看，于是「拆解为什么慢」只能靠猜。
    """

    def __init__(self, inner: ModelGateway) -> None:
        self._inner = inner
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(self, request: ChatRequest) -> ChatResponse:
        response = self._inner.chat(request)
        self.calls += 1
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


HEARTBEAT_SECONDS = 15.0

#: 这一次生成是谁在等。压缩、主循环、督导共用一个网关，不标明的话心跳长得一样。
_wait_phase: contextvars.ContextVar[str] = contextvars.ContextVar(
    "spool_wait_phase", default=""
)


class wait_phase:
    """标出当前这次模型调用属于哪一层。心跳读这个标记。"""

    def __init__(self, phase: str) -> None:
        self._phase = phase
        self._token: contextvars.Token[str] | None = None

    def __enter__(self) -> "wait_phase":
        self._token = _wait_phase.set(self._phase)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _wait_phase.reset(self._token)


def current_wait_phase() -> str:
    return _wait_phase.get()


def wait_line(elapsed: int, limit: float | None, phase: str = "") -> str:
    """心跳给人看的那一行。网页和 stderr 用同一句。

    `phase` 非空时写在前面，用来分开「素材压缩 / 主循环 / 督导」。
    """
    head = f"{phase}：" if phase else ""
    if limit is None:
        return f"{head}模型生成中，已等待 {elapsed}s"
    return f"{head}模型生成中，已等待 {elapsed}s / 上限 {int(limit)}s"


class WaitingGateway:
    """chat() 等待期间按固定间隔叫醒监听器。不改超时、不读流。"""

    def __init__(
        self,
        inner: ModelGateway,
        on_wait: Callable[[int, float | None], None] | None = None,
        heartbeat: float = HEARTBEAT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self.on_wait = on_wait
        self._heartbeat = heartbeat
        self._clock = clock

    def chat(self, request: ChatRequest) -> ChatResponse:
        started = self._clock()
        limit = self._limit_for(request)
        stop = threading.Event()
        phase = current_wait_phase()

        def tick() -> None:
            # 心跳在另一条线程上。ContextVar 不会跟着过去，这里按开跑时的标记重设。
            token = _wait_phase.set(phase)
            try:
                self._tick(stop, started, limit)
            finally:
                _wait_phase.reset(token)

        worker = threading.Thread(target=tick, name="spool-model-wait", daemon=True)
        worker.start()
        try:
            return self._inner.chat(request)
        finally:
            stop.set()
            worker.join(timeout=1.0)

    def _tick(self, stop: threading.Event, started: float, limit: float | None) -> None:
        while not stop.wait(self._heartbeat):
            callback = self.on_wait
            if callback is None:
                continue
            elapsed = int(self._clock() - started)
            try:
                callback(elapsed, limit)
            except Exception:
                pass

    def _limit_for(self, request: ChatRequest) -> float | None:
        fn = getattr(self._inner, "_timeout_for", None)
        if not callable(fn):
            return None
        try:
            value = fn(request.max_tokens)
        except Exception:
            return None
        if value is None:
            return None
        return float(value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def attach_wait_listener(
    gateway: object, on_wait: Callable[[int, float | None], None]
) -> None:
    """找到 WaitingGateway（可能被 CountingGateway 包着）并绑回调。找不到就静默。"""
    current = gateway
    for _ in range(8):
        if isinstance(current, WaitingGateway):
            current.on_wait = on_wait
            return
        current = getattr(current, "_inner", None)
        if current is None:
            return

