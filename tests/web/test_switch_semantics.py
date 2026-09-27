"""切换语义：在跑的任务**不杀、不阻塞**，切回去还看得到全过程（设计 §3.4）。

这是机器级网页壳比 `spool run` 多出来的那件事，也是最容易写错的一处：
`/events` 是长连接，而目标可能在中途被换掉——服务端不退订旧的、不订阅新的，
页面就会一直显示**旧工作区**的事件，看起来"切了"，其实没有。

用假 agent（覆盖 `command()`，见 `test_failure_paths.py` 的先例）：它一直吐事件，
所以"切走之后还在产出"是可以观测的，不必等真模型。
"""

import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.kernel.target import Target
from spoolkit.web.registry import RunnerRegistry
from spoolkit.web.runner import Runner
from spoolkit.web.server import WebState, build_server

# 一直吐事件的假 agent：每 50 毫秒一行 note，够测出"切走之后它还在跑"。
# 事件文本里带目标，用来分辨"这一帧是哪个工作区发出来的"。
CHATTY = (
    "import json, sys, time\n"
    "goal = sys.argv[1] if len(sys.argv) > 1 else 'x'\n"
    "for i in range(400):\n"
    "    print(json.dumps({'type': 'note', 'text': goal + '-' + str(i)}), flush=True)\n"
    "    time.sleep(0.05)\n"
    "print(json.dumps({'type': 'final', 'ok': True, 'text': '跑完了 ' + goal}), flush=True)\n"
)


class _Chatty(Runner):
    """真起子进程、一直吐事件的 Runner。"""

    def command(self, goal: str) -> list[str]:
        return [sys.executable, "-c", CHATTY, goal]


