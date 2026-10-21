"""⑤ 「撤销这一轮」是个**动作**，不是一条要用户抄的命令（问题④的网页那一半）。

用户在网页壳里看到的那一行是「撤销这一轮：spool revert --run …」。抄命令这件事
本身就是一个缺口（可发现性、以及"我为什么要手动做这件事"），所以壳上要有一枚
**真能点**的按钮：点一下 POST `/revert`，服务端走的是与 `spool revert --run <id>`
**同一份实现**（`runlog.revert_run`）——两条路的差异只会在最需要它的时候暴露。

这一组钉三件事：

1. 回环上点得动、而且**真的把文件退回去了**（不是只回一句"已撤销"）；
2. 没有可退的一轮时如实报 409，不静默当作成功；
3. 非回环绑定一律 403（`/revert` 会**写工作区**，它必须与 `/run`、`/confirm`
   在同一张 `WRITE_PATHS` 表里——远程只读那条边界不许在它身上漏一个口子）。
"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.kernel.target import Target
from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.web.registry import RunnerRegistry
from spoolkit.web.runner import Runner
from spoolkit.web.server import WebState, build_server
from tests.web.test_remote_readonly import (  # noqa: F401 - 复用那两个小工具
    REMOTE,
    _ws,
)


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


@pytest.fixture
def start_server():
    """起一个真 HTTP 服务（端口交给系统），yield 一个 `start(state, host)`。"""
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


def _state_with(runner: Runner) -> WebState:
    """一个目标已定好、当前 Runner 就是它的机器级状态（与页面同一条路）。"""
    registry = RunnerRegistry(lambda _root, _session: runner, limit=1)
    return WebState(
        registry,
        Target(
            root=runner.project_root,
            session=runner.session,
            workspace_source="explicit",
            session_source="explicit",
        ),
        pinned=True,
        fixed_runner=runner,
    )


def _real_run(root: Path) -> tuple[str, str]:
    """真造一轮：落一次改动（`.agent/runs/<id>/run.json` 因此有了基线）。

    返回（run_id, 改动后的正文）。**不手搓记录文件**：那份记录由
    `PendingChanges.apply()` 自己写，手搓一份就等于测一个假的世界。
    """
    (root / "keep.py").write_text("v1 = 1\n", encoding="utf-8")
    pending = PendingChanges(root)
    write_file_spec(root, pending).handler(
        {"path": "keep.py", "content": "v2 = 1\n"}
    )
    pending.apply()
    return pending.run_id, (root / "keep.py").read_text(encoding="utf-8")


def test_点一下撤销真的把文件退回去(tmp_path: Path, start_server) -> None:
    root = _ws(tmp_path, "a")
    run_id, after = _real_run(root)
    assert after == "v2 = 1\n", "这一轮没有真的落盘，后面那句断言就白量了"

    runner = Runner(root, session="t")
    url = start_server(_state_with(runner))

    status, body = _post(url + "/revert", {"run": run_id})
    assert status == 200, body
    payload = json.loads(body)
    assert payload["ok"] is True
    assert run_id in payload["message"]
    assert (root / "keep.py").read_text(encoding="utf-8") == "v1 = 1\n", (
        "回话说了已撤销，文件却没退回去"
    )


def test_没有回退命令时用这一轮自己的读数(tmp_path: Path, start_server) -> None:
    """页面传空 `run` 时，退的是**这一轮**（`final` 上那个 `revert_run_id`）。"""
    root = _ws(tmp_path, "a")
    run_id, _after = _real_run(root)

    runner = Runner(root, session="t")
    runner._final = {"revert_run_id": run_id}  # 等价于 final 事件已经到过
    url = start_server(_state_with(runner))

    status, body = _post(url + "/revert", {})
    assert status == 200, body
    assert (root / "keep.py").read_text(encoding="utf-8") == "v1 = 1\n"


def test_没有可退的一轮时如实报409(tmp_path: Path, start_server) -> None:
    root = _ws(tmp_path, "a")
    url = start_server(_state_with(Runner(root, session="t")))

    status, body = _post(url + "/revert", {})
    assert status == 409, body
    assert "没有可撤销的一轮" in json.loads(body)["error"]

    status, body = _post(url + "/revert", {"run": "根本没有这条"})
    assert status == 409, body
    assert "没有这条运行记录" in json.loads(body)["error"]


def test_非回环时撤销也是403(tmp_path: Path, start_server) -> None:
    """/revert 会写工作区：它必须在 WRITE_PATHS 里（远程只读不许漏口子）。"""
    root = _ws(tmp_path, "a")
    run_id, _after = _real_run(root)
    url = start_server(_state_with(Runner(root, session="t")), REMOTE)

    status, body = _post(url + "/revert", {"run": run_id})
    assert status == 403, f"/revert 竟然不是 403：{status} {body}"
    assert json.loads(body)["readonly"] is True
    assert (root / "keep.py").read_text(encoding="utf-8") == "v2 = 1\n", (
        "403 之后文件仍然被退回去了"
    )


def test_页面把撤销摆成一个可点的动作() -> None:
    """页面那一半（文本层那条判据，与 `test_page.py` 同一个手法）。

    这一条证伪不了视觉，它钉的是**接线**：`final` 到了要摆那枚按钮、点它走
    `POST /revert`、刷新/切回来那一路也要把它画回去。真行为由上面那几条
    HTTP 用例量（文件真的退回去了）。
    """
    from spoolkit.web.page import HTML

    script = HTML[HTML.rfind("<script>") + len("<script>"): HTML.rfind("</script>")]
    assert "function showUndo(" in script, "页面里没有那个可点的撤销动作"
    assert "post('/revert', {run: runId})" in script, "按钮没有走 /revert"
    final_at = script.find("data.type === 'final'")
    assert "showUndo(data)" in script[final_at: final_at + 900], (
        "final 到了没有把撤销摆出来"
    )
    assert "showUndo(state.final)" in script, "刷新 / 切回来那一路没有画回去"
