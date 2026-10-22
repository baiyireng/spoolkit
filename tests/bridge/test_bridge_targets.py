"""一条通道服务多个工作区：每个聊天各指一个，换绑不用重新配对。

原先桥在启动时钉一个 `--root`，放行记录又挂在工作区里，于是"换绑"= 杀进程 +
重敲长命令 + 重新配对。人还是同一个人，不该重来一遍。
"""
from pathlib import Path

from spoolkit.bridge.channel import Incoming
from spoolkit.bridge.core import Bridge
from spoolkit.kernel import pointers, sessions, trust, workspaces as kws


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    # 通道协议的签名是 `send(text, conversation)`（见 bridge/channel.py），
    # `sent` 里按 (会话, 文本) 存。计划样例把两个形参写反了——照它写的话
    # 存进去的是 (文本, 会话)，断言就永远对不上。
    def send(self, text: str, conversation: str) -> None:
        self.sent.append((conversation, text))

    def close(self) -> None:
        pass


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def _bridge(channel_obj, runner, channel="fake") -> Bridge:
    # 计划样例写的是 `Bridge(channel_obj, runner, channel=channel)`，但第一个位置
    # 参数本身就叫 `channel`（那是通道对象），再传 `channel=` 会"重复给值"直接
    # TypeError。接口清单里这条的名字是 `channel_name`，按它来。
    return Bridge(channel_obj, runner, channel_name=channel)


def test_没被放行的人拿码但进不了_agent(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    kws.register(root)
    pointers.set("chat:fake:u1", root, "cli")
    ran: list[str] = []
    channel = _FakeChannel()
    bridge = _bridge(channel, lambda text: ran.append(text) or "答")
    reply = bridge.handle(Incoming(user="u1", text="改点什么", conversation="c1"))
    assert reply.accepted is False
    assert ran == []
    assert any(pair[0] and "配对码" in pair[1] for pair in channel.sent)


def test_放行之后能干活并且写进那个工作区(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    kws.register(root)
    pointers.set("chat:fake:u1", root, "cli")
    trust.grant("fake", "u1", [root])
    seen: list[str] = []
    bridge = _bridge(_FakeChannel(), lambda text: seen.append(text) or "答")
    reply = bridge.handle(Incoming(user="u1", text="改点什么", conversation="c1"))
    assert reply.accepted is True
    assert seen == ["改点什么"]


def test_两个聊天各指一个工作区(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kws.register(a)
    kws.register(b)
    trust.grant("fake", "u1", [a, b])
    pointers.set("chat:fake:u1", a, "cli")
    pointers.set("chat:fake:u2", b, "cli")
    bridge = _bridge(_FakeChannel(), lambda text: "答")
    assert bridge.target_for("u1").root == a
    assert bridge.target_for("u2").root == b


def test_没被放行到的工作区够不到(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kws.register(a)
    kws.register(b)
    trust.grant("fake", "u1", [a])
    pointers.set("chat:fake:u1", b, "cli")  # 指针指着一个没放行的地方
    ran: list[str] = []
    channel = _FakeChannel()
    bridge = _bridge(channel, lambda text: ran.append(text) or "答")
    reply = bridge.handle(Incoming(user="u1", text="干活", conversation="c1"))
    assert reply.accepted is False
    assert ran == []
    # 要点：说清"未被放行"到哪个工作区（而不是"够不到"这类口语）
    denial = "".join(text for _, text in channel.sent)
    assert "被放行" in denial and str(b) in denial


def test_第一次说话会给这个聊天开一条会话(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    kws.register(root)
    trust.grant("fake", "u1", [root])
    pointers.set("chat:fake:u1", root, "")
    bridge = _bridge(_FakeChannel(), lambda text: "答")
    bridge.handle(Incoming(user="u1", text="修好 calc.py 里 sum_to", conversation="c1"))
    made = pointers.get("chat:fake:u1").session
    assert made
    assert sessions.list_sessions(root)[0].title == "修好 calc.py 里 sum_to"


def test_ws_use_切工作区不用重新配对(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kws.register(a)
    kws.register(b)
    trust.grant("fake", "u1", [a, b])
    pointers.set("chat:fake:u1", a, "cli")
    channel = _FakeChannel()
    bridge = _bridge(channel, lambda text: "答")
    bridge.handle(Incoming(user="u1", text="/ws use b", conversation="c1"))
    assert pointers.get("chat:fake:u1").workspace == b
    reply = bridge.handle(Incoming(user="u1", text="干活", conversation="c1"))
    assert reply.accepted is True  # 换工作区没有重新配对这一步
