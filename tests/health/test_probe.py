"""模型探活：给人看的红绿，不进模型上下文。"""

import socket

from spoolkit.health import describe_change, probe_from_extra, probe_model, probe_spec


def test_假模型算通() -> None:
    result = probe_model(provider="fake")
    assert result["ok"] is True
    assert result["name"] == "model"
    assert "假" in result["detail"]


def test_gemini_不探本机端口() -> None:
    result = probe_model(provider="gemini")
    assert result["ok"] is True
    assert "Gemini" in result["detail"] or "云端" in result["detail"]


def test_llamacpp_连不上就报断() -> None:
    result = probe_model(
        provider="llamacpp",
        base_url="http://127.0.0.1:1",
        timeout=0.3,
    )
    assert result["ok"] is False
    assert "127.0.0.1:1" in result["target"]
    assert result["detail"]


def test_子进程参数里的假模型也不打端口() -> None:
    result = probe_from_extra(["--provider", "fake", "--script", ""])
    assert result["ok"] is True
    assert "假" in result["detail"]


def test_通断变化会写成一行() -> None:
    down = {
        "ok": False,
        "target": "http://127.0.0.1:8080",
        "detail": "拒绝连接",
    }
    line = describe_change({"ok": True, "target": down["target"]}, down)
    assert line is not None
    assert "不通" in line
    assert "8080" in line
    assert describe_change(down, down) is None


def test_tcp探活通断() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    spec = f"tcp:127.0.0.1:{port}"
    try:
        ok, target, detail = probe_spec(spec, timeout=0.3)
        assert ok is True
        assert str(port) in target
        assert detail
    finally:
        server.close()
    ok, target, detail = probe_spec(spec, timeout=0.3, cached=False)
    assert ok is False
    assert str(port) in target
    assert detail
