"""POST /session/rename：人手改会话标题。不画界面。"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from spoolkit.kernel import sessions
from spoolkit.kernel.target import Target
from spoolkit.web.server import WRITE_PATHS, WebState, build_server


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


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class _Registry:
    def get(self, root: Path, session: str) -> object:
        return self.runner

    def running_targets(self) -> list[dict]:
        return []

    def close(self) -> None:
        pass


class _Runner:
    running = False
    extra_args: tuple[str, ...] = ()

    def __init__(self, root: Path, session: str) -> None:
        self.project_root = root
        self.session = session

    def snapshot(self, *, health: dict | None = None) -> dict:
        return {
            "messages": [
                {"role": "user", "content": "请帮我修好登录页"},
                {"role": "assistant", "content": "修好了"},
            ],
            "final": {"ok": True, "text": "修好了"},
            "trace": {"open": False, "goal": "请帮我修好登录页", "lines": []},
        }


def _state(root: Path, session: str, namer=None) -> WebState:
    runner = _Runner(root, session)
    registry = _Registry()
    registry.runner = runner
    return WebState(
        registry,
        Target(
            root=root,
            session=session,
            workspace_source="explicit",
            session_source="explicit",
        ),
        pinned=True,
        title_namer=namer,
    )


def _serve(state: WebState, host: str = "127.0.0.1") -> tuple[object, str]:
    httpd = build_server(state, host, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


def _stop(httpd: object, state: WebState) -> None:
    httpd.shutdown()
    httpd.server_close()
    state.close()


def test_重命名写入manual且折叠空白(tmp_path: Path) -> None:
    root = tmp_path
    made = sessions.create_session(root, title="旧标题")
    before = sessions.read_meta(root, made)
    sessions.write_meta(root, made, note="别丢掉")
    state = _state(root, made)
    version = state.current()[0]
    httpd, url = _serve(state)
    try:
        status, body = _post(url + "/session/rename", {"session": made, "title": "  甲   乙  "})
        assert status == 200, body
        assert json.loads(body) == {
            "session": made,
            "title": "甲 乙",
            "title_source": "manual",
        }
        meta = sessions.read_meta(root, made)
        assert meta["title"] == "甲 乙"
        assert meta["title_source"] == sessions.TITLE_MANUAL
        assert meta["note"] == "别丢掉"
        assert meta["created_at"] == before["created_at"]
        assert state.current()[0] == version + 1
        snapshot = _get(url + "/state")
        rows = [row for row in snapshot["sessions"] if row["id"] == made]
        assert rows and rows[0]["title"] == "甲 乙"
        assert rows[0]["title_source"] == "manual"
    finally:
        _stop(httpd, state)


def test_改成manual后Agent标题不再覆盖(tmp_path: Path) -> None:
    root = tmp_path
    made = sessions.create_session(root)
    sessions.ensure_title(root, made, "请帮我修好登录页")
    called = []

    def namer(goal: str, reply: str) -> str:
        called.append((goal, reply))
        return "模型起的名字"

    state = _state(root, made, namer=namer)
    status, body = state.rename_session(made, "人手标题")
    assert status == 200, body
    status, body = state.generate_title()
    assert status == 200, body
    assert body["title"] == "人手标题"
    assert body["generated"] is False
    assert called == []
    meta = sessions.read_meta(root, made)
    assert meta["title"] == "人手标题"
    assert meta["title_source"] == sessions.TITLE_MANUAL


def test_空标题超长未知会话和无目标(tmp_path: Path) -> None:
    root = tmp_path
    made = sessions.create_session(root, title="保持不变")
    state = _state(root, made)
    version = state.current()[0]

    status, body = state.rename_session("", "有标题")
    assert status == 400, body
    status, body = state.rename_session(made, "   \n\t  ")
    assert status == 400, body
    status, body = state.rename_session(made, "测" * (sessions.TITLE_LIMIT + 1))
    assert status == 400, body
    assert sessions.read_meta(root, made)["title"] == "保持不变"
    status, body = state.rename_session("no-such-session", "新标题")
    assert status == 404, body
    assert state.current()[0] == version

    empty = WebState(_Registry(), None)
    status, body = empty.rename_session(made, "新标题")
    assert status == 409, body
    empty.close()


def test_重命名端点是写路径且只读壳拒绝(tmp_path: Path) -> None:
    assert "/session/rename" in WRITE_PATHS
    root = tmp_path
    made = sessions.create_session(root, title="原标题")
    state = _state(root, made)
    httpd, url = _serve(state, "0.0.0.0")
    try:
        status, body = _post(url + "/session/rename", {"session": made, "title": "不该写上"})
        assert status == 403, body
        assert sessions.read_meta(root, made)["title"] == "原标题"
    finally:
        _stop(httpd, state)


def test_正好四十个字可以改(tmp_path: Path) -> None:
    root = tmp_path
    made = sessions.create_session(root, title="旧")
    title = "名" * sessions.TITLE_LIMIT
    state = _state(root, made)
    status, body = state.rename_session(made, title)
    assert status == 200, body
    assert body["title"] == title
    assert sessions.read_meta(root, made)["title"] == title
