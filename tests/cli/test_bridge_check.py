"""`spool bridge --check` 必须检查**真正要跑的那条路**。

踩过的坑：`--check` 建通道时没把 `--bridge-proxy` 传下去，于是它一直在测直连。
而 QQ 平台有 IP 白名单，直连和"借云主机出去"是两条不同的路——症状是
**检查通过、真跑失败**，也就是检查工具本身在骗人。
"""

import argparse
from pathlib import Path

import pytest

from spoolkit.cli.commands.bridge import bridge_command


def _args(root: Path, **over) -> argparse.Namespace:
    base = {
        "root": str(root),
        "channel": "qqbot",
        "check": True,
        "approve": "",
        "sandbox": False,
        "bridge_proxy": "",
        "token": "",
        "appid": "",
        "secret": "",
    }
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture()
def fake_channel(monkeypatch):
    """把通道换成记账的假货：只关心"传给它什么"。"""
    seen: dict = {}

    class _FakeQQ:
        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)

        def access_token(self) -> str:
            return "T"

        def gateway_url(self) -> str:
            return "wss://example.invalid/websocket"

        def close(self) -> None:
            pass

    monkeypatch.setattr("spoolkit.bridge.qqbot.QQBotChannel", _FakeQQ)
    return seen


def _workspace_with_credentials(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text(
        "QQ_AppID=102000000\nQQ_AppSecret=secret-value\n", encoding="utf-8"
    )
    return tmp_path


def test_check_把代理传下去(tmp_path, fake_channel, capsys) -> None:
    root = _workspace_with_credentials(tmp_path)
    code = bridge_command(_args(root, bridge_proxy="socks5://127.0.0.1:1080"))

    assert code == 0
    assert fake_channel["proxy"] == "socks5://127.0.0.1:1080"
    assert "socks5://127.0.0.1:1080" in capsys.readouterr().out  # 报告里要说出来


def test_check_没给代理时不传(tmp_path, fake_channel) -> None:
    root = _workspace_with_credentials(tmp_path)
    assert bridge_command(_args(root)) == 0
    assert fake_channel["proxy"] == ""
