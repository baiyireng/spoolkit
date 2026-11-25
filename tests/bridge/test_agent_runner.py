"""端到端：一条"聊天消息"真的驱动了一次 agent 运行。

这里**不假造 agent**：`AgentRunner` 起的是真的 `run --events` 子进程，
只是把模型换成假模型（离线、确定）。量的是这条链本身——
消息进去、子进程起来、事件流被读、结局被取回、回复出来。
"""

import json
from pathlib import Path

from spoolkit.bridge.agent_runner import AgentRunner


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "收尾", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _script(tmp_path: Path, answers: list[str]) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
    return path


def test_一条消息换来一段答复(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    script = _script(tmp_path, [_turn("看过了：a.py 里只有一个 x")])
    runner = AgentRunner(
        tmp_path,
        session="bridge-test",
        extra_args=(
            "--provider", "fake",
            "--script", str(script),
            "--policy", "auto",
            "--no-memory",
            "--max-steps", "2",
        ),
        timeout=120.0,
    )

    answer = runner("看看 a.py")

    assert "看过了" in answer


def test_模型起不来时给一句人话(tmp_path: Path) -> None:
    """子进程崩了也要有答复：聊天通道里"没有反应"是最糟的结果。"""
    runner = AgentRunner(
        tmp_path,
        extra_args=("--provider", "llamacpp", "--base-url", "http://127.0.0.1:1", "--no-memory"),
        timeout=60.0,
    )

    answer = runner("做事")

    assert answer  # 有话说，不是空字符串
    assert "跑" in answer or "失败" in answer or "连不上" in answer or "退出" in answer


def test_超时会回执_而不是干等(tmp_path: Path) -> None:
    """长任务可能跑十几分钟；超时要回一句"我先不等了"，而不是空白。"""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    # 假模型脚本只给一轮，但给两个 turn 让子进程有活干；clock 造一个"已经超时"。
    script = _script(tmp_path, [_turn("一"), _turn("二")])
    ticks = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0])
    runner = AgentRunner(
        tmp_path,
        extra_args=(
            "--provider", "fake", "--script", str(script), "--no-memory", "--max-steps", "2",
        ),
        timeout=10.0,
        clock=lambda: next(ticks, 1000.0),
        sleep=lambda _seconds: None,
    )

    answer = runner("做事")

    assert "超过" in answer or "没有产出" in answer
class _StubRunner:
    """顶替真的 Runner：只回答"我现在在等确认吗"。"""

    def __init__(self, awaiting: int = 0) -> None:
        self.awaiting = awaiting
        self.started: list[str] = []
        self.confirmed: list[bool] = []

    def start(self, message: str) -> bool:
        self.started.append(message)
        return True

    def snapshot(self) -> dict:
        return {
            "awaiting": self.awaiting,
            "finished": self.awaiting == 0,
            "final": {"text": "好"},
        }

    def confirm(self, apply: bool) -> bool:
        self.confirmed.append(apply)
        self.awaiting = 0
        return True


def test_等确认时回_y_是回答而不是新任务():
    """提示语说"回 y 应用、n 丢弃"，那就真的得是"回答"。

    原先 CLI 那条循环把每条消息都当新目标：你回一个 `y`，它拿着目标 "y" 又跑
    一轮 agent，而真正在等确认的那一轮一直卡着——症状是"它好像在工作，但做的
    不是我说的那件事"。
    """
    stub = _StubRunner(awaiting=2)
    runner = AgentRunner(Path("."), sleep=lambda _: None)
    runner._runner = stub

    assert runner("y") == "已应用。"
    assert stub.confirmed == [True]
    assert stub.started == []          # 没有拿 "y" 去开新任务

    runner._runner = _StubRunner(awaiting=1)
    assert runner("丢弃") == "已丢弃。"


