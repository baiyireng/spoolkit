"""`serve()` 退出时，把自己拉起的子进程收干净。

机器级壳同时可能有好几个目标在跑（设计 §3.4：切走不杀任务），退出时留下一堆孤儿
进程比什么都糟——它们还在改文件，而人以为"服务关了、活儿停了"。收尾照
`cli/commands/hub.py::stop_children` 的做法：注册表的 `close()` 逐个 `Runner.stop()`。

不跑真 agent：用假 agent（覆盖 `command()`，见 `test_failure_paths.py` 的先例），
它一直吐事件，所以"它还活着"是**可观测**的。
"""

import sys
import time
from pathlib import Path

from spoolkit.kernel.target import Target
from spoolkit.web import server as web_server
from spoolkit.web.registry import RunnerRegistry
from spoolkit.web.runner import Runner
from spoolkit.web.server import WebState

# 一直吐事件、直到被杀的假 agent。
CHATTY = (
    "import json, sys, time\n"
    "print(json.dumps({'type': 'note', 'text': '起来了'}), flush=True)\n"
    "while True:\n"
    "    time.sleep(0.1)\n"
)


class _Chatty(Runner):
    """真起子进程的 Runner（`command()` 换成一段死循环脚本）。"""

    def command(self, goal: str) -> list[str]:
        return [sys.executable, "-c", CHATTY]


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


def test_serve退出时收掉自己拉起的子进程(tmp_path: Path, monkeypatch) -> None:
    """`serve()` 的 `finally` 里那句 `state.close()` 必须真的走到。

    只测 `RunnerRegistry.close()`（`test_runner_registry.py` 里已有）是不够的：
    "表会收"与"退出时真的调了那张表"是两件事，少了后一句，孤儿进程照样留下。
    """
    a = _ws(tmp_path, "a")
    registry = RunnerRegistry(lambda root, session: _Chatty(root, session=session), limit=4)
    state = WebState(
        registry,
        Target(
            root=a,
            session="s1",
            workspace_source="explicit",
            session_source="explicit",
        ),
        pinned=False,
    )
    runner = registry.get(a, "s1")
    assert runner.start("在跑") is True
    assert _wait(lambda: runner.running)

    class _Httpd:
        server_port = 0

        def serve_forever(self) -> None:
            return None  # 立刻"被中断"

        def server_close(self) -> None:
            return None

    # `serve()` 内部自己构造 state：这里换成"已经有一条在跑"的那个，
    # 但它退出时那段收尾走的是**真代码**。
    monkeypatch.setattr(web_server, "build_state", lambda *args, **kwargs: state)
    monkeypatch.setattr(web_server, "build_server", lambda *args, **kwargs: _Httpd())
    # 健康探针是个后台线程，会去打本机默认的模型地址（本机真跑着 llama 的话，
    # 用例结果就取决于那台机器的状态了）。
    monkeypatch.setattr(web_server, "_watch_health", lambda *args, **kwargs: None)

    web_server.serve()

    assert runner._process is not None, "它根本没起过子进程，这条用例什么都没测"
    assert runner.running is False, "serve() 退出后它拉起的子进程还在跑"
    assert _wait(lambda: runner.snapshot()["finished"] is True), "进程没了，但收尾没走完"
