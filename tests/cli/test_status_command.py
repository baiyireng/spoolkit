"""`spool status`：本机服务现状 + 起停命令；只打本机、失败不冒栈、密钥绝不落屏。

**测试里不让真服务在跑**：所有探测点要么是本次起在临时端口上的桩服务
（`_serve`，`finally` 收尾），要么是一个刚被关掉的端口。退出码只按"关键项 +
状态是不是 up"判，所以不依赖这台机器上恰好有什么在监听。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from spoolkit.cli.app import main
from spoolkit.cli.commands.status import status_command
from spoolkit.status import Report, Row, render_human

SECRET = "SECRET-VALUE-DO-NOT-PRINT-1234"


# --- 小工具 -----------------------------------------------------------------


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    """把三层配置全指到临时目录：配置、机器级状态、用户级 .env。

    环境变量也要清：开发机上真设了 `SPOOLKIT_PROVIDER` 的话，它压得住配置文件，
    症状是"这条断言在别人机器上红"。
    """
    config = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(config))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(tmp_path / ".env"))
    for name in (
        "SPOOLKIT_PROVIDER",
        "AGENTS_DEV_PROVIDER",
        "SPOOLKIT_BASE_URL",
        "AGENTS_DEV_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    return config


class _Stub(BaseHTTPRequestHandler):
    """一个只答一种响应的本机桩服务。"""

    code = 200
    payload = b"{}"

    def do_GET(self) -> None:  # noqa: N802 - http.server 的约定
        self.send_response(self.code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args) -> None:  # 别把访问日志打到测试输出里
        pass


def _serve(code: int = 200, payload: dict | None = None) -> tuple[int, object]:
    """起一个桩服务，返回（端口, 收尾函数）。收尾必须放在 `finally` 里。"""
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    handler = type("_Handler", (_Stub,), {"code": code, "payload": body})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()

    return httpd.server_port, stop


def _closed_port() -> int:
    """拿一个刚关掉的端口号（本机没人听）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(argv: list[str]) -> int:
    return main(["status", *argv])


def _flatten(text: str) -> str:
    """Join wrapped field lines so long paths can be asserted with `in`."""
    import re

    # Drop newlines + any indent (card field wraps use variable leading spaces).
    return re.sub(r"\n\s*", "", text)


def _payload(out: str) -> dict:
    return json.loads(out)


# --- 表格与退出码 -----------------------------------------------------------


def test_全好时退0并给出起停命令(tmp_path, monkeypatch, capsys) -> None:
    """provider=llamacpp ＋ 模型桩 ＋ 一条配方桩：关键全在 → 0。"""
    config = _isolate(tmp_path, monkeypatch)
    port, stop = _serve(200)
    try:
        _write(
            config,
            f'provider = "llamacpp"\nbase_url = "http://127.0.0.1:{port}"\n'
            "[[service]]\n"
            'name = "llama"\n'
            f'command = ["llama-server", "--port", "{port}"]\n'
            f'health = "http://127.0.0.1:{port}/props"\n',
        )
        code = _run(["--timeout", "0.5", "--web-port", str(_closed_port())])
    finally:
        stop()
    out = capsys.readouterr().out
    assert code == 0, out
    # Card layout: each row exposes target / start / stop fields.
    assert "start" in out and "stop" in out and "target" in out
    assert "spool service start llama" in out
    assert "spool service stop llama" in out
    assert "critical ok" in out
    assert "up" in out
    assert "none" in out
    assert "MCP" in out
    assert "* critical" in out


def test_探活失败的原话补一句人话() -> None:
    """操作系统的英文原话（timed out / refused）照抄给用户看等于让他自己翻译。"""
    from spoolkit.status import _readable_probe

    timed_out = _readable_probe("timed out")
    assert "timed out" in timed_out or "no accept" in timed_out
    assert "timed out" in timed_out  # 原话留着：排查时它有用
    assert "nothing listening" in _readable_probe("Connection refused")
    assert _readable_probe("HTTP 500") == "HTTP 500"
    assert _readable_probe("[WinError 10061] 由于目标计算机积极拒绝") != ""


