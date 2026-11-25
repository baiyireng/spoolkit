"""会话的「移除 / 恢复」：`POST /session/delete` 与 `POST /session/restore`。

这里量的是**这一批新增的那条路**：

1. 「移除」= 归档——从列表里消失，但 `.agent/sessions/<id>/` 与 `memory.db`
   一个字节都不动，随时能放回来（页面上的"已移除"那一段就是出口）；
2. **正在跑的不许移除**（任务的记录不该在它还在跑的时候从列表里消失）；
3. 移掉的正好是**当前会话**时，目标要退到下一条还在的会话上——不然指针会指着
   一条列表里没有的会话（"我在一条已移除的会话里"）；
4. `--root` 钉住的壳不许移除**当前**会话（它只有一个目标，没有可退的地方）；
5. 非回环绑定一律 **403**：与 `/run`、`/confirm` 走**同一张** `WRITE_PATHS` 表——
   新的写端点漏出安全边界，是这一批最容易犯的错。

不起长驻进程：服务绑临时端口（`port=0`）在后台线程里 `serve_forever`，
`finally` 里关掉；"正在跑"那一条用 `_Chatty` 起一个**自己会退出**（被 stop 收掉）
的子进程，与 `test_runner_registry.py` 同一个做法。
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.kernel import pointers as kernel_pointers
from spoolkit.kernel import sessions as kernel_sessions
from spoolkit.kernel.target import Target
from spoolkit.web.registry import RunnerRegistry
from spoolkit.web.runner import Runner
from spoolkit.web.server import WebState, build_server, build_state

#: 一直吐事件、直到被 `stop()` 收掉的子进程（"正在跑"的那一条）。
CHATTY = (
    "import json, sys, time\n"
    "while True:\n"
    "    print(json.dumps({'type': 'note', 'text': '还在跑'}), flush=True)\n"
    "    time.sleep(0.05)\n"
)


class _Chatty(Runner):
    """真起子进程的 Runner（`command()` 换成一段死循环脚本）。"""

    def command(self, goal: str) -> list[str]:
        return [sys.executable, "-c", CHATTY]


@pytest.fixture
def start_server():
    made: list = []

    def start(state) -> str:
        httpd = build_server(state, "127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        made.append((httpd, thread, state))
        return f"http://127.0.0.1:{httpd.server_port}"

    yield start
    for httpd, thread, state in made:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        state.close()


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


def _state(url: str) -> dict:
    status, body = _get(url + "/state")
    assert status == 200, body
    return json.loads(body)


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _pinned(root: Path, session: str, *, pinned: bool = False) -> WebState:
    return build_state(
        None,
        pinned=pinned,
        target=Target(
            root=root,
            session=session,
            workspace_source="explicit",
            session_source="explicit",
        ),
    )


# --- 移除 = 归档（可恢复） ---


def test_移除一条会话从列表里消失但数据还在(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    keep = kernel_sessions.create_session(root, title="留着")
    gone = kernel_sessions.create_session(root, title="要移除的")
    state = _pinned(root, keep)
    url = start_server(state)

    status, body = _post(url + "/session/delete", {"session": gone})

    assert status == 200, body
    payload = json.loads(body)
    assert [row["id"] for row in payload["sessions"]] == [keep]
    assert [row["id"] for row in payload["sessions_removed"]] == [gone], (
        "移除之后没有出现在「已移除」里：那就不是可恢复的移除"
    )
    # 数据一个字都没动：归档只是 meta.json 上的一个标记。
    assert kernel_sessions.meta_path(root, gone).is_file()
    assert (root / ".agent" / "sessions" / gone).is_dir()
    listed = [item.id for item in kernel_sessions.list_sessions(root, include_archived=True)]
    assert gone in listed
    # 端点自己的响应与随后 /state 的口径要一致。
    assert [row["id"] for row in _state(url)["sessions"]] == [keep]


def test_恢复把移除的那条放回列表(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    keep = kernel_sessions.create_session(root, title="留着")
    gone = kernel_sessions.create_session(root, title="要移除的")
    state = _pinned(root, keep)
    url = start_server(state)
    assert _post(url + "/session/delete", {"session": gone})[0] == 200

    status, body = _post(url + "/session/restore", {"session": gone})

    assert status == 200, body
    payload = json.loads(body)
    assert gone in [row["id"] for row in payload["sessions"]]
    assert payload["sessions_removed"] == []


def test_状态接口多给了已移除那一段(start_server, tmp_path: Path) -> None:
    """`sessions` 的口径不许动（它一直只列没归档的），新增的是另一个字段。"""
    root = _ws(tmp_path, "a")
    keep = kernel_sessions.create_session(root, title="留着")
    gone = kernel_sessions.create_session(root, title="先归档")
    kernel_sessions.archive_session(root, gone)
    state = _pinned(root, keep)
    url = start_server(state)

    payload = _state(url)

    assert payload["sessions_removed"] == [{"id": gone, "title": "先归档"}]
    assert [row["id"] for row in payload["sessions"]] == [keep]
    assert all(row["archived"] is False for row in payload["sessions"])


# --- 当前会话被移除时：目标要有地方退 ---


def test_移除当前会话时目标退到下一条(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    first = kernel_sessions.create_session(root, title="当前这条")
    time.sleep(0.01)
    second = kernel_sessions.create_session(root, title="下一条")
    state = _pinned(root, first)
    url = start_server(state)

    status, body = _post(url + "/session/delete", {"session": first})

    assert status == 200, body
    payload = json.loads(body)
    assert payload["target"]["session"] == second, "目标还停在一条已移除的会话上"
    assert payload["target"]["root"] == str(root), "换会话不该动工作区"
    assert _state(url)["target"]["session"] == second
    # 指针也要跟着走：否则重启服务之后又指回那条被移除的会话。
    pointer = kernel_pointers.get("web")
    assert pointer is not None and pointer.session == second


def test_一条都不剩时退回默认会话名(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    only = kernel_sessions.create_session(root, title="就这一条")
    state = _pinned(root, only)
    url = start_server(state)

    status, body = _post(url + "/session/delete", {"session": only})

    assert status == 200, body
    payload = json.loads(body)
    assert payload["target"]["session"] == "cli", "没有可退的会话时要退回默认名"
    assert payload["target"]["session_source"] == "default"
    assert payload["sessions"] == []


# --- 正在跑的不许移除 ---


def test_正在跑的会话不许移除(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    busy = kernel_sessions.create_session(root, title="在跑的这条")
    other = kernel_sessions.create_session(root, title="另一条")
    registry = RunnerRegistry(lambda where, name: _Chatty(where, session=name), limit=2)
    state = WebState(
        registry,
        Target(
            root=root,
            session=busy,
            workspace_source="explicit",
            session_source="explicit",
        ),
        default_session="cli",
    )
    runner = state.current()[1]
    assert runner is not None and runner.start("做点事")
    url = start_server(state)
    try:
        status, body = _post(url + "/session/delete", {"session": busy})

        assert status == 409, body
        assert busy in json.loads(body)["error"], "拒绝了却没说清是谁在跑"
        # 被拒之后它还在列表里（没被悄悄归档）。
        assert busy in [row["id"] for row in _state(url)["sessions"]]

        # 同一张表里**别的**会话照样能移除（管的是"在跑的那一条"，不是整个壳）。
        assert _post(url + "/session/delete", {"session": other})[0] == 200
    finally:
        runner.stop()


# --- 钉住（--root）的壳 ---


def test_钉住的壳不许移除当前会话(start_server, tmp_path: Path) -> None:
    """只有一个目标时移除当前会话＝把页面留在一个没有目标的空壳里。"""
    root = _ws(tmp_path, "a")
    current = kernel_sessions.create_session(root, title="当前")
    other = kernel_sessions.create_session(root, title="另一条")
    state = _pinned(root, current, pinned=True)
    url = start_server(state)

    status, body = _post(url + "/session/delete", {"session": current})

    assert status == 409, body
    assert "--root" in json.loads(body)["error"]
    assert _state(url)["target"]["session"] == current, "被拒之后目标不该动"

    # 当前这条之外仍然可以移除：钉住的是"看哪一条"，不是"什么都不能改"。
    assert _post(url + "/session/delete", {"session": other})[0] == 200


# --- 出错要说人话 ---


def test_移除不存在的会话给人话(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    keep = kernel_sessions.create_session(root, title="留着")
    state = _pinned(root, keep)
    url = start_server(state)

    for path in ("/session/delete", "/session/restore"):
        status, body = _post(url + path, {"session": "查无此会话"})
        assert status == 400, body
        assert "查无此会话" in json.loads(body)["error"]
        assert "Traceback" not in body

    status, body = _post(url + "/session/delete", {})
    assert status == 400
    assert "session" in json.loads(body)["error"]
    assert [row["id"] for row in _state(url)["sessions"]] == [keep]


def test_没有目标时移除被拒给人话(start_server) -> None:
    state = build_state(None, pinned=False)
    url = start_server(state)

    status, body = _post(url + "/session/delete", {"session": "随便哪条"})

    assert status == 409, body
    assert "工作区" in json.loads(body)["error"]


# --- 只读：与 /run、/confirm 同一条线 ---


def test_非回环绑定下移除与恢复一律403(start_server, tmp_path: Path) -> None:
    """只读判据必须**覆盖新端点**：漏一个就等于把安全边界漏在门外。"""
    root = _ws(tmp_path, "a")
    keep = kernel_sessions.create_session(root, title="留着")
    gone = kernel_sessions.create_session(root, title="想移除的")
    state = _pinned(root, keep)
    url = start_server(state)
    # 绑一个非回环地址（本机够得着，但"够得着"与"允许写"是两件事）。
    remote = build_server(state, "0.0.0.0", 0)
    thread = threading.Thread(target=remote.serve_forever, daemon=True)
    thread.start()
    remote_url = f"http://127.0.0.1:{remote.server_port}"
    try:
        for path in ("/session/delete", "/session/restore"):
            status, body = _post(remote_url + path, {"session": gone})
            assert status == 403, body
            payload = json.loads(body)
            assert payload["readonly"] is True
            assert "只能看" in payload["error"]
        # 只读壳上**照旧能看**（列表与已移除那一段都在）。
        payload = json.loads(_get(remote_url + "/state")[1])
        assert keep in [row["id"] for row in payload["sessions"]]
    finally:
        remote.shutdown()
        remote.server_close()
        thread.join(timeout=5)
    # 远程那两下没有落盘：会话还在列表里，没有被归档。
    assert gone in [row["id"] for row in _state(url)["sessions"]]
