"""会话标题：种子、Agent 命名与"不覆盖手工标题"。

真正调模型的那一层（`web/titles.py`）用注入的 `title_namer` 代替；这里量的是
`WebState.generate_title()` 的编排与写盘口径，不联网、不起模型。
"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.kernel import sessions
from spoolkit.kernel.target import Target
from spoolkit.llm.types import ChatResponse
from spoolkit.web.server import WRITE_PATHS, WebState, build_server
from spoolkit.web.titles import TITLE_TIMEOUT_SECONDS, TitleNamer


@pytest.fixture
def start_server():
    made: list = []

    def start(state: WebState) -> str:
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


class _Registry:
    def __init__(self, runner: object) -> None:
        self.runner = runner

    def get(self, root: Path, session: str) -> object:
        return self.runner

    def running_targets(self) -> list[dict]:
        return []

    def close(self) -> None:
        pass


class _Runner:
    running = False
    extra_args: tuple[str, ...] = ()

    def __init__(self, root: Path, session: str, goal: str, reply: str) -> None:
        self.project_root = root
        self.session = session
        self._goal = goal
        self._reply = reply

    def snapshot(self, *, health: dict | None = None) -> dict:
        return {
            "messages": [
                {"role": "user", "content": self._goal},
                {"role": "assistant", "content": self._reply},
            ],
            "final": {"ok": True, "text": self._reply},
            "trace": {"open": False, "goal": self._goal, "lines": []},
        }


def _state(tmp_path: Path, *, session: str, goal: str, reply: str, namer) -> WebState:
    root = tmp_path
    runner = _Runner(root, session, goal, reply)
    target = Target(
        root=root,
        session=session,
        workspace_source="explicit",
        session_source="explicit",
    )
    return WebState(
        _Registry(runner),
        target,
        pinned=True,
        title_namer=namer,
    )


def test_Agent标题覆盖种子(tmp_path: Path) -> None:
    root = tmp_path
    made = sessions.create_session(root)
    sessions.ensure_title(root, made, "请帮我修好登录页")
    assert sessions.read_meta(root, made)["title_source"] == sessions.TITLE_SEED

    state = _state(
        root, session=made, goal="请帮我修好登录页", reply="修好了，并补了并发用例",
        namer=lambda goal, reply: "修复登录页并发崩溃",
    )
    status, body = state.generate_title()
    assert status == 200, body
    assert body["title"] == "修复登录页并发崩溃"
    assert body["generated"] is True
    meta = sessions.read_meta(root, made)
    assert meta["title"] == "修复登录页并发崩溃"
    assert meta["title_source"] == sessions.TITLE_AGENT


def test_手工标题不被Agent覆盖(tmp_path: Path) -> None:
    root = tmp_path
    made = sessions.create_session(root, title="我起的名字")
    called = []

    def namer(goal: str, reply: str) -> str:
        called.append((goal, reply))
        return "模型起的名字"

    state = _state(root, session=made, goal="目标", reply="结论", namer=namer)
    status, body = state.generate_title()
    assert status == 200, body
    assert body["title"] == "我起的名字"
    assert body["generated"] is False
    assert called == [], "手工标题还在，不该去调模型"
    assert sessions.read_meta(root, made)["title_source"] == sessions.TITLE_MANUAL


def test_模型失败时保留种子标题(tmp_path: Path) -> None:
    root = tmp_path
    made = sessions.create_session(root)
    state = _state(
        root, session=made, goal="请帮我修好登录页", reply="",
        namer=lambda goal, reply: "",
    )
    status, body = state.generate_title()
    assert status == 200, body
    assert body["title"] == "修好登录页"
    assert body["generated"] is False
    meta = sessions.read_meta(root, made)
    assert meta["title"] == "修好登录页"
    assert meta["title_source"] == sessions.TITLE_SEED


def test_进锁后发现正在跑就不调模型(tmp_path: Path) -> None:
    """锁外还空闲，排队期间用户开了下一轮：409，不调模型，也不写 meta。"""
    root = tmp_path
    made = sessions.create_session(root)
    sessions.ensure_title(root, made, "请帮我修好登录页")
    called = []

    def namer(goal: str, reply: str) -> str:
        called.append((goal, reply))
        return "不该写下的标题"

    runner = _Runner(root, made, "请帮我修好登录页", "修好了")
    state = WebState(
        _Registry(runner),
        Target(
            root=root,
            session=made,
            workspace_source="explicit",
            session_source="explicit",
        ),
        pinned=True,
        title_namer=namer,
    )

    class _Flip:
        def __enter__(self):
            runner.running = True
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    state._title_lock = _Flip()
    version = state.current()[0]
    status, body = state.generate_title()
    assert status == 409, body
    assert body["error"] == "这一轮还没跑完：等它收尾后自动起标题。"
    assert called == []
    assert state.current()[0] == version
    meta = sessions.read_meta(root, made)
    assert meta["title"] == "修好登录页"
    assert meta["title_source"] == sessions.TITLE_SEED


def test_超时保留种子标题(tmp_path: Path) -> None:
    """命名请求超时就当没起成：种子标题留着，不冒成 500。"""
    assert 4 <= TITLE_TIMEOUT_SECONDS <= 5
    root = tmp_path
    made = sessions.create_session(root)
    sessions.ensure_title(root, made, "请帮我修好登录页")

    class _Gateway:
        def chat(self, request: object) -> object:
            raise TimeoutError("title timed out")

    state = _state(
        root, session=made, goal="请帮我修好登录页", reply="修好了", namer=None,
    )
    state._title_namers[str(root)] = TitleNamer(root, gateway=_Gateway())
    status, body = state.generate_title()
    assert status == 200, body
    assert body["generated"] is False
    assert body["title"] == "修好登录页"
    meta = sessions.read_meta(root, made)
    assert meta["title"] == "修好登录页"
    assert meta["title_source"] == sessions.TITLE_SEED


def test_标题端点是写路径() -> None:
    """远程只读壳不许驱动模型：它和 /session/new 走同一张写路径表。"""
    assert "/session/title" in WRITE_PATHS


def test_TitleNamer清洗模型回复(tmp_path: Path) -> None:
    class _Gateway:
        def __init__(self) -> None:
            self.requests = []

        def chat(self, request):
            self.requests.append(request)
            return ChatResponse(text="标题：修复登录页。", prompt_tokens=10,
                                completion_tokens=4)

    gateway = _Gateway()
    namer = TitleNamer(tmp_path, gateway=gateway)
    assert namer.generate("请帮我修好登录页", "修好了") == "修复登录页"
    assert len(gateway.requests) == 1
    assert gateway.requests[0].max_tokens == 48
    assert "请帮我修好登录页" in gateway.requests[0].messages[1].content


def test_POST_session_title_写入标题并返回(start_server, tmp_path: Path) -> None:
    root = tmp_path
    made = sessions.create_session(root)
    state = _state(
        root, session=made, goal="请帮我修好登录页", reply="修好了",
        namer=lambda goal, reply: "修复登录页并发崩溃",
    )
    url = start_server(state)
    status, body = _post(url + "/session/title", {"session": made})
    assert status == 200, body
    payload = json.loads(body)
    assert payload["title"] == "修复登录页并发崩溃"
    assert payload["generated"] is True
    assert sessions.read_meta(root, made)["title_source"] == sessions.TITLE_AGENT

    # `/state` 里也看得到——左栏下一次状态刷新会拿到这个名字。
    with urllib.request.urlopen(url + "/state", timeout=10) as response:
        snapshot = json.loads(response.read().decode("utf-8"))
    rows = [row for row in snapshot["sessions"] if row["id"] == made]
    assert rows and rows[0]["title"] == "修复登录页并发崩溃"
    assert rows[0]["title_source"] == sessions.TITLE_AGENT
    assert snapshot["target"]["session_title"] == "修复登录页并发崩溃"


def test_只读壳不许驱动标题生成(tmp_path: Path) -> None:
    root = tmp_path
    made = sessions.create_session(root)
    state = _state(
        root, session=made, goal="目标", reply="结论",
        namer=lambda goal, reply: "不该被调用",
    )
    httpd = build_server(state, "0.0.0.0", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}"
        status, body = _post(url + "/session/title", {"session": made})
        assert status == 403, body
        assert "只能看" in body
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        state.close()
