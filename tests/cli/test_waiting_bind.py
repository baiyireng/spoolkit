import argparse
import io
import json
from pathlib import Path

from spoolkit.cli.events import EventWriter
from spoolkit.cli.runtime import bind_model_wait, provider_gateway
from spoolkit.llm.gateway import WaitingGateway, wait_line
from spoolkit.web.protocol import WAIT, parse_line


def test_散文监听器写stderr(capsys) -> None:
    gateway = WaitingGateway(object())
    args = argparse.Namespace(events=False)
    bind_model_wait(gateway, args)
    gateway.on_wait(15, 180.0)
    err = capsys.readouterr().err
    assert wait_line(15, 180.0) in err


def test_events监听器发wait_JSON() -> None:
    buf = io.StringIO()
    gateway = WaitingGateway(object())
    bind_model_wait(
        gateway, argparse.Namespace(events=True), writer=EventWriter(buf)
    )
    gateway.on_wait(15, 491.0)
    parsed = parse_line(buf.getvalue())
    assert parsed is not None
    assert parsed.type == WAIT
    assert parsed.data == {"elapsed": 15, "limit": 491}


def test_events没有limit时省略键() -> None:
    buf = io.StringIO()
    gateway = WaitingGateway(object())
    bind_model_wait(
        gateway, argparse.Namespace(events=True), writer=EventWriter(buf)
    )
    gateway.on_wait(15, None)
    parsed = parse_line(buf.getvalue())
    assert parsed is not None
    assert "limit" not in parsed.data


def test_provider_gateway包WaitingGateway(tmp_path: Path) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps(["{\"done\":true,\"final\":\"x\"}"]), encoding="utf-8")
    args = argparse.Namespace(
        provider="fake",
        script=str(script),
        model="",
        base_url="",
        proxy="",
        engine="",
    )
    gateway = provider_gateway(args, tmp_path)
    assert isinstance(gateway, WaitingGateway)


def test_采样参数从命令行进ProviderConfig(tmp_path: Path, monkeypatch) -> None:
    """`--temperature/--seed` 必须真的走到网关构造里，而不只是记在跑分记录里。

    默认值也必须保持「温度 0、不给种子」：历史读数全是在那种设置下取的。
    """
    from spoolkit.cli import app as cli_app
    from spoolkit.cli import runtime as cli_runtime

    parser = argparse.ArgumentParser(prog="spool")
    sub = parser.add_subparsers(dest="command")
    cli_app._add_run_command(sub)

    seen: dict = {}

    class Stub:
        def context_window(self):
            return 8192

        def close(self):
            pass

    def fake_load(name, config):
        seen["name"] = name
        seen["config"] = config
        return Stub()

    monkeypatch.setattr(cli_runtime, "load_gateway", fake_load)

    args = parser.parse_args(
        [
            "run",
            "--goal",
            "g",
            "--provider",
            "llamacpp",
            "--temperature",
            "0.4",
            "--seed",
            "7",
        ]
    )
    assert cli_runtime.provider_gateway(args, tmp_path) is not None
    assert seen["name"] == "llamacpp"
    assert seen["config"].temperature == 0.4
    assert seen["config"].seed == 7

    plain = parser.parse_args(["run", "--goal", "g", "--provider", "llamacpp"])
    assert cli_runtime.provider_gateway(plain, tmp_path) is not None
    assert seen["config"].temperature == 0.0
    assert seen["config"].seed is None
