"""`spool hub`：统一起网页壳，可选再拉 bridge。不启 llama / 隧道。"""

import argparse
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from spoolkit.cli.app import main
from spoolkit.cli.app import _add_web_args
from spoolkit.cli.commands.hub import (
    CHANNELS,
    bridge_child_argv,
    extra_from_serve_args,
    hub_command,
    stop_children,
)


def test_没给通道就不拼_bridge命令() -> None:
    args = argparse.Namespace(channel="", root=".")
    assert bridge_child_argv(args) == []


def test_web开关默认开_no_web关掉() -> None:
    parser = argparse.ArgumentParser()
    _add_web_args(parser)
    assert parser.parse_args([]).web is True
    assert parser.parse_args(["--no-web"]).web is False
    assert parser.parse_args(["--web"]).web is True


def test_run帮助也是默认开(capsys) -> None:
    try:
        main(["run", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "--no-web" in out
    assert "默认开" in out
    assert "不能生成代码" in out
    assert "不要用它当兜底" in out
    assert "真实任务不要走这条" in out


def test_给了通道就拼出_bridge子进程(tmp_path: Path) -> None:
    args = argparse.Namespace(
        channel="qqbot",
        root=str(tmp_path),
        bridge_proxy="socks5://127.0.0.1:1080",
        access="pairing",
        provider="llamacpp",
        model="",
        base_url="http://127.0.0.1:8080",
        script="",
        proxy="",
        policy="",
        scope="",
        web=True,
        web_allow=[],
        web_deny=[],
        allow_read=[],
        sandbox=False,
        session="cli",
        appid="",
        secret="",
        token="",
        allow_user=[],
        notify_disconnect=True,
    )
    argv = bridge_child_argv(args)
    assert "bridge" in argv
    assert "--channel" in argv and "qqbot" in argv
    assert "--bridge-proxy" in argv
    assert "--notify-disconnect" in argv
    assert str(tmp_path.resolve()) in argv or str(tmp_path) in argv
    assert "--from-channel" not in argv  # 那是 bridge 自己转给 run 的
    assert "--web" in argv
    assert "--no-web" not in argv


def test_hub把no_web转给bridge(tmp_path: Path) -> None:
    args = argparse.Namespace(
        channel="qqbot",
        root=str(tmp_path),
        bridge_proxy="",
        access="pairing",
        provider="",
        model="",
        base_url="",
        script="",
        proxy="",
        policy="",
        scope="",
        web=False,
        web_allow=[],
        web_deny=[],
        allow_read=[],
        sandbox=False,
        session="cli",
        appid="",
        secret="",
        token="",
        allow_user=[],
        notify_disconnect=False,
    )
    assert "--no-web" in bridge_child_argv(args)
    assert "--web" not in bridge_child_argv(args)


def test_网页壳默认转发联网() -> None:
    """QQ / 网页 / CLI 同一套能力：不显式关就带 --web。"""
    args = argparse.Namespace(
        provider="fake",
        model="",
        base_url="",
        script="",
        proxy="",
        policy="",
        scope="",
        allow_read=[],
        web_allow=[],
        web_deny=[],
    )
    extra = extra_from_serve_args(args)
    assert "--web" in extra
    assert "--no-web" not in extra


def test_网页壳关掉联网时转no_web() -> None:
    args = argparse.Namespace(
        provider="fake",
        model="",
        base_url="",
        script="",
        proxy="",
        policy="",
        scope="",
        web=False,
        allow_read=[],
        web_allow=[],
        web_deny=[],
    )
    extra = extra_from_serve_args(args)
    assert "--no-web" in extra
    assert "--web" not in extra


def test_网页壳参数会转给子进程() -> None:
    args = argparse.Namespace(
        provider="fake",
        model="",
        base_url="",
        script="",
        proxy="",
        policy="ask",
        scope="",
        web=True,
        web_allow=["example.com"],
        web_deny=[],
        allow_read=[],
    )
    extra = extra_from_serve_args(args)
    assert "--provider" in extra and "fake" in extra
    assert "--policy" in extra
    assert "--web" in extra
    assert "example.com" in extra


def test_未知通道拒绝启动(capsys) -> None:
    args = argparse.Namespace(
        channel="not-a-channel",
        root=".",
        host="127.0.0.1",
        port=8765,
        session="cli",
        provider="fake",
        model="",
        base_url="",
        script="",
        proxy="",
        policy="",
        scope="",
        web=False,
        web_allow=[],
        web_deny=[],
        allow_read=[],
        bridge_proxy="",
        access="pairing",
        sandbox=False,
        appid="",
        secret="",
        token="",
        allow_user=[],
    )
    assert hub_command(args) == 2
    err = capsys.readouterr().err
    assert "未知通道" in err
    for name in CHANNELS:
        assert name in err


def test_无通道只起网页壳(monkeypatch, tmp_path: Path) -> None:
    seen: dict = {}

    def fake_serve(root, **kwargs):
        seen["root"] = root
        seen["kwargs"] = kwargs

    monkeypatch.setattr("spoolkit.cli.commands.hub.serve", fake_serve)
    monkeypatch.setattr(
        "spoolkit.cli.commands.hub.subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该拉子进程")),
    )
    args = argparse.Namespace(
        channel="",
        root=str(tmp_path),
        host="127.0.0.1",
        port=0,
        session="cli",
        provider="fake",
        model="",
        base_url="",
        script="",
        proxy="",
        policy="",
        scope="",
        web=False,
        web_allow=[],
        web_deny=[],
        allow_read=[],
        bridge_proxy="",
        access="pairing",
        sandbox=False,
        appid="",
        secret="",
        token="",
        allow_user=[],
    )
    assert hub_command(args) == 0
    assert seen["root"] == tmp_path.resolve()
    assert seen["kwargs"]["extra_args"] == extra_from_serve_args(args)


def test_有通道会拉_bridge_退出时只杀自己的孩子(monkeypatch, tmp_path: Path) -> None:
    proc = MagicMock()
    proc.poll.return_value = None
    pops: list[list[str]] = []

    def fake_popen(argv, **_kwargs):
        pops.append(list(argv))
        return proc

    monkeypatch.setattr("spoolkit.cli.commands.hub.subprocess.Popen", fake_popen)
    monkeypatch.setattr("spoolkit.cli.commands.hub.serve", lambda *a, **k: None)
    args = argparse.Namespace(
        channel="fake",
        root=str(tmp_path),
        host="127.0.0.1",
        port=0,
        session="cli",
        provider="fake",
        model="",
        base_url="",
        script="",
        proxy="",
        policy="",
        scope="",
        web=False,
        web_allow=[],
        web_deny=[],
        allow_read=[],
        bridge_proxy="",
        access="pairing",
        sandbox=False,
        appid="",
        secret="",
        token="",
        allow_user=[],
    )
    assert hub_command(args) == 0
    assert pops and "bridge" in pops[0]
    proc.terminate.assert_called_once()


def test_停子进程时杀不掉就强制结束() -> None:
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="x", timeout=1), 0]
    stop_children([proc])
    proc.kill.assert_called_once()


def test_入口挂上了hub(capsys) -> None:
    try:
        main(["hub", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "--channel" in out
    assert "网页壳" in out
    assert "--no-web" in out
    assert "--web" in out


def test_总帮助提到hub(capsys) -> None:
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "hub" in capsys.readouterr().out
