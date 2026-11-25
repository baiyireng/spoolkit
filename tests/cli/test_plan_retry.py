"""advance_plan 遇到 failed 步骤时自动重试一次。

dogfood 暴露：步骤失败后 resume 阻塞，没有重试机制——很多失败是模型
一次性抽风（patch 格式错），重试就能过。修复：retries < 1 时自动改回
pending 重跑，再失败才阻塞。
"""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from spoolkit.agents.plan import DONE, FAILED, PENDING, Plan, PlanStep, save_plan, plan_path
from spoolkit.cli.commands.plan import advance_plan


def _args(tmp_path: Path, **kwargs) -> argparse.Namespace:
    base = dict(
        root=str(tmp_path),
        session="test-retry",
        provider="fake",
        model="",
        base_url="",
        proxy="",
        script="",
        window=4096,
        max_steps=2,
        step_ceiling=0,
        subagent_steps=2,
        no_memory=True,
        no_supervise=False,
        scope="**",
        cover="",
        limit=10,
        batch=0,
        no_roll=False,
        policy="auto",
        allow_read=[],
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def _save_failed_plan(tmp_path: Path, retries: int = 0) -> None:
    """存一个第 1 步已失败的计划。"""
    plan = Plan(
        goal="测试目标",
        steps=[
            PlanStep(
                index=1, goal="第一步", acceptance="过",
                scope=("**",), status=FAILED, note="上次失败了", retries=retries,
            ),
            PlanStep(
                index=2, goal="第二步", acceptance="过",
                scope=("**",), status=PENDING,
            ),
        ],
    )
    save_plan(plan_path(tmp_path, "test-retry"), plan)


def test_failed步骤自动重试一次(tmp_path: Path, capsys) -> None:
    """retries=0 的 failed 步骤被改回 pending 重跑。"""
    _save_failed_plan(tmp_path, retries=0)
    args = _args(tmp_path)
    gateway = object()

    call_count = [0]

    def fake_execute(ctx, step, scope, **kw):
        call_count[0] += 1
        # 模拟 LoopResult
        from spoolkit.agent.loop import LoopResult
        from spoolkit.agent.state import TaskState
        result = LoopResult(
            finished=True, final="这次成功了",
            state=TaskState(task_id="t", goal=step.goal),
            steps=1, resets=0, trace=[], prompt_tokens=0,
            completion_tokens=0, model_calls=0,
        )
        return result, None  # pending=None

    with patch("spoolkit.cli.commands.plan.execute_step", side_effect=fake_execute), \
         patch(
             "spoolkit.cli.commands.plan.record_step",
             # record_step 返回 `(结算结果, 撤销命令)`，两样都进 FINAL 事件。
             return_value=("none", ""),
         ):
        rc = advance_plan(args, tmp_path, gateway)

    assert rc == 0
    assert call_count[0] == 1  # 重试了一次
    # 「自动重试」这句是人读的进度：计划路径上散文一律走 stderr（D3），
    # stdout 是事件通道（`--events` 时一行一个 JSON）。
    out = capsys.readouterr().err
    assert "自动重试" in out
    assert "第 1 次" in out


def test_已重试过仍失败则阻塞(tmp_path: Path, capsys) -> None:
    """retries=1 的 failed 步骤不再重试，直接阻塞。"""
    _save_failed_plan(tmp_path, retries=1)
    args = _args(tmp_path)
    gateway = object()

    with patch("spoolkit.cli.commands.plan.execute_step") as fake_exec:
        rc = advance_plan(args, tmp_path, gateway)

    assert rc == 1
    assert fake_exec.call_count == 0  # 没有重试
    err = capsys.readouterr().err
    assert "已自动重试 1 次仍未通过" in err


def test_重试后retries递增(tmp_path: Path) -> None:
    """重试后 plan 文件里 retries 变成 1。"""
    _save_failed_plan(tmp_path, retries=0)
    args = _args(tmp_path)
    gateway = object()

    def fake_execute(ctx, step, scope, **kw):
        from spoolkit.agent.loop import LoopResult
        from spoolkit.agent.state import TaskState
        result = LoopResult(
            finished=False, final="又失败了",
            state=TaskState(task_id="t", goal=step.goal),
            steps=1, resets=0, trace=[], prompt_tokens=0,
            completion_tokens=0, model_calls=0,
        )
        return result, None

    with patch("spoolkit.cli.commands.plan.execute_step", side_effect=fake_execute), \
         patch(
             "spoolkit.cli.commands.plan.record_step",
             return_value=("none", ""),
         ):
        advance_plan(args, tmp_path, gateway)

    # 重新加载计划，验证 retries 递增了
    from spoolkit.agents.plan import load_plan
    plan = load_plan(plan_path(tmp_path, "test-retry"))
    assert plan.steps[0].retries == 1
