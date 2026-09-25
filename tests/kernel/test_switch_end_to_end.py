"""换工作区不用重新配对——这是用户当初抱怨的那条流程，钉成用例。

用假模型（只回放）与假通道：不起真 llama-server、不发真消息。

与计划样例的两处修正（与 Task 6 同源，理由写在注释里）：
1. `_FakeChannel.send` 的形参顺序：通道协议是 `send(text, conversation)`，
   计划样例把它写反了，照抄的话 `sent` 里存的是 (文本, 会话)；
2. 切回 A 得先 `/ws use 项目A` 再 `/use <会话>`：会话**属于工作区**，
   `/use` 只在当前工作区里挑，不能一步跨两个（那是设计 §3.3 归属那一条）。
"""
from pathlib import Path

from spoolkit.bridge.channel import Incoming
from spoolkit.bridge.core import Bridge
from spoolkit.kernel import pointers, sessions, trust, workspaces as kws


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, text: str, conversation: str) -> None:
        self.sent.append((conversation, text))

    def close(self) -> None:
        pass


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def test_换工作区不用重新配对且各留各的会话(tmp_path: Path) -> None:
    a = _ws(tmp_path, "项目A")
    b = _ws(tmp_path, "项目B")
    kws.register(a)
    kws.register(b)

    # 主人一次批准：这个人能碰两台（默认只给当时那一个，这里显式给两个）。
    trust.grant("fake", "u1", [a, b])
    pointers.set("chat:fake:u1", a, "")

    ran: list[str] = []
    channel = _FakeChannel()
    # 计划样例写的是 `channel="fake"`，但第一个位置参数本身就叫 `channel`
    # （那是通道对象）；接口清单里这条叫 `channel_name`（同 Task 6）。
    bridge = Bridge(channel, lambda text: ran.append(text) or "做完", channel_name="fake")

    # 在 A 里说一句 → 开一条会话、留下标题
    bridge.handle(Incoming(user="u1", text="在项目A里改点东西", conversation="c1"))
    session_a = pointers.get("chat:fake:u1").session
    assert session_a
    assert sessions.list_sessions(a)[0].title == "在项目A里改点东西"

    # 切到 B：一句话，**没有重新配对**
    bridge.handle(Incoming(user="u1", text="/ws use 项目B", conversation="c1"))
    assert pointers.get("chat:fake:u1").workspace == b
    bridge.handle(Incoming(user="u1", text="在项目B里干活", conversation="c1"))
    session_b = pointers.get("chat:fake:u1").session
    assert session_b and session_b != session_a
    assert sessions.list_sessions(b)[0].title == "在项目B里干活"

    # 切回 A：先换工作区，再选回那条会话——它还在，标题也在
    bridge.handle(Incoming(user="u1", text="/ws use 项目A", conversation="c1"))
    bridge.handle(Incoming(user="u1", text=f"/use {session_a}", conversation="c1"))
    assert pointers.get("chat:fake:u1").session == session_a
    assert [item.id for item in sessions.list_sessions(a)] == [session_a]
    assert sessions.list_sessions(a)[0].title == "在项目A里改点东西"
    assert len(ran) == 2  # 两条命令、两次干活；`/ws use` 与 `/use` 没进 agent
