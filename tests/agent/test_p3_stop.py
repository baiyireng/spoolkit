"""P3-5：停止关掉当前生成，已提交的节还在。"""

import json
import socket
import threading
import time
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.sections import STOP_MARK
from spoolkit.config import Config
from spoolkit.llm.cancel import GenerationCancelled
from spoolkit.llm.llamacpp import LlamaCppGateway
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatRequest, ChatResponse, Message
from spoolkit.tools.registry import ToolRegistry

_TITLES = ("背景", "问题", "方案", "步骤", "风险")


def _control() -> str:
    return json.dumps(
        {
            "thought": "分节写",
            "tool_calls": [],
            "done": True,
            "summary": "无需外部信息。按五节展开。",
            "answer_plan": {"mode": "long", "target_tokens": 2000, "sections": 5},
        },
        ensure_ascii=False,
    )


class _Slow:
    def __init__(self) -> None:
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.sections = 0

    def chat(self, request: ChatRequest) -> ChatResponse:
        blob = "\n".join(item.content for item in request.messages)
        if "不是控制层" in blob:
            text = "\n".join(f"{title} | 写清这一节 | 400" for title in _TITLES)
        elif "只写第" in blob:
            self.sections += 1
            if self.sections == 3:
                self.blocked.set()
                if not self.release.wait(5):
                    raise AssertionError("停止信号没有到")
                raise GenerationCancelled("半截第三节")
            title = next(item for item in _TITLES if f"「{item}」" in blob)
            text = f"只属于{title}的正文。"
        else:
            text = _control()
        return ChatResponse(text=text, prompt_tokens=20, completion_tokens=8)

    def chat_stream(self, request: ChatRequest, on_delta=None) -> ChatResponse:
        return self.chat(request)


def test_长文写到第三节时停止且两秒内结束(tmp_path: Path) -> None:
    gateway = _Slow()

    def release() -> None:
        assert gateway.blocked.wait(2)
        gateway.release.set()

    threading.Thread(target=release, daemon=True).start()
    loop = AgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        tokenizer=OfflineTokenCounter(),
        registry=ToolRegistry(),
        config=Config(
            project_root=tmp_path,
            context_window=32768,
            max_steps=2,
            supervise=False,
            overrides={"protocol_v2": 1},
        ),
    )
    started = time.perf_counter()
    result = loop.run("写一份说明")
    elapsed = time.perf_counter() - started
    assert elapsed < 2
    assert result.finished is False
    assert "## 背景" in result.final
    assert "## 问题" in result.final
    assert "只属于背景的正文" in result.final
    assert "只属于问题的正文" in result.final
    assert "只属于方案的正文" not in result.final
    assert STOP_MARK in result.final
    assert "第 3 节未完成" in result.final


def test_取消会在两秒内关掉挂起的流() -> None:
    ready = threading.Event()
    port: dict[str, int] = {}

    def serve() -> None:
        with socket.create_server(("127.0.0.1", 0)) as server:
            port["n"] = server.getsockname()[1]
            ready.set()
            server.settimeout(5)
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                return
            with conn:
                data = b""
                conn.settimeout(5)
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                body = b'data: {"choices":[{"delta":{"content":"\xe7\x94\xb2"}}]}\n\n'
                frame = f"{len(body):x}\r\n".encode() + body + b"\r\n"
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n" + frame
                )
                time.sleep(20)

    threading.Thread(target=serve, daemon=True).start()
    assert ready.wait(2)
    gateway = LlamaCppGateway(base_url=f"http://127.0.0.1:{port['n']}", timeout=30)
    holder: dict[str, object] = {}

    def run() -> None:
        started = time.perf_counter()
        try:
            gateway.chat_stream(
                ChatRequest(messages=(Message(role="user", content="写"),), max_tokens=32),
                on_delta=lambda _text: holder.setdefault("saw", True),
            )
        except GenerationCancelled as exc:
            holder["error"] = exc
        holder["elapsed"] = time.perf_counter() - started

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.perf_counter() + 2
    while "saw" not in holder and time.perf_counter() < deadline:
        time.sleep(0.01)
    gateway.request_stop()
    worker.join(2)
    gateway.close()
    assert not worker.is_alive()
    assert isinstance(holder.get("error"), GenerationCancelled)
    assert float(holder["elapsed"]) < 2
