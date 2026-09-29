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
