"""越界被丢掉的改动，必须进这一步的记录。

实测代价（一个真实需求：新增消息桥）：第 3 步要新增 CLI 子命令，得同时改
`cli/app.py` 来注册；而它声明的范围写成了**不存在的** `cli.py`。于是那两处
改动被范围闸门丢掉，模型却只看到"自动验证失败"——它不知道自己的写入根本没
落盘，于是"写 → 验证失败 → 重置 → 再写"，转到督导叫停（12 步、57.6k token）。

记进 note 之后，计划渲染、回头看的总结、续排的提示词都看得到这件事。
"""

import argparse
from pathlib import Path

from spoolkit.cli.commands.plan import record_step
from spoolkit.agents.plan import Plan, PlanStep, load_plan, plan_path, save_plan
from spoolkit.cli.commands.plan import StepRun
from spoolkit.tools.edit import PendingChanges
from spoolkit.agent.loop import LoopResult
from spoolkit.agent.state import TaskState


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(tmp_path), policy="auto", scope="**", no_memory=True, session="cli"
    )


def _result(finished: bool = True) -> LoopResult:
    return LoopResult(
        finished=finished,
        final="做完了",
        state=TaskState(task_id="t", goal="g"),
        steps=1,
        resets=0,
    )


def test_越界被丢弃会写进这一步的记录(tmp_path: Path) -> None:
    plan = Plan(
        goal="新增桥",
        steps=[
            PlanStep(
                index=1,
                goal="新增 CLI 子命令",
                acceptance="跑通",
                scope=("src/spoolkit/cli/commands/bridge.py",),
            )
        ],
    )
    save_plan(plan_path(tmp_path), plan)
    pending = PendingChanges(tmp_path)
    (tmp_path / "src" / "spoolkit" / "cli").mkdir(parents=True)
    (tmp_path / "src" / "spoolkit" / "cli" / "app.py").write_text("x = 1\n", encoding="utf-8")
    pending.propose("src/spoolkit/cli/app.py", "x = 2\n")

    ctx = StepRun(
        args=_args(tmp_path),
        project_root=tmp_path,
        gateway=None,
        plan=plan,
        non_interactive=True,
    )
    record_step(ctx, plan.steps[0], _result(), pending, scope=("src/spoolkit/cli/commands/bridge.py",))

    note = load_plan(plan_path(tmp_path)).steps[0].note
    assert "超出本步范围被丢弃" in note
    assert "cli/app.py" in note
    assert not (tmp_path / "src" / "spoolkit" / "cli" / "app.py").read_text(
        encoding="utf-8"
    ) == "x = 2\n"  # 真的没落盘


def test_范围内的改动不写那条备注(tmp_path: Path) -> None:
    plan = Plan(
        goal="改一处",
        steps=[PlanStep(index=1, goal="改 a", acceptance="过", scope=("src/a.py",))],
    )
    save_plan(plan_path(tmp_path), plan)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    pending.propose("src/a.py", "x = 2\n")

    ctx = StepRun(
        args=_args(tmp_path), project_root=tmp_path, gateway=None, plan=plan,
        non_interactive=True,
    )
    record_step(ctx, plan.steps[0], _result(), pending, scope=("src/a.py",))

    note = load_plan(plan_path(tmp_path)).steps[0].note
    assert "被丢弃" not in note
    assert (tmp_path / "src" / "a.py").read_text(encoding="utf-8") == "x = 2\n"
