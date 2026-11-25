"""后台探活：`/state` 与 `/target` **永不等探测**，并且说得出"什么时候探的"。

改之前这两条端点在请求路径上**同步**探活（`Runner.snapshot()` → `health_snapshot`
→ 发 HTTP）。模型地址连不上时一次探测要等到超时，同一口径实测（`.tmp/web-latency.py`）：

| 场景 | 改前 `GET /state` | 改前 `POST /target` |
|---|---|---|
| 探活打桩成"睡 3 秒" | 3033ms | 3035ms |
| 黑洞地址（连接超时） | 1298ms | 1252ms（缓存过期那一下）|

人在页面上"切个工作区 / 会话"就要等这么久，而那跟切换本身毫无关系。现在探测在一条
后台线程上（`web/probe.py`），请求路径只读**最近一次结果**。

这里量的是这一批新行为，每条都去掉就红：

1. 探活卡住时 `/state` 与 `/target` 仍然**几十毫秒**返回（改前 3 秒）；
2. `/state` 只**增**了 `health` 里那几个字段，`model` / `services` 的口径一个字没动；
3. `unknown`（还没探过）与 `down` 分得开：`model.ok` 是 `None` 而不是 `False`；
4. `slow`（连上了、没在超时内回话）与 `down`（连不上）分得开；
5. `probed_at` / `age_s` 说得出"这是什么时候探的"（陈旧值不许静默）；
6. 探测结果**被复用**：五次 `/state` 不会探五次；
7. 关掉服务之后探测**线程回到基线**（按线程名数）；
8. 探活自己崩了也不把这条线程弄死。

不起长驻进程：服务绑临时端口（`port=0`）在后台线程里 `serve_forever`，`finally`
里关掉；探活用打桩（睡一会儿 / 返回某个结果），不打任何真实地址。
"""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.kernel.target import Target
from spoolkit.web import probe as probe_mod
from spoolkit.web.probe import PROBE_THREAD_NAME, HealthProbe, state_of
from spoolkit.web.runner import Runner
from spoolkit.web.server import WebState, build_server, build_state

#: 假模型的探活是**瞬时**的（不连端口），用它当"正常"那一档。
FAKE = ["--provider", "fake", "--script", ""]

#: 请求时间的上限。探活打桩是睡 2 秒 / 3 秒，而请求该在几十毫秒里回来；
#: 0.6 秒是"本地 HTTP 往返 + 一点余量"，与探活那几秒差一个量级，判据不会飘。
FAST_SECONDS = 0.6


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


def _get(url: str) -> tuple[int, str, float]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read().decode("utf-8"), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), time.monotonic() - started


def _post(url: str, payload: dict) -> tuple[int, str, float]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8"), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), time.monotonic() - started


def _state(url: str) -> dict:
    status, body, _cost = _get(url + "/state")
    assert status == 200, body
    return json.loads(body)


def _wait(predicate, seconds: float = 10.0) -> bool:
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


def _state_for(root: Path, *, extra: list[str] | None = None) -> WebState:
    return build_state(
        None,
        pinned=False,
        target=Target(
            root=root,
            session="s1",
            workspace_source="explicit",
            session_source="explicit",
        ),
        extra_args=list(FAKE if extra is None else extra),
    )


# --- ① 请求路径不等探测 ---


def test_探活卡住时状态接口与切换都不等它(
    start_server, tmp_path: Path, monkeypatch
) -> None:
    """**这一批的主角**：探活睡 2 秒，两个端点仍要在几十毫秒里回来。

    证伪法：把 `WebState.snapshot()` 里的 `self.health()` 换回 `health_snapshot(...)`
    （同步探一次），两条断言立刻变成 ~2000ms 而红——这正是改前的行为。
    """
    from spoolkit import health as health_mod

    def slow(extra, **kwargs):  # noqa: ANN001, ANN003
        time.sleep(2.0)
        return {
            "ok": False,
            "name": "model",
            "target": "http://127.0.0.1:9",
            "detail": "拒绝连接",
            "kind": "connect",
        }

    monkeypatch.setattr(health_mod, "probe_from_extra", slow)

    a, b = _ws(tmp_path, "a"), _ws(tmp_path, "b")
    state = _state_for(a)
    url = start_server(state)

    status, body, cost = _get(url + "/state")
    assert status == 200
    assert cost < FAST_SECONDS, f"GET /state 等了 {cost:.3f} 秒（探活卡住了）"
    # 第一份结果还没到：诚实地报"还没探过"，而不是假装通或断。
    assert json.loads(body)["health"]["state"] == "unknown"

    status, body, cost = _post(url + "/target", {"workspace": str(b)})
    assert status == 200, body
    assert cost < FAST_SECONDS, f"POST /target 等了 {cost:.3f} 秒（探活卡住了）"
    assert json.loads(body)["target"]["root"] == str(b)


