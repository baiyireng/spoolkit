"""桥/聊天那条通道上的「回 y 撤销」（问题④的动作化）。

收尾报告的最后一行现在写着：

    撤销这一轮：spool revert --run <id> （网页里可直接点，桥里回 y）

聊天里的人不会去抄那条命令，所以**说了 `y` 能退就得真的能退**：
`AgentRunner` 在"没有待确认内容、而上一轮留了退路"时，把 `y` 当作撤销，
走的是 `Runner.revert`（与网页壳那枚按钮、与 `spool revert --run` 同一份实现）。

两条边界必须同时守住，这一组就是钉它们：

1. **有改动在等确认时，`y` 永远是「应用」**——那条路排在前面，不许被撤销吃掉；
2. **上一轮没有退路时，`y` 照旧是一句普通的话**（`tests/bridge/test_agent_runner.py`
   里那条"别把正常的词吃掉"钉的是同一个不变量，这里再钉一次带 revert 的替身）。
"""

from pathlib import Path

from spoolkit.bridge.agent_runner import AgentRunner


class _UndoRunner:
    """只回答三件事：在等确认吗 / 上一轮的结局是什么 / 撤销跑成没有。"""

    def __init__(self, run_id: str = "r1", awaiting: int = 0) -> None:
        self.final = {"text": "做完了", "revert_run_id": run_id} if run_id else {"text": "做完了"}
        self.awaiting = awaiting
        self.started: list[str] = []
        self.reverted: list[str] = []

    def start(self, message: str) -> bool:
        self.started.append(message)
        return True

    def snapshot(self) -> dict:
        return {
            "awaiting": self.awaiting,
            "running": False,
            "finished": self.awaiting == 0,
            "final": dict(self.final),
        }

    def confirm(self, apply: bool) -> bool:
        self.awaiting = 0
        return True

    def revert(self, run_id: str = "") -> tuple[int, str]:
        target = run_id or str(self.final.get("revert_run_id") or "")
        self.reverted.append(target)
        return 0, f"已撤销这一轮（{target}）：共 1 处，恢复/删除 1、跳过 0、冲突 0。"


def _runner(stub) -> AgentRunner:
    runner = AgentRunner(Path("."), sleep=lambda _: None)
    runner._runner = stub
    return runner


def test_上一轮有退路时回_y_就是撤销() -> None:
    stub = _UndoRunner(run_id="20261005-000000-000001")
    answer = _runner(stub)("y")

    assert "已撤销这一轮" in answer
    assert stub.reverted == ["20261005-000000-000001"]
    assert stub.started == [], "回 y 被当成了新任务"


def test_中文的_是_也算撤销() -> None:
    stub = _UndoRunner(run_id="r7")
    assert "已撤销这一轮" in _runner(stub)("是")
    assert stub.reverted == ["r7"]


def test_有改动等确认时_y_是应用而不是撤销() -> None:
    stub = _UndoRunner(run_id="r1", awaiting=2)
    assert _runner(stub)("y") == "已应用。"
    assert stub.reverted == [], "还没确认就先把这一轮退回去了"
    assert stub.started == []


def test_上一轮没有退路时_y_照旧是普通目标(monkeypatch) -> None:
    """没有回退命令就没有这条动作：`y` 该照常当一句话送进 agent。"""
    import spoolkit.bridge.agent_runner as module

    stub = _UndoRunner(run_id="")
    monkeypatch.setattr(module, "Runner", lambda *a, **k: stub)
    runner = module.AgentRunner(Path("."), sleep=lambda _: None)

    assert runner("y") == "做完了"
    assert stub.started == ["y"]
    assert stub.reverted == []


def test_撤销没退干净时如实说并给出命令那条路() -> None:
    """有冲突时不许说成"已撤销"：非零退出码照实回话，并点出命令那条路。"""

    class _Conflict(_UndoRunner):
        def revert(self, run_id: str = "") -> tuple[int, str]:
            return 3, "已撤销这一轮（r1）：共 1 处，恢复/删除 0、跳过 0、冲突 1。"

    answer = _runner(_Conflict(run_id="r1"))("y")
    assert "冲突 1" in answer
    assert "spool revert" in answer, "没退干净时没给出命令那条路"
