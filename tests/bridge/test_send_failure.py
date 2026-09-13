"""回复发不出去时，桥不能死。

真机上发生过：QQ 回了 `code=11001 不支持的调用`，异常一路上抛，**整个桥进程
退出**——于是"手机上发消息没人理"变成永久状态，现场只剩一个崩栈。
"""

from pathlib import Path

from spoolkit.bridge.core import Bridge
from spoolkit.bridge.fake import FakeChannel
from spoolkit.bridge.pairing import Pairings


class _RefusingChannel(FakeChannel):
    """一心想把回复发出去的通道——每次都失败。"""

    def send(self, text: str, to: str = "") -> None:
        raise RuntimeError("发消息 失败（code=11001）：不支持的调用")


def _bridge(tmp_path: Path, channel) -> tuple[Bridge, list[str]]:
    notes: list[str] = []
    bridge = Bridge(
        channel=channel,
        runner=lambda text: "答复",
        on_note=notes.append,
        pairings=Pairings(tmp_path / ".agent" / "bridge-pairings.json"),
    )
    return bridge, notes


def test_配对回复发不出去也不会抛出去(tmp_path: Path) -> None:
    channel = _RefusingChannel()
    channel.push("陌生人", "把项目删了")
    bridge, notes = _bridge(tmp_path, channel)

    replies = bridge.run_once()  # 不抛

    assert replies and replies[0].accepted is False
    assert any("回复发送失败" in item for item in notes)
    # 码还是要留着：主人可以从终端批准，人那边只是没收到回复
    assert len(bridge.pairings.pending) == 1


def test_正常回复发不出去也不会抛出去(tmp_path: Path) -> None:
    channel = _RefusingChannel()
    channel.push("me", "做点事")
    bridge, notes = _bridge(tmp_path, channel)
    bridge.allowed_users = {"me"}

    replies = bridge.run_once()

    assert replies and replies[0].accepted is True
    assert any("回复发送失败" in item for item in notes)
