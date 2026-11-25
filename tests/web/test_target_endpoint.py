"""机器级网页壳的目标：`GET /state` 与 `POST /target`。

这块的契约是"一个进程 = 一个 target **状态** + 一张 Runner 表"（设计 §3.3）。
所以这里量的是：没有目标时状态要**明确**（页面不必去猜字段缺没缺）、切换之后
`/state` 的口径跟着换、`--root` 起的时候（钉住）拒绝切换并说明、点名一个不存在
的工作区要给**人话**而不是回溯。

不跑真 agent：`/state` 与 `/target` 都不需要子进程，只有 `/run` 才起。
"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.kernel import pointers as kernel_pointers
from spoolkit.kernel import sessions as kernel_sessions
from spoolkit.kernel import workspaces as kernel_workspaces
from spoolkit.kernel.target import Target
from spoolkit.web.runner import Runner
from spoolkit.web.server import build_server, build_state


@pytest.fixture
def start_server():
    """起一个真 HTTP 服务（端口交给系统），yield 一个 start(state) 函数。"""
    made: list = []

    def start(state) -> str:
        httpd = build_server(state, "127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        made.append((httpd, state))
        return f"http://127.0.0.1:{httpd.server_port}"

    yield start
    for httpd, state in made:
        httpd.shutdown()
        httpd.server_close()
        close = getattr(state, "close", None)  # 老用法的参数是个裸 Runner
        if callable(close):
            close()


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


# --- 空态 ---


def test_没有目标时状态是明确的空态(start_server, tmp_path: Path) -> None:
    """机器级网页壳起得来，而且页面一眼看得出"还没有目标、去选一个"。"""
    state = build_state(None, pinned=False)
    url = start_server(state)

    payload = _state(url)
    assert payload["target"]["root"] == ""
    assert payload["target"]["session"] == ""
    assert payload["target"]["pinned"] is False
    assert payload["target"]["workspace_source"] == "none"
    assert payload["target"]["note"], "空态要有一句人话，页面不该自己去猜"
    assert payload["workspaces"] == []
    assert payload["sessions"] == []
    assert payload["running_targets"] == []
    # `running` 仍是"当前目标那一个在不在跑"的布尔值——老语义不许被换成表。
    assert payload["running"] is False
    assert "model" in payload["health"]


def test_没有目标时不许拿cwd顶数(start_server, tmp_path: Path, monkeypatch) -> None:
    """机器级入口不认 cwd（设计 §3.2）：站在工作区里起网页壳也一样没有目标。"""
    here = _ws(tmp_path, "cwd-ws")
    monkeypatch.chdir(here)

    state = build_state(None, pinned=False)
    url = start_server(state)

    assert _state(url)["target"]["root"] == ""


def test_登记表里唯一的工作区成为初始目标(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "only")
    kernel_workspaces.register(root)

    state = build_state(None, pinned=False)
    url = start_server(state)

    payload = _state(url)
    assert payload["target"]["root"] == str(root)
    assert payload["target"]["workspace_source"] == "registry"  # 来源要说得出来
    assert payload["workspaces"][0]["root"] == str(root)
    assert payload["workspaces"][0]["current"] is True


def test_登记表里说不清时先空着并列出候选(start_server, tmp_path: Path) -> None:
    """内核的态度是"不猜"（`resolve_target` 在有多个候选时抛错）。

    机器级网页壳要**接住**那个异常：起得来、先空着、把清单交给页面选——
    让它整个起不来，比"先空着"坏得多。
    """
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kernel_workspaces.register(a)
    kernel_workspaces.register(b)

    state = build_state(None, pinned=False)
    url = start_server(state)

    payload = _state(url)
    assert payload["target"]["root"] == ""
    assert {row["root"] for row in payload["workspaces"]} == {str(a), str(b)}


# --- 切换 ---


def test_切换之后状态跟着换(start_server, tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kernel_workspaces.register(a)
    kernel_workspaces.register(b)
    kernel_sessions.create_session(b, title="B 里的这条")
    state = build_state(None, pinned=False)
    url = start_server(state)

    status, body = _post(url + "/target", {"workspace": str(a)})
    assert status == 200, body
    assert json.loads(body)["target"]["root"] == str(a)
    assert _state(url)["target"]["root"] == str(a)

    status, body = _post(url + "/target", {"workspace": "b"})  # 名字也认
    assert status == 200, body
    payload = json.loads(body)
    assert payload["target"]["root"] == str(b)
    assert payload["target"]["workspace_source"] == "explicit"
    assert [row["title"] for row in payload["sessions"]] == ["B 里的这条"]


def test_只给会话时工作区不变(start_server, tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    made = kernel_sessions.create_session(a, title="换到这条")
    state = build_state(None, pinned=False)
    url = start_server(state)
    assert _post(url + "/target", {"workspace": str(a)})[0] == 200

    status, body = _post(url + "/target", {"session": made})

    assert status == 200, body
    payload = json.loads(body)
    assert payload["target"]["root"] == str(a)
    assert payload["target"]["session"] == made
    assert payload["target"]["session_source"] == "explicit"


def test_没有目标时只给会话给人话(start_server, tmp_path: Path) -> None:
    """只给 `session`、而**还没有当前目标**时必须 400，并且说人话。

    机器级壳刚起来（还没有目标）时，页面上的会话下拉是空的，但外部驱动者
    （脚本、MCP 客户端）完全可能只发一个 `{"session": …}`。这时**不能猜**一个
    工作区顶数——哪怕登记表里就有候选：猜错的症状是"它在一份我以为不相干的记忆里
    干活"，而那正是内核反复拒绝做的那类猜测（`kernel/target.py` 的自述）。
    """
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kernel_workspaces.register(a)
    kernel_workspaces.register(b)  # 有两个候选 → 内核说不清 → 还没有目标
    state = build_state(None, pinned=False)
    url = start_server(state)
    assert _state(url)["target"]["root"] == ""

    status, body = _post(url + "/target", {"session": "随便哪条"})

    assert status == 400
    payload = json.loads(body)
    assert "workspace" in payload["error"]
    assert "Traceback" not in body
    assert _state(url)["target"]["root"] == "", "被拒之后目标不该动"


# --- 指针：显式选择要写下来 ---


def test_显式切换会写下网页壳指针(start_server, tmp_path: Path) -> None:
    """刷新页面停得住是不够的：**重启服务**也要停住。

    进程内的 `WebState` 一重启就没了，于是回到内核顺位；而多个工作区时
    `register()` 的排序副作用（登记表里排第一）不足以让"上次选的那个"回来。
    所以页面上的显式选择要写进 `clients.json` 的 `web` 那条。
    """
    a = _ws(tmp_path, "a")
    made = kernel_sessions.create_session(a, title="换到这条")
    state = build_state(None, pinned=False)
    url = start_server(state)

    assert _post(url + "/target", {"workspace": str(a), "session": made})[0] == 200

    pointer = kernel_pointers.get("web")
    assert pointer is not None, "显式切换没写指针：重启服务就回到内核顺位了"
    assert pointer.workspace == a
    assert pointer.session == made

    # 重启一次（机器级、不带 --root）：初始目标就该是指针那条
    again = build_state(None, pinned=False)
    try:
        payload = again.snapshot()
    finally:
        again.close()
    assert payload["target"]["root"] == str(a)
    assert payload["target"]["session"] == made
    assert payload["target"]["workspace_source"] == "pointer"


def test_内核顺位定出来的初始目标不写指针(start_server, tmp_path: Path) -> None:
    """口径与 `spool ws use` 一致：**只有显式选择才写**。

    登记表里只有一个工作区时，内核顺位会把它定成初始目标——那是内核推断的，
    不是人选的结果。把它记成"上次选的"，下一次启动就会跳过更合适的候选
    （指针排在登记表前面）。
    """
    root = _ws(tmp_path, "only")
    kernel_workspaces.register(root)

    state = build_state(None, pinned=False)
    url = start_server(state)

    assert _state(url)["target"]["workspace_source"] == "registry"
    assert kernel_pointers.get("web") is None, "内核顺位定出来的目标被当成「人选过」记下来了"


def test_空body的no_op切换不写指针(start_server, tmp_path: Path) -> None:
    """空 body 的 `POST /target` 什么都没改，也就什么都不该记。"""
    a = _ws(tmp_path, "a")
    state = build_state(
        None,
        pinned=False,
        target=Target(
            root=a,
            session="s1",
            workspace_source="registry",
            session_source="default",
        ),
    )
    url = start_server(state)

    status, body = _post(url + "/target", {})

    assert status == 200, body
    assert json.loads(body)["target"]["root"] == str(a)
    assert kernel_pointers.get("web") is None, "没人选什么，却记了一条指针"


def test_指针写不进去也不挡切换(start_server, tmp_path: Path, monkeypatch) -> None:
    """指针是便利，不是数据（`kernel/pointers.py` 的自述）。

    写不进去（磁盘满、状态目录没权限）时，切换**已经在进程内生效**了；这时回报
    失败等于告诉人"没切成"，而它其实是切成了。
    """
    a = _ws(tmp_path, "a")
    state = build_state(None, pinned=False)
    url = start_server(state)

    def boom(*args, **kwargs):
        raise OSError("状态目录写不进去")

    monkeypatch.setattr(kernel_pointers, "set", boom)

    status, body = _post(url + "/target", {"workspace": str(a)})

    assert status == 200, body
    assert json.loads(body)["target"]["root"] == str(a)
    assert _state(url)["target"]["root"] == str(a)


# --- 出错时给人话 ---


def test_切到不存在的工作区给人话(start_server, tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    kernel_workspaces.register(a)
    state = build_state(None, pinned=False)
    url = start_server(state)

    status, body = _post(url + "/target", {"workspace": "查无此区"})

    assert status == 400
    payload = json.loads(body)  # 必须是 JSON：回溯会让页面显示一坨栈
    assert "查无此区" in payload["error"]
    assert "Traceback" not in body


def test_切到不存在的会话给人话(start_server, tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    kernel_sessions.create_session(a, title="真有的那条")
    state = build_state(None, pinned=False)
    url = start_server(state)
    assert _post(url + "/target", {"workspace": str(a)})[0] == 200

    status, body = _post(url + "/target", {"session": "查无此会话"})

    assert status == 400
    assert "查无此会话" in json.loads(body)["error"]


def test_没有目标时发起运行被拒(start_server) -> None:
    state = build_state(None, pinned=False)
    url = start_server(state)

    status, body = _post(url + "/run", {"goal": "做点事"})

    assert status == 409
    assert "工作区" in json.loads(body)["error"]


# --- 钉住（老行为） ---


def test_给了root就钉住不许切(start_server, tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kernel_workspaces.register(b)
    state = build_state(a, pinned=True)
    url = start_server(state)

    payload = _state(url)
    assert payload["target"]["root"] == str(a)
    assert payload["target"]["pinned"] is True

    status, body = _post(url + "/target", {"workspace": str(b)})

    assert status == 409
    assert "--root" in json.loads(body)["error"]
    assert _state(url)["target"]["root"] == str(a), "被拒之后目标不该动"


def test_调用方解析好的来源原样收下(start_server, tmp_path: Path) -> None:
    """`serve_command` 已经把目标连来源一起解析好了：不许在这里重解析一遍。

    重解析会把"这条会话来自指针"记成"显式给的"——`/state` 的来源说明就成了假话。
    """
    a = _ws(tmp_path, "a")
    resolved = Target(
        root=a,
        session="指针那条",
        workspace_source="explicit",
        session_source="pointer",
    )
    state = build_state(a, session="指针那条", pinned=True, target=resolved)
    url = start_server(state)

    payload = _state(url)
    assert payload["target"]["session_source"] == "pointer"
    assert payload["target"]["workspace_source"] == "explicit"


def test_老用法的单个Runner也是钉住的(start_server, tmp_path: Path) -> None:
    """`build_server(runner, …)`（测试、嵌入）的行为不许变：它只有一个目标。"""
    runner = Runner(tmp_path, session="t", extra_args=["--provider", "fake", "--script", ""])
    url = start_server(runner)

    assert _state(url)["target"]["session"] == "t"
    assert _state(url)["target"]["pinned"] is True
    assert _post(url + "/target", {"workspace": str(tmp_path)})[0] == 409


def test_给了project_root就等于钉住它(tmp_path: Path, monkeypatch) -> None:
    """评审 Important：`build_state(root)` 原先会**静默忽略** root。

    `serve(project_root)` 是这套壳最初的签名，它的意思是"钉住这个工作区"，
    而不是"机器级的初始目标"。不归一的话，`serve(SA)` 会转身去钉指针或登记表里的
    **另一个**工作区（评审实测：实际 target 是 SB，而且一声不响）。
    """
    from spoolkit.kernel import pointers as kernel_pointers

    a = _ws(tmp_path, "A")
    b = _ws(tmp_path, "B")
    kernel_workspaces.register(b)
    kernel_pointers.set("web", b, "s-b")  # "别处"确实存在，而且指针正指着它

    state = build_state(a)  # 只给 root，不给 pinned
    try:
        payload = state.snapshot()
    finally:
        state.close()

    assert payload["target"]["root"] == str(a.resolve())
    assert payload["target"]["pinned"] is True  # 它被当成"钉住这个"
    assert payload["target"]["workspace_source"] == "explicit"


def test_serve_的提示语也要说真话(tmp_path: Path, monkeypatch, capsys) -> None:
    """`serve(root)` 不带 `pinned` 时，终端那句话必须说"钉住了"，不能说"机器级"。

    归一只有一处是不够的：`build_state` 里改了、`serve` 里没改的话，行为对而
    **提示语在骗人**——而"终端说机器级、实际钉住了"正是这次评审逮到的那个形状。
    """
    from spoolkit.web import server as web_server

    a = _ws(tmp_path, "A")

    class _Httpd:
        server_port = 0

        def serve_forever(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    seen: dict = {}
    real_build_state = web_server.build_state

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_build_state(*args, **kwargs)

    monkeypatch.setattr(web_server, "build_state", spy)
    monkeypatch.setattr(web_server, "build_server", lambda state, host, port: _Httpd())
    # 健康探针是个后台线程，会去打本机默认的模型地址（开发机上真在跑的话，
    # 测试输出里会冒出"模型通"，而且用例的结果就依赖了那台机器的状态）。
    monkeypatch.setattr(web_server, "_watch_health", lambda *a, **k: None)

    web_server.serve(a)
    err = capsys.readouterr().err

    assert seen.get("pinned") is True  # 归一发生在进 build_state 之前
    assert "机器级网页壳" not in err  # 提示语不许说反
