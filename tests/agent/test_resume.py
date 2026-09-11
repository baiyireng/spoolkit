import json
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.agent.state import TaskState, save_state
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.registry import ToolRegistry


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "结束", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _loop(tmp_path: Path, script: list[str]) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=ToolRegistry(),
        config=Config(project_root=tmp_path, context_window=4096, max_steps=5),
    )


def test_不恢复时从零开始(tmp_path: Path) -> None:
    save_state(
        TaskState(task_id="task", goal="旧任务", done=["第一步"], step=3),
        tmp_path / ".agent" / "tasks" / "task.json",
    )
    result = _loop(tmp_path, [_turn("完成")]).run("新任务")
    # goal 来自参数，而不是检查点
    assert result.state.goal == "新任务"


def test_恢复时沿用检查点里的目标与进度(tmp_path: Path) -> None:
    save_state(
        TaskState(task_id="task", goal="未完成的任务", done=["第一步"], step=3),
        tmp_path / ".agent" / "tasks" / "task.json",
    )
    result = _loop(tmp_path, [_turn("完成")]).run("被忽略的新目标", resume=True)
    assert result.state.goal == "未完成的任务"
    assert result.state.done == ["第一步"]
    # 从第 3 步接着算，不是从 0
    assert result.steps == 4


def test_恢复时提示里说明是接着做(tmp_path: Path) -> None:
    save_state(
        TaskState(task_id="task", goal="旧任务", step=2),
        tmp_path / ".agent" / "tasks" / "task.json",
    )
    loop = _loop(tmp_path, [_turn("完成")])
    loop.run("旧任务", resume=True)
    users = [m.content for m in loop.gateway.requests[0].messages if m.role == "user"]
    assert "继续之前未完成的任务" in users[0]


def test_没有检查点时恢复等于新建(tmp_path: Path) -> None:
    result = _loop(tmp_path, [_turn("完成")]).run("全新任务", resume=True)
    assert result.finished is True
    assert result.state.goal == "全新任务"


def test_成功后检查点被清掉(tmp_path: Path) -> None:
    _loop(tmp_path, [_turn("完成")]).run("做完")
    assert not (tmp_path / ".agent" / "tasks" / "task.json").exists()


def test_未完成时检查点被保留(tmp_path: Path) -> None:
    loop = _loop(tmp_path, ["不是 JSON"] * 10)
    result = loop.run("做不完")
    assert result.finished is False
    path = tmp_path / ".agent" / "tasks" / "task.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["goal"] == "做不完"


def test_续跑未完成的任务能接着完成(tmp_path: Path) -> None:
    # 第一次撞上限，留下检查点
    _loop(tmp_path, ["不是 JSON"] * 10).run("两段式任务")
    # 第二次带上 resume，应当能读到进度并完成
    result = _loop(tmp_path, [_turn("终于完成")]).run("两段式任务", resume=True)
    assert result.finished is True
    assert result.state.goal == "两段式任务"
    assert result.steps > 5
