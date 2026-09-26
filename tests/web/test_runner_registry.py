"""按 (工作区, 会话) 复用的 Runner 表。

这张表的存在理由只有一个：**切走再切回要看到同一个对象**。所以这里量的全是
"同一个键给的是不是同一个对象"、"上限淘汰的是不是最久没看的那个空闲目标"、
以及"在跑的一个都不许动"。用假的 Runner（覆盖 `command()`，见
`test_failure_paths.py` 的先例）把时间压到毫秒级，不跑真 agent。
"""

import sys
import time
from pathlib import Path

import pytest

from spoolkit.web.registry import DEFAULT_LIMIT, RunnerRegistry
from spoolkit.web.runner import Runner

# 一直吐事件、直到被杀掉的假 agent。用它才测得出"在跑的不被淘汰"：
# 必须有真的子进程在跑。
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


class _Fake(Runner):
    """不起子进程：只记 `stop()` 被叫过几次。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stopped = 0

    def stop(self, timeout: float = 0.0) -> None:
        self.stopped += 1


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


# --- 同一个键同一个对象 ---


def test_同一个键永远是同一个对象(tmp_path: Path) -> None:
    made: list[Path] = []
    registry = RunnerRegistry(lambda root, session: made.append(root) or _Fake(root))
    a = _ws(tmp_path, "a")

    first = registry.get(a, "s1")
    assert registry.get(a, "s1") is first
    assert registry.get(str(a), "s1") is first  # 路径写成字符串也算同一个键
    assert made == [a], "同一个键被构造了不止一次"


def test_不同键各是一个对象(tmp_path: Path) -> None:
    registry = RunnerRegistry(lambda root, session: _Fake(root, session=session))
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")

    assert registry.get(a, "s1") is not registry.get(a, "s2")
    assert registry.get(a, "s1") is not registry.get(b, "s1")


# --- 上限与回收 ---


def test_超过上限淘汰最久未使用的那个(tmp_path: Path) -> None:
    registry = RunnerRegistry(lambda root, session: _Fake(root), limit=2)
    a, b, c = (_ws(tmp_path, name) for name in "abc")

    first_a = registry.get(a, "s")
    first_b = registry.get(b, "s")
    assert registry.get(a, "s") is first_a  # a 刚被用过，b 成了最久没看的那个
    registry.get(c, "s")  # 超上限 → 淘汰 b

    assert registry.get(a, "s") is first_a, "最久没看的那个不是 b"
    assert registry.get(b, "s") is not first_b, "最久未使用的那个没有被淘汰"


def test_在跑的目标不被淘汰(tmp_path: Path) -> None:
    """切走不许打断任务：A 在跑，页面切到 B、C，A 必须活着、还是同一个对象。"""
    registry = RunnerRegistry(lambda root, session: _Chatty(root), limit=2)
    a, b, c = (_ws(tmp_path, name) for name in "abc")

    running = registry.get(a, "s")
    assert running.start("在跑") is True
    assert _wait(lambda: running.running)

    registry.get(b, "s")
    registry.get(c, "s")

    assert registry.get(a, "s") is running, "在跑的目标被当成空闲的淘汰掉了"
    assert running.running is True, "在跑的子进程被杀了"
    registry.close()


def test_在跑的多于上限时也不杀(tmp_path: Path) -> None:
    """上限不是硬保证：`limit=1` 但有两条在跑时，两条都留着。"""
    registry = RunnerRegistry(lambda root, session: _Chatty(root), limit=1)
    a, b = (_ws(tmp_path, name) for name in "ab")

    first = registry.get(a, "s")
    assert first.start("在跑") is True
    second = registry.get(b, "s")
    assert second.start("也在跑") is True

    assert registry.get(a, "s") is first
    assert registry.get(b, "s") is second
    registry.close()


def test_别人注册表里的目标不会被顺手杀(tmp_path: Path) -> None:
    """`close()` 只收自己这张表里的：淘汰是摘表，不是杀进程。"""
    a = _ws(tmp_path, "a")
    other = _Chatty(a)
    assert other.start("别人的") is True
    assert _wait(lambda: other.running)

    registry = RunnerRegistry(lambda root, session: _Fake(root), limit=1)
    registry.get(a, "s")
    registry.get(_ws(tmp_path, "b"), "s")  # 触发一次淘汰
    assert other.running is True, "淘汰把不相干的进程杀了"
    other.stop()


# --- 清场 ---


def test_close收掉每一个自己拉起的子进程(tmp_path: Path) -> None:
    made: list[_Fake] = []

    def build(root: Path, session: str) -> _Fake:
        runner = _Fake(root, session=session)
        made.append(runner)
        return runner

    registry = RunnerRegistry(build, limit=DEFAULT_LIMIT)
    registry.get(_ws(tmp_path, "a"), "s1")
    registry.get(_ws(tmp_path, "a"), "s2")

    registry.close()

    assert [item.stopped for item in made] == [1, 1]


def test_close真的杀掉了在跑的子进程(tmp_path: Path) -> None:
    """不是"从表里删掉"就算数：真子进程要收干净（照 hub.stop_children）。"""
    registry = RunnerRegistry(lambda root, session: _Chatty(root), limit=2)
    runner = registry.get(_ws(tmp_path, "a"), "s")
    assert runner.start("在跑") is True
    assert _wait(lambda: runner.running)

    registry.close()

    assert runner.running is False, "close() 之后子进程还在跑"
    # 读输出的那个线程在子进程退出后才补上结局，给它一拍。
    assert _wait(lambda: runner.snapshot()["finished"] is True)


def test_上限至少是1(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RunnerRegistry(lambda root, session: _Fake(root), limit=0)


# --- 给页面看的那一行 ---


def test_running_targets只报在跑的(tmp_path: Path) -> None:
    registry = RunnerRegistry(lambda root, session: _Chatty(root), limit=4)
    a, b = (_ws(tmp_path, "a"), _ws(tmp_path, "b"))
    idle = registry.get(b, "闲的")

    running = registry.get(a, "忙的")
    assert running.start("正在做的事") is True
    assert _wait(lambda: running.running)

    rows = registry.running_targets()
    assert [row["session"] for row in rows] == ["忙的"]
    assert rows[0]["root"] == str(a)
    assert rows[0]["name"] == "a"  # 工作区名：页面要显示"谁在跑"
    assert rows[0]["goal"] == "正在做的事"
    assert idle.running is False
    registry.close()
