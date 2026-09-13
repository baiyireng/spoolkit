"""通道插件装载：接一家新通道不必改核心。

这是从 OpenClaw 抄来的那件重要的东西——它的核心一行微信代码都没有，
通道是外部插件。我们这边对应的两条路：装好的包走 entry point，
自己写的脚本走环境变量给模块名。
"""

import sys
from pathlib import Path

from agents_dev.bridge.plugins import ENTRY_POINT_GROUP, load_channel_factories


def _write_plugin(tmp_path: Path) -> None:
    (tmp_path / "my_channel.py").write_text(
        "\n".join(
            [
                '"""一个最小的通道插件：证明装载这条路是通的。"""',
                "from agents_dev.bridge.channel import Incoming",
                "",
                "class MyChannel:",
                '    name = "my"',
                "    def __init__(self, spec=None):",
                "        self.spec = spec or {}",
                "        self.sent = []",
                "        self._inbox = []",
                "    def send(self, text, to=''):",
                "        self.sent.append((to, text))",
                "    def poll(self):",
                "        items, self._inbox = self._inbox, []",
                "        return items",
                "    def close(self):",
                "        pass",
                "    def push(self, user, text):",
                "        self._inbox.append(Incoming(user=user, text=text))",
                "",
                "def build(spec):",
                "    return MyChannel(spec)",
            ]
        ),
        encoding="utf-8",
    )


def test_按模块名装载自己写的通道(tmp_path: Path, monkeypatch) -> None:
    _write_plugin(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("AGENTS_DEV_CHANNEL_PLUGINS", "my_channel:build")

    factories = load_channel_factories()

    assert "my_channel" in factories  # 名字取模块名最后一段，不是函数名
    channel = factories["my_channel"]({"name": "my", "root": str(tmp_path)})
    assert channel.spec["root"] == str(tmp_path)


def test_不带冒号时整模块当工厂(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "simple_channel.py").write_text(
        "class build:\n"
        "    def __init__(self, spec=None):\n"
        "        self.spec = spec or {}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("AGENTS_DEV_CHANNEL_PLUGINS", "simple_channel")

    factories = load_channel_factories()

    assert "simple_channel" in factories


def test_装载不了的插件跳过_不拖垮其它的(tmp_path: Path, monkeypatch) -> None:
    _write_plugin(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(
        "AGENTS_DEV_CHANNEL_PLUGINS",
        "不存在的模块,my_channel:不存在的函数,my_channel:build",
    )

    factories = load_channel_factories()

    assert list(factories) == ["my_channel"]  # 坏的跳过，好的留下


def test_没配就没有插件(monkeypatch) -> None:
    monkeypatch.delenv("AGENTS_DEV_CHANNEL_PLUGINS", raising=False)
    assert ENTRY_POINT_GROUP == "agents_dev.channels"
    # 本仓库没有装任何通道插件，所以这里应当是空的（entry point 那条路
    # 由发布出去的插件包负责，测试里不假装装了一个）。
    assert load_channel_factories() == {}


def test_插件通道能接进桥(tmp_path: Path, monkeypatch) -> None:
    """装载的目的就是这个：新通道进来就能用，桥与核心都不用改。"""
    _write_plugin(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("AGENTS_DEV_CHANNEL_PLUGINS", "my_channel:build")

    from agents_dev.bridge.core import Bridge

    channel = load_channel_factories()["my_channel"]({"name": "my"})
    bridge = Bridge(channel, lambda text: f"收到：{text}", access="open")

    channel.push("u", "做点事")
    replies = bridge.run_once()

    assert replies[0].accepted is True
    assert channel.sent == [("", "收到：做点事")]


def test_模块能重复导入(tmp_path: Path, monkeypatch) -> None:
    """同一个进程里装载两次不该炸（CLI 每次启动都会装载一次）。"""
    _write_plugin(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("AGENTS_DEV_CHANNEL_PLUGINS", "my_channel:build")
    assert load_channel_factories() == load_channel_factories()