def test_没有在等确认时_y_就是普通目标(monkeypatch):
    """别把正常的词吃掉：没人等确认时，`y` 该照常当一句话送进 agent。"""
    import spoolkit.bridge.agent_runner as module

    stub = _StubRunner(awaiting=0)
    monkeypatch.setattr(module, "Runner", lambda *a, **k: stub)
    runner = AgentRunner(Path("."), sleep=lambda _: None)

    assert runner("y") == "好"
    assert stub.started == ["y"]


# --- 一条通道服务整台机器：按 (工作区, 会话) 各复用一个 Runner ---


class _StubProcess:
    def __init__(self, root, session="", extra_args=()) -> None:
        self.root, self.session = str(root), session
        self.started: list[str] = []
        self.awaiting = 0

    def start(self, message: str) -> bool:
        self.started.append(message)
        return True

    def snapshot(self) -> dict:
        return {
            "awaiting": self.awaiting,
            "finished": self.awaiting == 0,
            "final": {"text": f"{self.session}看过"},
        }

    def confirm(self, apply: bool) -> bool:
        self.awaiting = 0
        return True


def _target(root: Path, session: str):
    from spoolkit.kernel.target import Target

    return Target(
        root=root, session=session, workspace_source="pointer", session_source="pointer"
    )


def test_每条聊天各复用一个runner(tmp_path: Path, monkeypatch) -> None:
    """`start()` 的"已有运行就拒绝"是按实例算的——共用一个实例会让两个聊天
    互相挡住，所以一个 (工作区, 会话) 一个 Runner。"""
    import spoolkit.bridge.agent_runner as module

    made: list[tuple[str, str]] = []

    def _make(root, session="", extra_args=()):
        made.append((str(root), session))
        return _StubProcess(root, session)

    monkeypatch.setattr(module, "Runner", _make)
    a, b = tmp_path / "a", tmp_path / "b"
    targets = {
        "chat:fake:u1": _target(a, "s1"),
        "chat:fake:u2": _target(b, "s2"),
    }
    runner = AgentRunner(targets.__getitem__, sleep=lambda _: None)

    assert runner.for_client("chat:fake:u1")("一") == "s1看过"
    assert runner.for_client("chat:fake:u2")("二") == "s2看过"
    assert runner.for_client("chat:fake:u1")("三") == "s1看过"

    assert made == [(str(a), "s1"), (str(b), "s2")]  # 每个目标只建一次


def test_y_找正在等的那个runner而不是最近那个(tmp_path: Path, monkeypatch) -> None:
    """两个聊天同时在跑时，`y` 不能答到"最近说话的那条"上去。"""
    import spoolkit.bridge.agent_runner as module

    stubs: list[_StubProcess] = []

    def _make(root, session="", extra_args=()):
        stub = _StubProcess(root, session)
        stubs.append(stub)
        return stub

    monkeypatch.setattr(module, "Runner", _make)
    a, b = tmp_path / "a", tmp_path / "b"
    targets = {
        "chat:fake:u1": _target(a, "s1"),
        "chat:fake:u2": _target(b, "s2"),
    }
    runner = AgentRunner(targets.__getitem__, sleep=lambda _: None)
    runner.for_client("chat:fake:u1")("一")  # 二者都跑完
    runner.for_client("chat:fake:u2")("二")
    stubs[0].awaiting = 3  # u1 那条在等确认
    stubs[1].awaiting = 0

    assert runner.for_client("chat:fake:u2")("y") == "已应用。"


def test_老形状给路径仍然可用(tmp_path: Path, monkeypatch) -> None:
    """`AgentRunner(路径)` 这条老用法不能被新协议弄坏。"""
    import spoolkit.bridge.agent_runner as module

    seen: list[tuple[str, str]] = []

    def _make(root, session="", extra_args=()):
        seen.append((str(root), session))
        return _StubProcess(root, session)

    monkeypatch.setattr(module, "Runner", _make)
    runner = AgentRunner(tmp_path, session="bridge-test", sleep=lambda _: None)

    assert runner("做事") == "bridge-test看过"
    assert runner.project_root == tmp_path
    assert seen == [(str(tmp_path), "bridge-test")]
