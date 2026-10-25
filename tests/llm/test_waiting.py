import threading
import time

import pytest

from spoolkit.llm.gateway import (
    HEARTBEAT_SECONDS,
    CountingGateway,
    WaitingGateway,
    attach_wait_listener,
    wait_line,
)
from spoolkit.llm.types import ChatRequest, ChatResponse, Message


def _request() -> ChatRequest:
    return ChatRequest(messages=(Message(role="user", content="x"),), max_tokens=256)


def _ok() -> ChatResponse:
    return ChatResponse(text="ok", prompt_tokens=1, completion_tokens=1)


class _Immediate:
    def chat(self, request):
        return _ok()


class _Block:
    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate

    def chat(self, request):
        self.gate.wait(timeout=5)
        return _ok()

    def _timeout_for(self, max_tokens: int) -> float:
        return 180.0


class _Boom:
    def chat(self, request):
        raise RuntimeError("down")


def test_间隔是十五秒() -> None:
    assert HEARTBEAT_SECONDS == 15.0


def test_立刻返回零次心跳() -> None:
    ticks: list[tuple[int, float | None]] = []
    gateway = WaitingGateway(
        _Immediate(), on_wait=lambda e, lim: ticks.append((e, lim)), heartbeat=0.05
    )
    assert gateway.chat(_request()).text == "ok"
    time.sleep(0.12)
    assert ticks == []


def test_堵住后按间隔跳() -> None:
    ticks: list[tuple[int, float | None]] = []
    gate = threading.Event()
    gateway = WaitingGateway(
        _Block(gate), on_wait=lambda e, lim: ticks.append((e, lim)), heartbeat=0.05
    )
    worker = threading.Thread(target=lambda: gateway.chat(_request()))
    worker.start()
    deadline = time.monotonic() + 2.0
    while len(ticks) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    gate.set()
    worker.join(timeout=2)
    assert len(ticks) >= 2
    assert ticks[0][1] == 180.0


def test_inner抛错也停线程() -> None:
    ticks: list = []
    gateway = WaitingGateway(
        _Boom(), on_wait=lambda e, lim: ticks.append(e), heartbeat=0.05
    )
    with pytest.raises(RuntimeError, match="down"):
        gateway.chat(_request())
    n = len(ticks)
    time.sleep(0.12)
    assert len(ticks) == n


def test_回调抛错不打断chat() -> None:
    def bad(elapsed, limit):
        raise RuntimeError("listener")

    gate = threading.Event()
    gateway = WaitingGateway(_Block(gate), on_wait=bad, heartbeat=0.05)
    worker = threading.Thread(target=lambda: gateway.chat(_request()), daemon=True)
    worker.start()
    time.sleep(0.12)
    gate.set()
    worker.join(timeout=2)
    assert worker.is_alive() is False


def test_没有timeout_for时limit为None() -> None:
    ticks: list = []
    gate = threading.Event()

    class Plain:
        def chat(self, request):
            gate.wait(timeout=5)
            return _ok()

    gateway = WaitingGateway(
        Plain(), on_wait=lambda e, lim: ticks.append(lim), heartbeat=0.05
    )
    worker = threading.Thread(target=lambda: gateway.chat(_request()))
    worker.start()
    deadline = time.monotonic() + 2.0
    while not ticks and time.monotonic() < deadline:
        time.sleep(0.02)
    gate.set()
    worker.join(timeout=2)
    assert ticks
    assert ticks[0] is None


def test_透过CountingGateway绑定() -> None:
    inner = WaitingGateway(_Immediate())
    wrapped = CountingGateway(inner)
    attach_wait_listener(wrapped, lambda e, lim: None)
    assert inner.on_wait is not None


def test_chat结束调用on_wait_end() -> None:
    ended: list[str] = []
    gateway = WaitingGateway(
        _Immediate(),
        on_wait=lambda e, lim: None,
        on_wait_end=lambda: ended.append("done"),
        heartbeat=0.05,
    )
    assert gateway.chat(_request()).text == "ok"
    assert ended == ["done"]


def test_inner抛错也调用on_wait_end() -> None:
    ended: list[str] = []
    gateway = WaitingGateway(
        _Boom(),
        on_wait=lambda e, lim: None,
        on_wait_end=lambda: ended.append("done"),
        heartbeat=0.05,
    )
    with pytest.raises(RuntimeError, match="down"):
        gateway.chat(_request())
    assert ended == ["done"]


def test_attach绑on_wait_end() -> None:
    inner = WaitingGateway(_Immediate())
    wrapped = CountingGateway(inner)
    end = lambda: None
    attach_wait_listener(wrapped, lambda e, lim: None, on_wait_end=end)
    assert inner.on_wait_end is end


def test_wait_line有无上限() -> None:
    assert wait_line(15, None) == "模型生成中，已等待 15s"
    assert wait_line(15, 180.0) == "模型生成中，已等待 15s / 上限 180s"
