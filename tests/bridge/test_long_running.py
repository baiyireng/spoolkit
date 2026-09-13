"""用聊天通道真的"指挥它干活"时暴露的三件事。

1. **连发两条 = 两个 agent 同时跑**（原先每条消息都新建一个 Runner，
   "已有运行在跑就拒绝"只认自己那个实例）。
2. **长任务的结论没人送回来**（桥等超时就回"我先不等了"，跑完的结论丢掉）。
3. **答复太长发不出去**（对来消息有长度上限，对去消息什么都没有）。
"""

import threading
import time
from pathlib import Path

from spoolkit.bridge.agent_runner import AgentRunner
from spoolkit.bridge.core import Bridge
from spoolkit.bridge.fake import FakeChannel
from spoolkit.bridge.qqbot import MAX_CONTENT_BYTES, _split_for_qq


class _SlowRunner:
    """顶替真的 Runner：一直"在跑"，除非你告诉它跑完了。"""

    def __init__(self, finish_after: int | None = None) -> None:
        self.running = False
        self.calls = 0
        self.rejections = 0
        self._left = finish_after
        self._final = {"text": "结论：改好了"}

    def start(self, message: str) -> bool:
        if self.running:
            self.rejections += 1
            return False
        self.calls += 1
        self.running = True
        self._left = self._left if self._left is not None else None
        return True

    def snapshot(self) -> dict:
        if self.running and self._left is not None:
            self._left -= 1
            if self._left <= 0:
                self.running = False
        return {
            "running": self.running,
            "finished": not self.running,
            "awaiting": 0,
            "final": self._final,
        }

    def confirm(self, apply: bool) -> bool:
        return False


def test_连发两条不会同时跑两个_agent(monkeypatch) -> None:
    import itertools

    import spoolkit.bridge.agent_runner as module

    stub = _SlowRunner()                  # 永远"在跑"
    monkeypatch.setattr(module, "Runner", lambda *a, **k: stub)
    ticks = itertools.count()
    runner = AgentRunner(
        Path("."), timeout=5.0, sleep=lambda _: None, clock=lambda: next(ticks) * 1.0
    )

    assert "不等了" in runner("第一件事")   # 它确实在跑（等到超时）
    second = runner("第二件事")

    assert second == "上一轮还在跑，等它结束再发。"
    assert stub.calls == 1, "同一个工作区里不该同时起第二个 agent"


def test_超时之后结论要补发回来() -> None:
    sent: list[str] = []
    runner = AgentRunner(
        Path("."),
        timeout=0.0,                 # 立刻判超时
        sleep=lambda _: time.sleep(0.01),
        notify=sent.append,
    )
    runner._runner = _SlowRunner(finish_after=2)

    answer = runner("做一件长活")

    assert "不等了" in answer
    deadline = time.monotonic() + 5
    while not sent and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sent and "结论：改好了" in sent[0]


def test_桥把补发挂到它知道的会话上() -> None:
    """补发必须回到**用户那条会话**，不能凭空发。"""
    sent: list[tuple[str, str]] = []

    class _Runner:
        notify = None

        def set_notify(self, notify) -> None:
            self.notify = notify

        def __call__(self, text: str) -> str:
            self.notify("（这一轮跑完了）\n结论")
            return "先不等了"

    channel = FakeChannel()
    inner = _Runner()
    bridge = Bridge(channel, inner, access="open")
    channel.push("me", "做活")

    bridge.run_once()

    # 补发要回到**用户那条会话**（fake 通道里就是 "chat"）
    assert ("chat", "（这一轮跑完了）\n结论") in channel.sent


def test_长答复要切成多条() -> None:
    long_text = "汉" * 2000            # 2000 字 × 3 字节 = 6000 字节
    chunks = _split_for_qq(long_text)

    assert len(chunks) > 1
    assert "".join(chunks) == long_text          # 一个字都不能丢
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= MAX_CONTENT_BYTES


def test_长答复分条时_msg_seq_要递增() -> None:
    """同一条消息的第 N 条回复要带不同的 msg_seq，否则平台按重复丢掉。"""
    from tests.bridge.test_qqbot import _capturing_channel, _ScriptedWS

    channel, _ = _capturing_channel(_ScriptedWS([]))
    channel._last_event_id = "MSG1"
    payloads: list[dict] = []
    original = channel._send_one

    def spy(url, text, kind, seq, headers):
        payloads.append({"seq": seq, "text": text})
        original(url, text, kind, seq, headers)

    channel._send_one = spy
    channel.send("汉" * 2000, "c2c:OPENID")

    assert [item["seq"] for item in payloads] == list(range(1, len(payloads) + 1))
    assert len(payloads) > 1
