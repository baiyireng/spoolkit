import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.web.runner import Runner
from spoolkit.web.server import build_server


@pytest.fixture
def server(tmp_path: Path):
    runner = Runner(
        tmp_path,
        session="t",
        extra_args=["--provider", "fake", "--script", ""],
    )
    httpd = build_server(runner, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_首页返回HTML(server: str) -> None:
    status, body = _get(server + "/")
    assert status == 200
    assert "<html" in body.lower()


def test_状态接口返回快照(server: str) -> None:
    status, body = _get(server + "/state")
    assert status == 200
    assert json.loads(body)["running"] is False


def test_发起运行返回202(server: str) -> None:
    assert _post(server + "/run", {"goal": "做点事"})[0] == 202


def test_缺少目标返回400(server: str) -> None:
    assert _post(server + "/run", {})[0] == 400
    assert _post(server + "/run", {"goal": "   "})[0] == 400


def test_重复发起返回409(server: str) -> None:
    assert _post(server + "/run", {"goal": "第一次"})[0] == 202
    assert _post(server + "/run", {"goal": "第二次"})[0] == 409


def test_没有待确认时确认返回409(server: str) -> None:
    assert _post(server + "/confirm", {"apply": True})[0] == 409


def test_未知路径返回404(server: str) -> None:
    # 路径用 ASCII：urllib 不会替我们把中文路径做百分号编码
    assert _get(server + "/nope")[0] == 404


def test_事件流首帧是状态快照(server: str) -> None:
    """重连时不补发事件，所以每次连接都要先把完整状态给过去。"""
    with urllib.request.urlopen(server + "/events", timeout=10) as response:
        assert response.status == 200
        assert "text/event-stream" in response.headers["Content-Type"]
        # 读一整行而不是 read(N)：SSE 流不会结束，read(N) 会一直等到攒够 N 字节
        first = response.readline().decode("utf-8")
    assert "state" in first
    assert "running" in first
