"""⑤ 手动中断任务：`POST /run/stop`。

用户的原话是"运行中只能禁用发送 + 提示，因为服务端没有停止入口"。这个端点补的就是
那个入口，语义刻意**只有一套**：复用 `Runner.stop()`（先 terminate 请它自己收尾、
到点才 kill，见 `web/runner.py`），**不另写一套 kill**。

这里量五件事：

1. **温和停止**：真起一个长驻的假 agent（`fake_idle_agent.py`），停掉之后
   `/state` 不再在跑、`finished` 为真、`stopped` 为真、`final` 有一条**说得出是
   被停掉的**结局（不许让页面停在"正在思考"）；已经写进库里的往来一条都不少；
2. 空闲时停 → **409 说人话**（不冒栈）；
3. 还没有目标时停 → 409 说人话；
4. **非回环一律 403**：与 `/run`、`/confirm`、会话那几个端点走**同一张**
   `WRITE_PATHS` 表，而且拦在业务分支**之前**（一个 stop 调用都不该发生）；
5. 停完之后还能**接着发下一轮**——停止不是把这条壳弄死。
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.kernel.target import Target
from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.transcript import USER, record_message
from spoolkit.store.db import open_db
from spoolkit.web.registry import RunnerRegistry
from spoolkit.web.runner import Runner
from spoolkit.web.server import WRITE_PATHS, WebState, build_server, build_state

FAKE = Path(__file__).resolve().parent / "fake_idle_agent.py"
REMOTE = "0.0.0.0"


class _IdleRunner(Runner):
    """真起一个长驻的假 agent：`/run/stop` 有东西可停，而且停得住（它自己不拦信号）。"""

    def command(self, goal: str) -> list[str]:
        return [
            sys.executable,
            str(FAKE),
            str(self.project_root / ".agent" / "memory.db"),
            self.session,
            goal,
        ]


class _CountingRunner(Runner):
    """不真起子进程的假 Runner：只数"闸门有没有放它进来"。

    非回环那条要量的正是这个——403 之后**一个 stop 都不该发生**。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stop_calls = 0

    @property
    def running(self) -> bool:
        # 装作正在跑：不然 409 会把 403 那条路掩盖掉。
        return True

    def stop(self, timeout: float = 1.0) -> None:
        self.stop_calls += 1


@pytest.fixture
def start_server():
    made: list = []

    def start(state, host: str = "127.0.0.1") -> str:
        httpd = build_server(state, host, 0)
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
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
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


def _target(root: Path, session: str = "t") -> Target:
    return Target(
        root=root,
        session=session,
        workspace_source="explicit",
        session_source="explicit",
    )


def _idle_state(root: Path, session: str = "t") -> WebState:
    """目标已定好、Runner 起的是长驻假 agent（快、确定、能停）。

    先在库里放一条**更早**的往来：它不属于这一轮，停止之后也必须一条不少
    （"不许因为停止而丢消息"量的就是它）。
    """
    conn = open_db(root / ".agent" / "memory.db")
    init_memory_schema(conn)
    record_message(conn, session, USER, "上一轮说过的话")
    conn.close()
    registry = RunnerRegistry(lambda where, name: _IdleRunner(where, session=name))
    return WebState(registry, _target(root, session))


