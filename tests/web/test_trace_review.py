"""② AI 回复之后：卡片自动更新、思考过程**事后能回看**（排错用）。

用户的原话有两条硬要求，这里是服务端那一半：

1. 回复到达/结束时卡片要被真正的回复替换、分批到达的内容增量追加、不许留下陈旧的
   占位或重复内容——页面那一半的行为数字在 `test_page_dom.py`（`reply_card` 那一组）。
2. **刷新页面、切走再切回，仍能回看那一轮的思考与失败原因**。刷新就是"再请求一次
   `/state`"，切走再切回就是 `POST /target` 出去再回来（同一个 (工作区, 会话) 拿回
   **同一个 Runner**，见 `web/registry.py`）。所以这一条量的是：那两条路之后，
   轨迹里的**失败原因**（哪一步 / 调了什么 / 为什么失败 / 下一步）一个字都不少。

数据从**既有**的地方来：子进程发出来的事件（`trace_line` 把它们翻成人话）+ 记忆库里的
往来（`attach_traces` 按时间窗口把轨迹认领到这一轮那条回复上）。没有第二份数据库，
既有字段与语义一个字都没动——只增了 `Runner.snapshot()` 的 `trace` / `stopped`
与每条消息的 `trace` / `created_at`。
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.kernel import workspaces as kernel_workspaces
from spoolkit.kernel.target import Target
from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.transcript import ASSISTANT, USER
from spoolkit.store.db import open_db
from spoolkit.web.registry import RunnerRegistry
from spoolkit.web.runner import Runner
from spoolkit.web.server import WebState, build_server

FAKE = Path(__file__).resolve().parent / "fake_trace_agent.py"


class _TraceRunner(Runner):
    """用假 agent 换掉真命令行，其余逻辑完全一致（与 `test_failure_paths` 同一个手法）。"""

    def command(self, goal: str) -> list[str]:
        return [
            sys.executable,
            str(FAKE),
            str(self.project_root / ".agent" / "memory.db"),
            self.session,
            goal,
        ]


def _memory(root: Path) -> None:
    conn = open_db(root / ".agent" / "memory.db")
    init_memory_schema(conn)
    conn.close()


def _runner(root: Path) -> _TraceRunner:
    _memory(root)
    return _TraceRunner(root.resolve(), session="t")


def _wait(predicate, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def start_server():
    """起一个真 HTTP 服务（端口交给系统），yield 一个 `start(state)`。"""
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
        with urllib.request.urlopen(url, timeout=15) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
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


def _state_for(root: Path, session: str = "t") -> WebState:
    """一个目标已定好的机器级状态，Runner 换成不起真 agent 的那个。

    为什么要自己拼 `WebState` 而不是 `build_state(...)`：`build_state` 造出来的
    Runner 会去跑真 `spoolkit.cli.app run`（要模型、要几十秒），而这条用例量的是
    "轨迹在刷新与切换之后还在不在"——它要的是**快而确定**的一轮。
    """
    _memory(root)
    registry = RunnerRegistry(lambda where, name: _TraceRunner(where, session=name))
    return WebState(registry, _target(root, session))


def _texts(trace: list[dict]) -> list[str]:
    return [str(line.get("text") or "") for line in trace]


# --- 轨迹真的攒出来了，而且挂在这一轮的回复上 ---


def test_思考轨迹挂在这一轮的回复上(tmp_path: Path) -> None:
    """一轮只挂一条（有回复就挂回复那条）：不然同一段思考会在两张卡片上各出现一遍。

    失败那一轮的工具行**必须带原因**（`detail`）：只写"read_file → 失败"等于把
    排错要的那一句丢掉。
    """
    root = _ws(tmp_path, "a")
    runner = _runner(root)
    assert runner.start("看看 a.py") is True
    assert _wait(lambda: runner.snapshot()["finished"]), "假 agent 没跑完"

    snapshot = runner.snapshot()
    messages = snapshot["messages"]
    assert [item["role"] for item in messages] == [USER, ASSISTANT]
    reply = messages[1]
    texts = _texts(reply["trace"])
    assert any("第 1 步" in one for one in texts), f"没有步骤：{texts}"
    failed = [line for line in reply["trace"] if line.get("ok") is False]
    assert any("read_file" in one for one in _texts(failed)), f"没有工具那行：{texts}"
    tool = [line for line in failed if line.get("type") == "tool"][0]
    assert "文件不存在" in tool["detail"], "工具失败那行没有原因"
    assert any("已拒绝" in one or "没做成" in one for one in texts), (
        f"没有结局那行：{texts}"
    )
    # final 那条的结构化线索（只增字段）跟着轨迹一起留下来了：
    # 页面据此把"哪一步 / 哪个地址 / 下一步敲什么"事后重说一遍。
    final = [line for line in reply["trace"] if line.get("type") == "final"][0]
    assert final["error"]["kind"] == "connect"
    assert final["error"]["url"] == "http://127.0.0.1:9"
    # 一轮只挂一条：用户那条不重复挂同一份轨迹。
    assert messages[0]["trace"] == []
    # 顶层也有一份（这一轮跑完了，所以 open 是假）。
    assert snapshot["trace"]["open"] is False
    assert _texts(snapshot["trace"]["lines"]), "顶层轨迹是空的"


def test_只增不改_既有键一个不少(tmp_path: Path) -> None:
    """新加的东西全是**新增的键**：老驱动者读到的字段与语义一个字没变。"""
    root = _ws(tmp_path, "a")
    runner = _runner(root)
    runner.start("看看 a.py")
    assert _wait(lambda: runner.snapshot()["finished"])
    snapshot = runner.snapshot()

    for key in ("running", "finished", "awaiting", "goal", "usage", "final",
                "diffs", "session", "messages", "done", "ok", "pairings", "health"):
        assert key in snapshot, f"既有字段 {key} 不见了"
    for item in snapshot["messages"]:
        for key in ("role", "content", "meta"):
            assert key in item, f"消息的既有字段 {key} 不见了"
    # 老驱动者读 `messages` 只认 role/content/meta，多出来的键不影响它。
    assert snapshot["messages"][1]["content"] == "没做成：文件不存在"


# --- 刷新 / 切走再切回：轨迹还在（这一批最硬的那条要求） ---


def test_刷新之后轨迹还能回看(start_server, tmp_path: Path) -> None:
    """刷新页面 = 重新请求一次 `/state`：那一轮的思考与失败原因必须还在。

    证伪法：把 `snapshot()` 里 `attach_traces(messages, self._turns)` 那一行删掉，
    这条立刻红（第一次请求也拿不到轨迹）。
    """
    root = _ws(tmp_path, "a")
    state = _state_for(root)
    url = start_server(state)

    status, body = _post(url + "/run", {"goal": "看看 a.py"})
    assert status == 202, body
    assert _wait(lambda: _state(url)["finished"]), "这一轮没跑完"

    first = _state(url)["messages"][-1]
    # "刷新"：同一个服务上重新请求一次（页面重新加载走的就是这一条）。
    fresh = _state(url)["messages"][-1]
    assert fresh["role"] == ASSISTANT
    assert _texts(fresh["trace"]) == _texts(first["trace"]), "刷新之后轨迹变了"
    texts = _texts(fresh["trace"])
    assert any("read_file" in one for one in texts)
    assert any("文件不存在" in one for one in texts), "失败原因没留下来"
    final = [line for line in fresh["trace"] if line.get("type") == "final"][0]
    assert final["error"]["url"] == "http://127.0.0.1:9", "下一步要用的地址没了"


def test_切走再切回轨迹还在(start_server, tmp_path: Path) -> None:
    """切走**不杀任务、也不丢历史**（设计 §3.4）：切回来看到的还是同一个 Runner。

    这里先跑完一轮，再 `POST /target` 切到别的工作区、切回来，然后请求 `/state`。
    证伪法：把 `RunnerRegistry` 的复用改成每次都新建（`registry.py::get`），
    切回来拿到的就是另一个 Runner——轨迹（与事件留痕）都没了，这条红。
    """
    root = _ws(tmp_path, "a")
    other = _ws(tmp_path, "b")
    kernel_workspaces.register(root)
    kernel_workspaces.register(other)
    state = _state_for(root)
    url = start_server(state)

    status, body = _post(url + "/run", {"goal": "看看 a.py"})
    assert status == 202, body
    assert _wait(lambda: _state(url)["finished"]), "这一轮没跑完"
    before = _state(url)["messages"][-1]["trace"]
    assert _texts(before), "跑完就没轨迹，后面那两步等于白量"

    status, body = _post(url + "/target", {"workspace": str(other)})
    assert status == 200, body
    assert json.loads(body)["target"]["root"] == str(other)
    away = _state(url)["messages"]
    assert away == [] or not _texts(away[-1]["trace"]), (
        "换到别的工作区却还看着上一个工作区的轨迹"
    )

    status, body = _post(url + "/target", {"workspace": str(root)})
    assert status == 200, body
    after = _state(url)["messages"][-1]
    assert _texts(after["trace"]) == _texts(before), "切回来轨迹变了或没了"
    assert after["content"] == "没做成：文件不存在"