def test_探测结果被复用而不是每个请求探一次(
    start_server, tmp_path: Path, monkeypatch
) -> None:
    """五次 `/state` 只该探一次（间隔没到就不重探）。

    证伪法：`snapshot()` 里改成每个请求同步探一次（改前的行为）时这里会数到 5+。
    """
    from spoolkit import health as health_mod

    calls = []

    def counting(extra, **kwargs):  # noqa: ANN001, ANN003
        calls.append(1)
        return {
            "ok": True,
            "name": "model",
            "target": "",
            "detail": "假模型（不连本机端口）",
            "kind": "",
        }

    monkeypatch.setattr(health_mod, "probe_from_extra", counting)

    root = _ws(tmp_path, "a")
    state = _state_for(root)
    # 先把探针起成"间隔一分钟"：这一条要量的是"请求不会自己探"，不是节拍。
    state.start_probe(interval=60.0)
    url = start_server(state)  # build_server 会复用这个探针（幂等）

    assert _wait(lambda: calls), "后台探针一次都没探"
    first = len(calls)
    for _ in range(5):
        assert _state(url)["health"]["model"]["ok"] is True
    assert len(calls) == first, f"请求把探活又打了一遍：{first} → {len(calls)}"


# --- ② `/state` 的既有口径一个字没动，新字段只是加上去 ---


def test_状态里探活那块只增字段(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    state = _state_for(root)
    url = start_server(state)
    assert _wait(lambda: _state(url)["health"]["state"] != "unknown")

    health = _state(url)["health"]

    # 既有字段：一个不少、口径不变（页面状态区读的就是它们）。
    for key in ("model", "services"):
        assert key in health, f"health.{key} 不见了（既有字段不许动）"
    for key in ("ok", "name", "target", "detail"):
        assert key in health["model"], f"health.model.{key} 不见了（既有字段不许动）"
    assert health["services"] == []

    # 新增字段：状态、什么时候探的、离现在多久、探的是哪个地址。
    assert health["state"] in {"up", "slow", "down"}
    assert isinstance(health["probed_at"], float) and health["probed_at"] > 0
    assert isinstance(health["age_s"], float) and health["age_s"] >= 0
    assert isinstance(health["url"], str)


def test_状态里的顶层字段一个不少(start_server, tmp_path: Path) -> None:
    """探活改后台不许顺手把 `/state` 的其它键弄丢（页面的每一块都读它）。"""
    root = _ws(tmp_path, "a")
    state = _state_for(root)
    url = start_server(state)
    payload = _state(url)
    for key in (
        "running",
        "finished",
        "awaiting",
        "awaiting_detail",
        "goal",
        "usage",
        "final",
        "diffs",
        "session",
        "messages",
        "done",
        "ok",
        "pairings",
        "health",
        "target",
        "workspaces",
        "sessions",
        "sessions_removed",
        "readonly",
        "host",
        "running_targets",
    ):
        assert key in payload, f"/state 少了一个既有字段：{key}"


# --- ③ unknown / down / slow 分得开 ---


def test_还没探过与探过不通分得开(start_server, tmp_path: Path, monkeypatch) -> None:
    """`unknown` 不许长得像 `down`：还没探过时 `model.ok` 是 `None`（不是 `False`）。

    `False` 会让状态区那枚徽标立刻变红并写着"模型不通"——那是假话；`True` 更假。
    """
    from spoolkit import health as health_mod

    def slow_down(extra, **kwargs):  # noqa: ANN001, ANN003
        time.sleep(0.4)
        return {
            "ok": False,
            "name": "model",
            "target": "http://127.0.0.1:9",
            "detail": "拒绝连接",
            "kind": "connect",
        }

    monkeypatch.setattr(health_mod, "probe_from_extra", slow_down)

    root = _ws(tmp_path, "a")
    state = _state_for(root)
    url = start_server(state)

    unknown = _state(url)["health"]
    assert unknown["state"] == "unknown"
    assert unknown["model"]["ok"] is None, "还没探过却报了一个通/断"
    assert unknown["probed_at"] is None
    assert unknown["age_s"] is None

    assert _wait(lambda: _state(url)["health"]["state"] != "unknown", 10)
    down = _state(url)["health"]
    assert down["state"] == "down"
    assert down["model"]["ok"] is False
    assert down["probed_at"] is not None and down["age_s"] is not None
    assert "9" in down["url"], "探的是哪个地址要说得出"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("slow", "slow"), ("connect", "down"), ("server", "down"), ("", "down")],
)
def test_慢与不通分成两档(start_server, tmp_path: Path, monkeypatch, kind, expected) -> None:
    """`slow`（连上了、没在超时内回话）与 `down`（连不上）对人的下一步不一样。"""
    from spoolkit import health as health_mod

    def stubbed(extra, **kwargs):  # noqa: ANN001, ANN003
        return {
            "ok": False,
            "name": "model",
            "target": "http://127.0.0.1:8080",
            "detail": "拿不到答案",
            "kind": kind,
        }

    monkeypatch.setattr(health_mod, "probe_from_extra", stubbed)

    root = _ws(tmp_path, "a")
    state = _state_for(root)
    url = start_server(state)
    assert _wait(lambda: _state(url)["health"]["state"] != "unknown", 10)
    assert _state(url)["health"]["state"] == expected