def _wait(predicate, seconds: float = 20.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    return root.resolve()


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


class _Stream:
    """读 `/events` 的客户端：把 data 帧塞进队列，方便"等下一帧"。"""

    def __init__(self, url: str) -> None:
        self._response = urllib.request.urlopen(url, timeout=30)
        self._frames: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            for raw in self._response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue  # 心跳（": ping"）不是帧
                self._frames.put(json.loads(line[len("data: ") :]))
        except Exception:  # noqa: BLE001 - 连接关掉时读循环自然结束
            pass

    def next(self, predicate, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError("等不到满足条件的那一帧")
            try:
                frame = self._frames.get(timeout=left)
            except queue.Empty:
                raise AssertionError("等不到满足条件的那一帧（流里没有新帧）") from None
            if predicate(frame):
                return frame

    def collect(self, seconds: float) -> list[dict]:
        found: list[dict] = []
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return found
            try:
                found.append(self._frames.get(timeout=left))
            except queue.Empty:
                return found

    def close(self) -> None:
        try:
            self._response.close()
        except Exception:  # noqa: BLE001 - 关不上不影响断言
            pass


@pytest.fixture
def start_server():
    made: list = []

    def start(state) -> str:
        httpd = build_server(state, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        made.append((httpd, state))
        return f"http://127.0.0.1:{httpd.server_port}"

    yield start
    for httpd, state in made:
        httpd.shutdown()
        httpd.server_close()
        state.close()


def _machine_state(a: Path) -> WebState:
    """机器级状态：Runner 换成假 agent，其余（目标、注册表）都是真的。"""
    registry = RunnerRegistry(lambda root, session: _Chatty(root, session=session), limit=4)
    return WebState(
        registry,
        Target(
            root=a,
            session="s1",
            workspace_source="explicit",
            session_source="explicit",
        ),
        pinned=False,
    )


def _payload(url: str) -> dict:
    status, body = _get(url + "/state")
    assert status == 200, body
    return json.loads(body)


def test_切走再切回任务没被杀且事件还在(start_server, tmp_path: Path) -> None:
    a, b = _ws(tmp_path, "a"), _ws(tmp_path, "b")
    state = _machine_state(a)
    url = start_server(state)

    assert _post(url + "/run", {"goal": "A 的活"})[0] == 202
    runner_a = state.current()[1]
    assert _wait(lambda: runner_a.running)
    assert _wait(lambda: len(runner_a.events()) >= 3), "它还没开始吐事件"

    # 切走：**不许阻塞**（阻塞的实现会一直等到任务跑完，那是十几秒）。
    started = time.monotonic()
    status, body = _post(url + "/target", {"workspace": str(b)})
    elapsed = time.monotonic() - started
    assert status == 200, body
    assert elapsed < 5, f"切换把请求堵住了 {elapsed:.1f} 秒"
    assert state.current()[1] is not runner_a, "当前目标没换成 B"

    # 任务留在它自己的工作区里继续跑
    assert runner_a.running is True, "切走把任务杀了"
    seen = runner_a.events()[-1]["seq"]
    assert _wait(lambda: runner_a.events()[-1]["seq"] > seen), "切走之后它不再产出事件"

    # `/state` 必须说得出"别处还有一条在跑"，并区分哪个是当前目标
    payload = _payload(url)
    assert payload["target"]["root"] == str(b)
    rows = [row for row in payload["running_targets"] if row["root"] == str(a)]
    assert rows, "在跑的那条没被报出来"
    assert rows[0]["current"] is False
    assert rows[0]["goal"] == "A 的活"

    # 换个目标可以**同时**再起一条：不同 target 互不挡（同一 target 才是 409）
    assert _post(url + "/run", {"goal": "B 的活"})[0] == 202
    assert len(_payload(url)["running_targets"]) == 2

    # 切回去：还是**同一个对象**（同一个目标两条互不相干的历史，正是这张表要防的）
    assert _post(url + "/target", {"workspace": str(a)})[0] == 200
    assert state.current()[1] is runner_a
    payload = _payload(url)
    assert payload["target"]["root"] == str(a)
    assert payload["goal"] == "A 的活"
    assert payload["running"] is True
    assert payload["running_targets"][0]["current"] is True
    events = runner_a.events()
    assert events[0]["seq"] == 1, "切回来之后事件留痕被清掉了"
    assert any(event.get("type") == "note" for event in events)


def test_目标变了事件流跟着换(start_server, tmp_path: Path) -> None:
    """`/events` 是长连接：服务端要退订旧的、订阅新的，并补一帧 state。"""
    a, b = _ws(tmp_path, "a"), _ws(tmp_path, "b")
    state = _machine_state(a)
    url = start_server(state)
    stream = _Stream(url + "/events")
    try:
        first = stream.next(lambda frame: frame.get("type") == "state")
        assert first["target"]["root"] == str(a)

        assert _post(url + "/run", {"goal": "A 的活"})[0] == 202
        stream.next(
            lambda frame: frame.get("type") == "note"
            and str(frame.get("text", "")).startswith("A 的活-")
        )

        # 切到 B：页面上应该立刻收到一帧属于 B 的 state（不必重连浏览器）
        assert _post(url + "/target", {"workspace": str(b)})[0] == 200
        switched = stream.next(
            lambda frame: frame.get("type") == "state"
            and frame["target"]["root"] == str(b)
        )
        assert switched["target"]["session"] == "s1"

        # 旧订阅必须退掉：A 还在每 50 毫秒吐事件，迟到的话这里会读到。
        late = stream.collect(1.0)
        assert not [
            frame for frame in late if str(frame.get("text", "")).startswith("A 的活-")
        ], "还在往旧工作区的事件流上灌事件"

        # 而 B 自己的事件到得了——说明订阅的确实是新的那个
        assert _post(url + "/run", {"goal": "B 的活"})[0] == 202
        stream.next(
            lambda frame: frame.get("type") == "note"
            and str(frame.get("text", "")).startswith("B 的活-")
        )
    finally:
        stream.close()


def test_退回旧订阅不会串台(start_server, tmp_path: Path) -> None:
    """切回 A 之后，事件流要重新订阅 A（而不是停在 B 上）。"""
    a, b = _ws(tmp_path, "a"), _ws(tmp_path, "b")
    state = _machine_state(a)
    url = start_server(state)
    stream = _Stream(url + "/events")
    try:
        stream.next(lambda frame: frame.get("type") == "state")
        assert _post(url + "/run", {"goal": "A 的活"})[0] == 202
        stream.next(
            lambda frame: frame.get("type") == "note"
            and str(frame.get("text", "")).startswith("A 的活-")
        )
        assert _post(url + "/target", {"workspace": str(b)})[0] == 200
        stream.next(
            lambda frame: frame.get("type") == "state" and frame["target"]["root"] == str(b)
        )

        assert _post(url + "/target", {"workspace": str(a)})[0] == 200
        stream.next(
            lambda frame: frame.get("type") == "state" and frame["target"]["root"] == str(a)
        )

        mine = stream.next(
            lambda frame: frame.get("type") == "note"
            and str(frame.get("text", "")).startswith("A 的活-"),
            timeout=20,
        )
        assert str(mine["text"]).startswith("A 的活-")
    finally:
        stream.close()


def test_一个轮询窗口内来回切也要补一帧(start_server, tmp_path: Path, monkeypatch) -> None:
    """A→B→A 落在**同一个轮询窗口**里时，也要补一帧 state。

    这一条是复审逼出来的：重订阅的判据是 Runner 身份（A→B→A 之后身份还是 A，不重订是对的），
    但"补一帧"必须另有判据——否则页面**永远不知道目标动过**。复审用探针复现过，
    并指出这个行为当时**没有用例**（把版本号判据删掉 `tests/web` 仍然全绿），所以这里补上。

    为了确定性地把它钉住：把轮询周期调大、且**不让任何任务在跑**（没有事件的话，
    事件循环就只在周期到点时醒一次），于是两次切换必然落在同一个窗口里——
    这时服务端能依赖的只剩"版本号变过"这一个信号。
    """
    from spoolkit.web import server as web_server

    a, b = _ws(tmp_path, "a"), _ws(tmp_path, "b")
    state = _machine_state(a)
    url = start_server(state)
    stream = _Stream(url + "/events")
    try:
        first = stream.next(lambda frame: frame.get("type") == "state")
        assert first["target"]["root"] == str(a)

        monkeypatch.setattr(web_server, "TARGET_POLL_SECONDS", 2.0)
        assert _post(url + "/target", {"workspace": str(b)})[0] == 200
        assert _post(url + "/target", {"workspace": str(a)})[0] == 200

        # 版本号变过 → 必须有一帧；删掉那个判据时这里会等到超时（用例变红）。
        again = stream.next(lambda frame: frame.get("type") == "state", timeout=8.0)
        assert again["target"]["root"] == str(a)
    finally:
        stream.close()
