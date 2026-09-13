"""Web 壳的错误路径。

这块的时序最容易出错，而出错的表现又最像「它在忙」：确认按钮点不动、
页面停在运行中、崩了却没有结局。所以这里全部用假 agent 把时序压到
毫秒级，让四种情况都能反复跑。
"""

import sys
import time
from pathlib import Path

from spoolkit.web.protocol import FINAL
from spoolkit.web.runner import Runner

FAKE = Path(__file__).resolve().parent / "fake_agent.py"


class _Scripted(Runner):
    """用假 agent 脚本替换真实命令行，其余逻辑完全一致。"""

    def command(self, goal: str) -> list[str]:
        return [sys.executable, str(FAKE)]


class _Silent(Runner):
    """跑完什么都不发的子进程，用来验证「必须补一条结局」。"""

    def command(self, goal: str) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.exit(3)"]


class _Noisy(Runner):
    """先吐一句人话再死。真实崩溃就是这个样子：原因在 stderr 上。"""

    def command(self, goal: str) -> list[str]:
        return [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('脚本文件不存在: x.json\\n'); sys.exit(2)",
        ]


def _wait(predicate, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _finals(listener) -> list:
    items = []
    while True:
        try:
            items.append(listener.get_nowait())
        except Exception:
            break
    return [event for event in items if event.type == FINAL]


def test_假agent能走到等待确认(tmp_path: Path) -> None:
    runner = _Scripted(tmp_path)
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)


def test_确认y会写进子进程并得到回应(tmp_path: Path) -> None:
    """这条走的是真实的 stdin 管道——不是模拟，是真写进去。"""
    runner = _Scripted(tmp_path)
    listener = runner.subscribe()
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)
    assert runner.confirm(True) is True
    assert _wait(lambda: not runner.running)
    finals = _finals(listener)
    assert finals and "收到 y" in finals[-1].data["text"]


def test_确认n也会写进去(tmp_path: Path) -> None:
    runner = _Scripted(tmp_path)
    listener = runner.subscribe()
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)
    assert runner.confirm(False) is True
    assert _wait(lambda: not runner.running)
    assert "收到 n" in _finals(listener)[-1].data["text"]


def test_反复确认只生效一次(tmp_path: Path) -> None:
    """重复写 stdin 会让后续的 input() 拿到意外的输入，那类错位极难查。"""
    runner = _Scripted(tmp_path)
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)
    assert runner.confirm(True) is True
    assert runner.confirm(True) is False


def test_没有等待时确认被拒绝(tmp_path: Path) -> None:
    assert _Scripted(tmp_path).confirm(True) is False


def test_快照带着待确认的diff(tmp_path: Path) -> None:
    """重连的页面只能靠快照重建面板。

    快照里没有 diff 的话，按钮会回来、内容却是空的——那时候用户
    只能盲点「应用」，而这正是「先看 diff 再应用」想避免的事。
    """
    runner = _Scripted(tmp_path)
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)
    diffs = runner.snapshot()["diffs"]
    assert diffs and diffs[0]["path"] == "a.py"
    assert "+新" in diffs[0]["text"]


def test_决定之后快照不再挂diff(tmp_path: Path) -> None:
    runner = _Scripted(tmp_path)
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)
    assert runner.confirm(False) is True
    assert _wait(lambda: not runner.snapshot()["diffs"])


def test_子进程没给结局时会补一条失败结局(tmp_path: Path) -> None:
    """不给结局的话界面会永远停在「运行中」，而你以为它还在干活。"""
    runner = _Silent(tmp_path)
    listener = runner.subscribe()
    runner.start("做点事")
    assert _wait(lambda: not runner.running)
    finals = _finals(listener)
    assert finals and finals[-1].data["ok"] is False


def test_补的结局也要进快照(tmp_path: Path) -> None:
    """重连的页面拿的是快照，补发的事件它一条都收不到。

    只广播不进快照的话，断线再连上来的页面看到的是「已完成、却没有任何
    结局」——那一屏什么都没有，比报错还难查：既像跑完了又像没跑。
    """
    runner = _Silent(tmp_path)
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["final"] is not None, timeout=20)
    final = runner.snapshot()["final"]
    assert final["ok"] is False
    assert "退出码 3" in final["text"]


def test_正常结局也在快照里(tmp_path: Path) -> None:
    """补发的路径修好了，别把正常那条踩坏。"""
    runner = _Scripted(tmp_path)
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)
    assert runner.confirm(False) is True
    assert _wait(lambda: runner.snapshot()["final"] is not None)
    assert runner.snapshot()["final"]["ok"] is True


def test_崩溃原因不会被吞掉(tmp_path: Path) -> None:
    """只说「退出码 2」等于没说——原因就在先前被丢弃的那几行里。"""
    runner = _Noisy(tmp_path)
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["final"] is not None, timeout=20)
    text = runner.snapshot()["final"]["text"]
    assert "退出码 2" in text
    assert "脚本文件不存在" in text


def test_崩过一次还能再发一次(tmp_path: Path) -> None:
    """崩了不该把服务卡死。

    这里必须用 _Silent：假 agent 会停在 stdin 上等确认，
    没人回答它就永远不会结束，拿它测「崩了之后还能不能再发」是测不到的。
    """
    runner = _Silent(tmp_path)
    runner.start("第一次")
    assert _wait(lambda: not runner.running)
    assert runner.start("第二次") is True


def test_正常结束后还能再发一次(tmp_path: Path) -> None:
    runner = _Scripted(tmp_path)
    runner.start("第一次")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)
    assert runner.confirm(False) is True
    assert _wait(lambda: not runner.running)
    assert runner.start("第二次") is True


def test_订阅者断开后不再收到事件(tmp_path: Path) -> None:
    runner = _Scripted(tmp_path)
    listener = runner.subscribe()
    runner.unsubscribe(listener)
    runner.start("做点事")
    time.sleep(0.5)
    assert listener.empty()