def test_state_of_把探测结果翻成三档() -> None:
    """没经过 HTTP 的那一层：`ok=True` 是 up，`kind=slow` 才是 slow，其余是 down。"""
    assert state_of({"ok": True}) == "up"
    assert state_of({"ok": False, "kind": "slow"}) == "slow"
    assert state_of({"ok": False, "kind": "connect"}) == "down"
    assert state_of({"ok": None}) == "down"  # 老结果没有 kind：宁可当成不通


# --- ④ 什么时候探的 ---


def test_age_s_说得出离现在多久(start_server, tmp_path: Path, monkeypatch) -> None:
    """陈旧值可以接受，**静默的**陈旧值不行：`age_s` 要跟着时间走。

    探针间隔调成一分钟：这样"这一份结果有多旧"只会随时间增长，
    `age_s` 不涨就说明它是探测那一刻的常量（那正是"静默的陈旧值"）。
    """
    root = _ws(tmp_path, "a")
    state = _state_for(root)
    state.start_probe(interval=60.0)
    url = start_server(state)
    assert _wait(lambda: _state(url)["health"]["state"] != "unknown")

    first = _state(url)["health"]
    assert 0.0 <= first["age_s"] < 5.0, "刚探完就说成好几秒前"
    time.sleep(1.1)
    second = _state(url)["health"]
    assert second["probed_at"] == first["probed_at"], "这一条要的是同一份结果"
    assert second["age_s"] > first["age_s"], "age_s 没跟着时间走（陈旧值被静默了）"


# --- ⑤ 线程收得干净（不泄漏） ---


def _probe_threads() -> int:
    return sum(1 for item in threading.enumerate() if item.name == PROBE_THREAD_NAME)


def test_关掉服务之后探测线程回到基线(tmp_path: Path) -> None:
    """谁起的谁收：`WebServer.server_close()` 里把探测线程停掉。

    不钉这条的话，每开一个壳（每个用例起一个服务）都留下一条线程。量的是**线程名**
    （不怕别的库线程混进来）。

    ⚠️ 这条**必须**走老用法（`build_server(runner, …)`）：那条路上 `WebState` 是
    `build_server` 内部造的，调用方**拿不到它**，也就没人会去 `state.close()`——
    **服务的生命周期是那条线程唯一的边界**。用 `WebState` 写这条会假绿：
    teardown 里那句 `state.close()` 顺手就把线程停了，`server_close` 收没收根本量不出来
    （实测：把 `server_close` 里那句 `stop()` 删掉，用 WebState 写的版本照样全绿）。
    """
    baseline = _probe_threads()
    root = _ws(tmp_path, "a")
    runner = Runner(root, session="s1", extra_args=list(FAKE))
    httpd = build_server(runner, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        assert _probe_threads() == baseline + 1, "起了服务却没有后台探针"
        assert _state(url)["host"] == "127.0.0.1"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert _wait(lambda: _probe_threads() == baseline, 10), (
        f"关掉服务之后还剩 {_probe_threads() - baseline} 条探测线程"
    )


def test_没有服务就没有探活线程() -> None:
    """探针是**服务**起的：`build_state(...).snapshot()`（测试 / 嵌入直接用）
    不该顺手拉起一条线程——那种用法里没人会去 close 服务。"""
    assert _probe_threads() == 0
    probe = HealthProbe()
    assert _probe_threads() == 0, "只是构造一个探针就起了线程"
    probe.stop()


# --- ⑥ 探活自己崩了也不许把这条线程弄死 ---


def test_探活崩了线程仍然活着并且报不通(
    start_server, tmp_path: Path, monkeypatch
) -> None:
    """`_probe_once` 必须自己接住异常：抛出去这条线程就死了，而 `/state` 会
    永远停在"还没探过"上（看起来像壳坏了，其实只是探活崩了一次）。"""
    def boom(*_args, **_kwargs):
        raise RuntimeError("探活自己崩了")

    monkeypatch.setattr(probe_mod, "health_snapshot", boom)

    root = _ws(tmp_path, "a")
    state = _state_for(root)
    url = start_server(state)
    assert _wait(lambda: _state(url)["health"]["state"] != "unknown", 10)

    health = _state(url)["health"]
    assert health["state"] == "down"
    assert health["model"]["ok"] is False
    assert "探活失败" in health["model"]["detail"]
    assert _wait(lambda: _probe_threads() >= 1), "探测线程被一次异常弄死了"