def test_关键项没起时非零(tmp_path, monkeypatch, capsys) -> None:
    """provider=llamacpp 但模型答 500 → 本机模型是关键且不 up → 1。"""
    config = _isolate(tmp_path, monkeypatch)
    port, stop = _serve(500)
    try:
        _write(
            config,
            f'provider = "llamacpp"\nbase_url = "http://127.0.0.1:{port}"\n',
        )
        code = _run(["--timeout", "0.5", "--json"])
    finally:
        stop()
    payload = _payload(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["critical_down"] == ["model"]
    model = [row for row in payload["rows"] if row["name"] == "model"][0]
    assert model["state"] == "down"
    assert "HTTP 500" in model["detail"]


def test_没配过供应商也算关键(tmp_path, monkeypatch, capsys) -> None:
    """装机第一天：没有可用模型（内置默认 fake）→ 非零，并说得出怎么配。"""
    _isolate(tmp_path, monkeypatch)
    code = _run(["--timeout", "0.2", "--json"])
    payload = _payload(capsys.readouterr().out)
    assert code == 1
    assert payload["critical_down"] == ["provider"]
    provider = [row for row in payload["rows"] if row["name"] == "provider"][0]
    assert provider["critical"] is True
    assert "spool config --set provider=llamacpp" in provider["start"]


def test_云端供应商时本机模型不判(tmp_path, monkeypatch, capsys) -> None:
    """provider=gemini：本机模型探不通也不算关键 → 0（"全好"的另一种形状）。

    ⚠️ **为什么要把 `base_url` 指到一个关着的端口**：这条原来只写 `provider = "gemini"`，
    于是它断言的那句"不影响退出码"**取决于本机 8080 上恰好没有模型服务**——机器上 llama 一起来
    （本机真会这样），模型那一行就变成"正常"，这句话不再出现，用例就红了。**测试不该看环境的脸色**：
    这里显式把地址指到一个按构造就没人监听的端口（1 号端口我们永远不会占用），
    使"模型探不通"成为**用例自己造出来的前提**，而不是碰运气。
    """
    config = _isolate(tmp_path, monkeypatch)
    _write(config, 'provider = "gemini"\nbase_url = "http://127.0.0.1:1"\n')
    code = _run(["--timeout", "0.2", "--json"])
    payload = _payload(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    model = [row for row in payload["rows"] if row["name"] == "model"][0]
    assert model["critical"] is False
    assert "ignored for exit code" in model["detail"]


def test_模型起来了但慢是单独一档(tmp_path, monkeypatch, capsys) -> None:
    """503（加载中）不能报成"没起"，也不能报成"通"——那是"起来了但慢"。"""
    config = _isolate(tmp_path, monkeypatch)
    port, stop = _serve(503)
    try:
        _write(
            config,
            f'provider = "llamacpp"\nbase_url = "http://127.0.0.1:{port}"\n',
        )
        code = _run(["--timeout", "0.5", "--json"])
    finally:
        stop()
    payload = _payload(capsys.readouterr().out)
    assert code == 1
    model = [row for row in payload["rows"] if row["name"] == "model"][0]
    assert model["state"] == "slow"
    assert "503" in model["detail"]


def test_配方没写command就不算关键(tmp_path, monkeypatch, capsys) -> None:
    """只探不启的配方（`command` 空）不通时只报告，不改退出码。"""
    config = _isolate(tmp_path, monkeypatch)
    _write(
        config,
        'provider = "gemini"\n'
        "[[service]]\n"
        'name = "tunnel"\n'
        f'health = "tcp:127.0.0.1:{_closed_port()}"\n',
    )
    code = _run(["--timeout", "0.2", "--json"])
    payload = _payload(capsys.readouterr().out)
    assert code == 0
    row = [item for item in payload["rows"] if item["name"] == "service:tunnel"][0]
    assert row["critical"] is False
    assert row["state"] != "up"
    assert "probe-only" in row["start"]


def test_写了command但看不出状态也算没就绪(tmp_path, monkeypatch, capsys) -> None:
    """没有探活地址、也没 pid：说"看不出"，且按关键处理（你声明了要拉起它）。"""
    config = _isolate(tmp_path, monkeypatch)
    _write(
        config,
        'provider = "gemini"\n'
        "[[service]]\n"
        'name = "tunnel"\n'
        'command = ["ssh", "-N", "-D", "1080", "跳板"]\n',
    )
    code = _run(["--timeout", "0.2", "--json"])
    payload = _payload(capsys.readouterr().out)
    assert code == 1
    row = [item for item in payload["rows"] if item["name"] == "service:tunnel"][0]
    assert row["state"] == "unknown"
    assert row["critical"] is True


def test_配方行说清是不是本命令拉起的(tmp_path, monkeypatch, capsys) -> None:
    """`service:<配方>` 与 `service list` 同一套口径：**pid 记录是判据**，探针报状态。

    端口上有东西（多半是用户自己手起的 llama / 隧道）→ 状态是 `up`（那个地址确实
    在服务），但说明里必须点清"非本命令启动"；写了自己的 pid 记录之后才叫
    "本命令启动的进程存活"。两边都不许把别人占着的端口说成我们的。
    """
    from spoolkit import services

    config = _isolate(tmp_path, monkeypatch)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = int(sock.getsockname()[1])
    try:
        _write(
            config,
            'provider = "gemini"\n'
            "[[service]]\n"
            'name = "tunnel"\n'
            'command = ["ssh", "-N", "-D", "1080", "跳板"]\n'
            f'health = "tcp:127.0.0.1:{port}"\n',
        )
        code = _run(["--timeout", "0.2", "--json"])
        payload = _payload(capsys.readouterr().out)
        row = [item for item in payload["rows"] if item["name"] == "service:tunnel"][0]
        assert code == 0
        assert row["state"] == "up"
        assert "not started by us" in row["detail"] or "not ours" in row["detail"], row["detail"]
        assert "owned process alive" not in row["detail"]

        # 记下本命令拉起的 pid（写活着的本进程，不用收尾）之后，口径才变成"在跑"。
        services.write_pid("tunnel", os.getpid())
        code = _run(["--timeout", "0.2", "--json"])
        payload = _payload(capsys.readouterr().out)
        row = [item for item in payload["rows"] if item["name"] == "service:tunnel"][0]
        assert code == 0
        assert row["state"] == "up"
        assert "owned process alive" in row["detail"], row["detail"]
        assert f"pid {os.getpid()}" in row["detail"]
        # 探针结果已经在上面那句里，不许再追加一遍（同一句话印两遍）。
        assert row["detail"].count("port open") == 1, row["detail"]
    finally:
        sock.close()


# --- 网页壳 -----------------------------------------------------------------


def test_网页壳在跑时报端口与只读性(tmp_path, monkeypatch, capsys) -> None:
    _isolate(tmp_path, monkeypatch)
    snapshot = {
        "readonly": True,
        "host": "0.0.0.0",
        "target": {"root": "D:\\某个工作区", "session": "web-1"},
        "workspaces": [],
    }
    port, stop = _serve(200, snapshot)
    try:
        code = _run(["--timeout", "0.5", "--json", "--web-port", str(port)])
    finally:
        stop()
    payload = _payload(capsys.readouterr().out)
    assert code in (0, 1)  # 网页壳不影响退出码，这里只看它怎么报
    row = [item for item in payload["rows"] if item["name"] == f"web:{port}"][0]
    assert row["state"] == "up"
    assert "readonly" in row["detail"]
    assert "0.0.0.0" in row["detail"]
    assert "D:\\某个工作区" in row["detail"]
    assert f"spool web --port {port}" == row["start"]


def test_网页壳可写时说可写(tmp_path, monkeypatch, capsys) -> None:
    _isolate(tmp_path, monkeypatch)
    port, stop = _serve(
        200, {"readonly": False, "host": "127.0.0.1", "target": {"root": "", "session": ""}}
    )
    try:
        _run(["--timeout", "0.5", "--json", "--web-port", str(port)])
    finally:
        stop()
    payload = _payload(capsys.readouterr().out)
    row = [item for item in payload["rows"] if item["name"] == f"web:{port}"][0]
    assert row["state"] == "up"
    assert "writable" in row["detail"]
    assert "(none)" in row["detail"]


def test_端口上有别的东西就说不是网页壳(tmp_path, monkeypatch, capsys) -> None:
    _isolate(tmp_path, monkeypatch)
    port, stop = _serve(200, {"hello": "world"})
    try:
        _run(["--timeout", "0.5", "--json", "--web-port", str(port)])
    finally:
        stop()
    payload = _payload(capsys.readouterr().out)
    row = [item for item in payload["rows"] if item["name"] == f"web:{port}"][0]
    assert row["state"] == "down"
    assert "shape" in row["detail"]


def test_没人听的端口说没起(tmp_path, monkeypatch, capsys) -> None:
    _isolate(tmp_path, monkeypatch)
    port = _closed_port()
    _run(["--timeout", "0.2", "--json", "--web-port", str(port)])
    payload = _payload(capsys.readouterr().out)
    row = [item for item in payload["rows"] if item["name"] == f"web:{port}"][0]
    assert row["state"] in {"down", "slow"}
    assert row["state"] != "up"


# --- 通道 / MCP / 工作区 / 密钥 ---------------------------------------------


def test_通道栏只给凭据来源不给值(tmp_path, monkeypatch, capsys) -> None:
    """绝不许打印密钥值；但"从哪个文件来的"必须说得出来。"""
    config = _isolate(tmp_path, monkeypatch)
    _write(
        config,
        'provider = "gemini"\n'
        "[channel.qqbot]\n"
        'proxy = "socks5://127.0.0.1:1080"\n',
    )
    env_file = tmp_path / ".env"
    _write(env_file, f"QQ_AppID=102xxxxxx\nQQ_AppSecret={SECRET}\n")
    code = _run(["--timeout", "0.2"])
    captured = capsys.readouterr()
    both = _flatten(captured.out + captured.err)
    assert SECRET not in both
    assert "102xxxxxx" not in both
    # 来源说得出：文件路径 + 键名（键名不是值）
    assert str(env_file) in both
    assert "QQ_AppSecret" in both
    assert "bridge:qqbot" in captured.out or "bridge qqbot" in captured.out
    row_line = [
        line for line in _flatten(captured.out).splitlines() if "bridge qqbot" in line
    ][0]
    assert "unknown" in row_line
    assert code == 0  # 通道不影响退出码


def test_通道缺凭据时说清往哪写(tmp_path, monkeypatch, capsys) -> None:
    config = _isolate(tmp_path, monkeypatch)
    _write(config, 'provider = "gemini"\n[channel.qqbot]\n')
    code = _run(["--timeout", "0.2", "--json"])
    payload = _payload(capsys.readouterr().out)
    assert code == 0
    row = [item for item in payload["rows"] if item["name"] == "bridge:qqbot"][0]
    assert row["state"] == "down"
    assert "missing credentials" in row["detail"]
    assert str(tmp_path / ".env") in row["detail"]


def test_用户级env只印键数不印值(tmp_path, monkeypatch, capsys) -> None:
    _isolate(tmp_path, monkeypatch)
    _write(tmp_path / ".env", f"QQ_AppSecret={SECRET}\nTAVILY_API_KEY={SECRET}\n")
    _run(["--timeout", "0.2"])
    captured = capsys.readouterr()
    both = captured.out + captured.err
    assert SECRET not in both
    assert "2 keys" in both


def test_mcp没登记时说不适用(tmp_path, monkeypatch, capsys) -> None:
    _isolate(tmp_path, monkeypatch)
    _run(["--timeout", "0.2", "--json"])
    payload = _payload(capsys.readouterr().out)
    row = [item for item in payload["rows"] if item["name"] == "mcp"][0]
    assert row["state"] == "none"
    assert "(none)" in row["target"]


def test_mcp登记了但默认不真连(tmp_path, monkeypatch, capsys) -> None:
    config = _isolate(tmp_path, monkeypatch)
    _write(
        config,
        'provider = "gemini"\n'
        "[[mcp]]\n"
        'name = "filesystem"\n'
        'command = "npx"\n'
        'args = ["-y", "@modelcontextprotocol/server-filesystem"]\n',
    )
    _run(["--timeout", "0.2", "--json"])
    payload = _payload(capsys.readouterr().out)
    row = [item for item in payload["rows"] if item["name"] == "mcp:filesystem"][0]
    assert row["state"] == "unknown"
    assert "--probe" in row["detail"]


def test_probe连不上也不冒栈(tmp_path, monkeypatch, capsys) -> None:
    """`--probe` 真连一个不存在的命令：一句人话 ＋ 非零？不，它不是关键 → 0。"""
    config = _isolate(tmp_path, monkeypatch)
    _write(
        config,
        'provider = "gemini"\n'
        "[[mcp]]\n"
        'name = "broken"\n'
        'command = "spoolkit-no-such-binary-xyz"\n',
    )
    code = _run(["--timeout", "0.2", "--probe", "--mcp-timeout", "2", "--json"])
    captured = capsys.readouterr()
    payload = _payload(captured.out)
    row = [item for item in payload["rows"] if item["name"] == "mcp:broken"][0]
    assert row["state"] == "down"
    assert "unreachable" in row["detail"]
    assert "Traceback" not in captured.err
    assert code == 0


def test_工作区与指针一起报(tmp_path, monkeypatch, capsys) -> None:
    _isolate(tmp_path, monkeypatch)
    book = tmp_path / "proj"
    (book / ".agent").mkdir(parents=True)
    state = tmp_path / "state"
    _write(state / "workspaces.json", json.dumps({"workspaces": [str(book)]}))
    _write(
        state / "clients.json",
        json.dumps(
            {"clients": {"web": {"workspace": str(book), "session": "web-1"}}}
        ),
    )
    _run(["--timeout", "0.2", "--json"])
    payload = _payload(capsys.readouterr().out)
    row = [item for item in payload["rows"] if item["name"] == "workspace"][0]
    assert row["state"] == "up"
    assert "1 registered" in row["target"]
    assert "web" in row["detail"]
    assert str(book) in row["detail"]
    workspace_env = [
        item for item in payload["rows"] if item["name"] == "env:workspace"
    ]
    assert workspace_env, "有工作区时就该报它的 .env 落点"
    assert str(book / ".env") in workspace_env[0]["detail"]


def test_json字段名稳定(tmp_path, monkeypatch, capsys) -> None:
    _isolate(tmp_path, monkeypatch)
    _run(["--timeout", "0.2", "--json"])
    payload = _payload(capsys.readouterr().out)
    assert set(payload) == {
        "ok",
        "critical_down",
        "provider",
        "timeout_s",
        "elapsed_ms",
        "rows",
    }
    assert set(payload["provider"]) == {"value", "source"}
    assert payload["rows"], "至少要有几行"
    for row in payload["rows"]:
        assert set(row) == {
            "name",
            "label",
            "state",
            "critical",
            "target",
            "detail",
            "start",
            "stop",
        }
        assert row["state"] in {"up", "slow", "down", "unknown", "none"}


# --- 用法与入口 -------------------------------------------------------------


def test_配置解析失败给一句人话并退2(tmp_path, monkeypatch, capsys) -> None:
    config = _isolate(tmp_path, monkeypatch)
    _write(config, 'provider = "D:\\tools\\x.exe"\n')  # 反斜杠没转义，整份读不出来
    code = _run(["--timeout", "0.2"])
    captured = capsys.readouterr()
    assert code == 2
    assert "parse error" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out.strip() == ""  # 表全是假象，一个字都不打


def test_不合法的超时与端口退2(tmp_path, monkeypatch, capsys) -> None:
    _isolate(tmp_path, monkeypatch)
    assert _run(["--timeout", "0"]) == 2
    assert "--timeout must be > 0" in capsys.readouterr().err
    assert _run(["--web-port", "70000"]) == 2
    assert "--web-port must be in 1..65535" in capsys.readouterr().err


def test_入口挂上了status(capsys) -> None:
    try:
        main(["status", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "--json" in out
    assert "--web-port" in out
    assert "loopback" in out or "status table" in out or "model" in out


def test_命令函数直接调用也行(tmp_path, monkeypatch, capsys) -> None:
    """不经 argparse 也要能跑：参数缺省时按默认值走。"""
    _isolate(tmp_path, monkeypatch)
    args = argparse.Namespace(json=True)
    code = status_command(args)
    payload = _payload(capsys.readouterr().out)
    assert code == 1
    assert payload["critical_down"] == ["provider"]


def test_没有别名只有status(tmp_path, monkeypatch, capsys) -> None:
    """别名/前缀是"另一个入口"，会让两处慢慢走散——只留 `spool status`。"""
    _isolate(tmp_path, monkeypatch)
    try:
        _run([])
    except SystemExit as exc:  # pragma: no cover - 上面那条就是给 argparse 用的
        raise AssertionError(f"`status` 不该需要子参数：{exc}") from exc
    for alias in ("state", "st", "服务"):
        try:
            main([alias])
        except SystemExit as exc:
            assert exc.code == 2, alias
            continue
        raise AssertionError(f"不该有 {alias} 这个入口")


def test_render_human_embeds_state_markup() -> None:
    row = Row(
        name="model",
        label="local model",
        state="up",
        target="http://127.0.0.1:8080",
        detail="HTTP 200",
        start="spool service start x",
        stop="spool service stop x",
        critical=True,
    )
    text = render_human(
        Report(
            rows=(row,),
            provider="llamacpp",
            provider_source="test",
            timeout=0.5,
            elapsed_ms=1,
        )
    )
    assert "[state.up]" in text
    assert "[accent]" in text
    assert "[muted]target[/]" in text or "[muted]target" in text