def _wait(predicate, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# --- 停得掉，而且停得干净 ---


def test_运行中能停掉而且状态不再在跑(start_server, tmp_path: Path) -> None:
    """一轮跑起来 → `/run/stop` → 不在跑、有结局、已有消息一条都不少。

    证伪法：把 `/run/stop` 那条分支从 `do_POST` 里删掉 → 404，这条红；
    把 `runner.stop()` 换成"只把 running 置假" → 子进程还在跑，`finished` 永远不真。
    """
    root = _ws(tmp_path, "a")
    state = _idle_state(root)
    url = start_server(state)

    status, body = _post(url + "/run", {"goal": "干一件长活"})
    assert status == 202, body
    # 等它真的**干起活来**（发出了 step），而不是等"进程起来了"：`running` 在
    # Popen 一返回就是真，而那一瞬间它连第一句话都还没写进库。
    assert _wait(lambda: _state(url)["trace"]["lines"]), "假 agent 没开始干活"

    status, body = _post(url + "/run/stop", {})
    assert status == 200, body
    assert json.loads(body) == {"ok": True, "stopped": True}

    assert _wait(lambda: _state(url)["finished"] and not _state(url)["running"]), (
        "停完之后还显示在跑"
    )
    payload = _state(url)
    assert payload["running"] is False
    assert payload["stopped"] is True, "`/state` 说不出这一轮是被停掉的"
    assert payload["awaiting"] == 0, "停了还挂着待确认"
    assert payload["final"], "停了却没有结局：页面会一直停在「正在思考」"
    assert "停止" in payload["final"]["text"], f"结局没说清是停的：{payload['final']}"
    # 已产出的内容**保留**：停之前写进库里的往来一条都不少（那两条都在）。
    assert [item["content"] for item in payload["messages"]] == [
        "上一轮说过的话",
        "干一件长活",
    ]
    # 这一轮攒下的轨迹也还在（这一轮没有助手那条，就挂在它自己那条消息上——
    # 宁可挂在用户那条上，也不能让这一轮的线索没有落脚点）。
    texts = [line["text"] for line in payload["messages"][-1]["trace"]]
    assert any("第 1 步" in one for one in texts), f"停了就把轨迹丢了：{texts}"


def test_停完还能接着发下一轮(start_server, tmp_path: Path) -> None:
    """停止不是把这条壳弄死：`running` 落回去之后，`/run` 照旧收得下一条。"""
    root = _ws(tmp_path, "a")
    state = _idle_state(root)
    url = start_server(state)

    assert _post(url + "/run", {"goal": "第一轮"})[0] == 202
    assert _wait(lambda: _state(url)["running"])
    assert _wait(lambda: _state(url)["trace"]["lines"]), "第一轮没开始干活"
    assert _post(url + "/run/stop", {})[0] == 200
    assert _wait(lambda: _state(url)["finished"])

    assert _post(url + "/run", {"goal": "第二轮"})[0] == 202, "停完之后发不出下一条"
    assert _post(url + "/run/stop", {})[0] == 200
    assert _wait(lambda: _state(url)["finished"])


# --- 没有可停的东西：说人话 ---


def test_空闲时停止是409说人话(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    state = _idle_state(root)
    url = start_server(state)

    status, body = _post(url + "/run/stop", {})

    assert status == 409, body
    error = json.loads(body)["error"]
    assert "没有在跑" in error, f"没说清为什么停不了：{error}"
    assert "Traceback" not in body


def test_没有目标时停止是409说人话(start_server) -> None:
    url = start_server(build_state(None, pinned=False))

    status, body = _post(url + "/run/stop", {})

    assert status == 409, body
    assert "工作区" in json.loads(body)["error"]
    assert "Traceback" not in body


# --- 只读：与 /run、/confirm 同一条线 ---


def test_停止进了同一张写端点表() -> None:
    """新写端点必须进 `WRITE_PATHS`：漏一个就是把安全边界漏在门外。"""
    assert "/run/stop" in WRITE_PATHS


def test_非回环时停止是403而且根本没走到Runner(start_server, tmp_path: Path) -> None:
    """绑在非回环地址上时**只能看**：停止和发任务一样是"驱动"。

    证伪法：把 `/run/stop` 从 `WRITE_PATHS` 里拿掉，这条会变成 200，
    而假 Runner 的 `stop_calls` 会变成 1——它明明在跑，却没人拦。
    """
    root = _ws(tmp_path, "a")
    runner = _CountingRunner(root, session="t")
    state = WebState(
        RunnerRegistry(lambda where, name: runner, limit=1), _target(root)
    )
    url = start_server(state, REMOTE)

    status, body = _post(url + "/run/stop", {})

    assert status == 403, body
    payload = json.loads(body)
    assert payload["readonly"] is True
    assert "只能看" in payload["error"]
    assert runner.stop_calls == 0, "403 之后还走到了 Runner.stop"


# --- 停止的那句话（页面据此不把它画成失败） ---


def test_停止的结局说得出是被停的(tmp_path: Path) -> None:
    """`_death_note` 在人主动停时要**说得出是停的**（退出码照旧给出来）。

    这条补的结局会进快照、也会推到页面上：写成"进程退出，退出码 1"会让一次主动
    停止看起来像一次神秘崩溃，然后人去翻根本不存在的日志。

    证伪法：把 `if self._stopped` 那个分支删掉，这条红。
    """
    runner = _IdleRunner(tmp_path, session="t")
    runner._stopped = True
    note = runner._death_note(1)
    assert "手动停止" in note and "保留" in note
    runner._stopped = False
    assert runner._death_note(1).startswith("进程退出，退出码 1"), "既有口径被改了"
