"""会话之间不许串味。

原先 `plan.json` / `progress.md` / `tasks/*.json` 都是**工作区级**的：同一个
工作区里换个会话，读到的还是上一个会话的计划与进度。真实后果见过两次——QQ 那轮
让 agent 找小说，它读到别的任务（`scratch_lab` 的 percent 函数）的残留计划，
最后一条答复答非所问。

这组用例钉住"换个会话就是换一套状态"，而不只是钉住路径字符串。
"""

import json
from pathlib import Path

from spoolkit import session_state
from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.registry import ToolRegistry


def _unfinished() -> list[str]:
    """一轮就跑不完的脚本：让它把检查点留下。"""
    return ["不是 JSON"] * 5


def _loop(tmp_path: Path, session: str) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    return AgentLoop(
        gateway=FakeModel(script=_unfinished(), tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=ToolRegistry(),
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=2,
            session=session,
        ),
    )


def test_路径本来就分开(tmp_path: Path) -> None:
    assert session_state.plan_path(tmp_path, "a") != session_state.plan_path(tmp_path, "b")
    assert (
        session_state.progress_path(tmp_path, "a")
        != session_state.progress_path(tmp_path, "b")
    )
    assert (
        session_state.task_path(tmp_path, "a", "task")
        != session_state.task_path(tmp_path, "b", "task")
    )
    # 中文会话名也要能落成合法目录名
    assert session_state.dir_for(tmp_path, "找小说 / 第二轮").is_relative_to(
        tmp_path / ".agent" / "sessions"
    )


def test_两个会话的检查点互不覆盖(tmp_path: Path) -> None:
    """同一个工作区、两个会话各留一份未完成的检查点——两条都要在，而且各自是
    自己的目标。原先它们写同一个 `tasks/task.json`，后跑的会盖掉先跑的。"""
    _loop(tmp_path, "a").run("任务A")
    _loop(tmp_path, "b").run("任务B")

    state_a = json.loads(
        session_state.task_path(tmp_path, "a", "task").read_text(encoding="utf-8")
    )
    state_b = json.loads(
        session_state.task_path(tmp_path, "b", "task").read_text(encoding="utf-8")
    )

    assert state_a["goal"] == "任务A"
    assert state_b["goal"] == "任务B"


def test_一个会话读不到另一个会话的计划(tmp_path: Path) -> None:
    from spoolkit.agents.plan import parse_plan, load_plan, plan_path, save_plan

    plan = parse_plan(
        json.dumps(
            {
                "steps": [
                    {"goal": "甲", "acceptance": "a", "scope": ["x"], "executor": "self"}
                ]
            },
            ensure_ascii=False,
        ),
        "只属于 a 的目标",
    )
    save_plan(plan_path(tmp_path, "a"), plan)

    assert load_plan(plan_path(tmp_path, "a")) is not None
    assert load_plan(plan_path(tmp_path, "b")) is None
