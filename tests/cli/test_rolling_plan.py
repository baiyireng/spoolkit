"""分批推进：先做一批 → 回头看一眼 → 再排下一批。

存在的理由：一次排 50 步，等于把"实际会怎样"排除在决策之外。真实任务里，
前三步做完之后你往往才知道后面该怎么做（某个接口不是那样、某个约束计划里
没料到）。分批推进让**每一批都吃到前一批的实际结果**。

代价也要钉住：计划的总生成量并不因此变少（步骤数没变），反而多出每批一次
的"回头看"调用——它换的是更准的后续计划，不是更少的 token。
"""

import argparse
import json
from pathlib import Path

from spoolkit.agents.plan import DONE, Plan, PlanStep, extend_plan, reflect_progress
from spoolkit.cli.commands.plan import autonomous
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter


def _plan(goals: list[tuple[str, str]]) -> str:
    return json.dumps(
        {
            "steps": [
                {
                    "goal": goal,
                    "acceptance": acceptance,
                    "scope": [goal.split("/")[0] + "/"],
                    "executor": "self",
                }
                for goal, acceptance in goals
            ]
        },
        ensure_ascii=False,
    )


def _more(goals: list[tuple[str, str]], done: bool = False) -> str:
    payload = json.loads(_plan(goals))
    payload["done"] = done
    return json.dumps(payload, ensure_ascii=False)


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "收尾", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _args(tmp_path: Path, **kwargs) -> argparse.Namespace:
    base = dict(
        goal="把工作区里的题都做对",
        scope="**",
        cover="",
        limit=10,
        batch=2,
        no_roll=False,
        policy="auto",
        window=4096,
        max_steps=2,
        subagent_steps=2,
        no_memory=True,
        session="cli",
        provider="fake",
        model="",
        base_url="",
        proxy="",
        script="",
        no_supervise=False,
        step_ceiling=0,
        allow_read=[],
        root=str(tmp_path),
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_回头看把实际结果喂给总结(tmp_path: Path) -> None:
    plan = Plan(
        goal="把题做对",
        steps=[
            PlanStep(index=1, goal="改 01", acceptance="过", status=DONE, note="改好了"),
            PlanStep(index=2, goal="改 02", acceptance="过", status=DONE, note="测试仍失败"),
        ],
    )
    gateway = FakeModel(
        script=[
            json.dumps(
                {
                    "summary": "做到两步；第二步没成；下一步先看测试。",
                    "lessons": [
                        {
                            "rule": "这类题的验收测试固定了对外接口，动手前先读它",
                            "trigger": "pytest,验收,测试",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        ],
        tokenizer=OfflineTokenCounter(),
    )

    summary, lessons = reflect_progress(gateway, plan.goal, plan.steps)

    assert "第二步没成" in summary
    assert lessons == [("这类题的验收测试固定了对外接口，动手前先读它", "pytest,验收,测试")]
    prompt = gateway.requests[0].messages[0].content
    assert "改 01" in prompt and "改好了" in prompt
    assert "测试仍失败" in prompt


def test_没有完成的步骤时不问(tmp_path: Path) -> None:
    gateway = FakeModel(script=[], tokenizer=OfflineTokenCounter())
    plan = Plan(goal="x", steps=[PlanStep(index=1, goal="甲", acceptance="过")])
    assert reflect_progress(gateway, "x", plan.steps) == ("", [])
    assert gateway.requests == []


def test_回头看解析不出来也不挡住推进() -> None:
    """格式坏了退回「摘要=原文、教训为空」——回头看不能把整次运行停在这。"""
    from spoolkit.agents.plan import _parse_reflection

    assert _parse_reflection("不是 JSON 的一段总结") == ("不是 JSON 的一段总结", [])
    summary, lessons = _parse_reflection(
        json.dumps({"summary": "还行", "lessons": [{"rule": "没有触发词"}]})
    )
    assert summary == "还行"
    assert lessons == []  # 没触发词的教训永远不会被推出来，存了等于没存


def test_教训最多留两条() -> None:
    from spoolkit.agents.plan import _parse_reflection

    payload = json.dumps(
        {
            "summary": "s",
            "lessons": [
                {"rule": "甲", "trigger": "a"},
                {"rule": "乙", "trigger": "b"},
                {"rule": "丙", "trigger": "c"},
            ],
        },
        ensure_ascii=False,
    )
    _, lessons = _parse_reflection(payload)
    assert lessons == [("甲", "a"), ("乙", "b")]


def test_回头看得来的教训进库_并能被后面的同类任务命中(tmp_path: Path) -> None:
    """这是「在任务中成长」最直接的一条落地：

    回头看写下的教训存进教训库，之后**同类任务开局就会被主动推送**。
    """
    from spoolkit.cli.commands.plan import _keep_lessons
    from spoolkit.cli.runtime import build_memory
    from spoolkit.memory.lessons import match_lessons

    memory = build_memory(tmp_path, 4096, "cli")
    stored = _keep_lessons(
        [("动手前先读验收测试：它固定了对外接口", "pytest,验收,测试")], memory
    )

    assert stored == 1
    hits = match_lessons(
        memory._conn, "把工作区里的题都做对，每道题跑 python -m pytest -q 通过"
    )
    assert [item.text for item in hits] == ["动手前先读验收测试：它固定了对外接口"]
    # 话题不相关的任务不该被推
    assert match_lessons(memory._conn, "把这份报告翻译成英文") == []


def test_没有记忆会话时教训不落库(tmp_path: Path) -> None:
    """不写记忆的运行不该偷偷留下状态。"""
    from spoolkit.cli.commands.plan import _keep_lessons

    assert _keep_lessons([("甲", "a")], None) == 0


def test_续排提示里带上回头看与已完成清单() -> None:
    plan = Plan(
        goal="把题做对",
        steps=[PlanStep(index=1, goal="改 01", acceptance="过", status=DONE)],
    )
    gateway = FakeModel(script=[_more([("改 02/conf.py", "过")])], tokenizer=OfflineTokenCounter())

    fresh = extend_plan(gateway, plan, "第一步用的是 conf.py，不是 settings.py")

    assert [step.goal for step in fresh] == ["改 02/conf.py"]
    prompt = gateway.requests[0].messages[0].content
    assert "第一步用的是 conf.py" in prompt
    assert "1. 改 01（done）" in prompt
    assert "从第 2 步开始" in prompt


def test_它说做完了就返回空() -> None:
    plan = Plan(goal="x", steps=[PlanStep(index=1, goal="甲", acceptance="过", status=DONE)])
    gateway = FakeModel(script=[_more([], done=True)], tokenizer=OfflineTokenCounter())
    assert extend_plan(gateway, plan, "做完了") == []


def test_自主模式分批执行并续排(tmp_path: Path, capsys) -> None:
    """两批：先排 2 步做完，回头看一眼，再排 1 步。"""
    script = [
        _plan([("01_a/x.py", "过"), ("02_b/y.py", "过")]),
        _turn("改好了 01"),
        _turn("改好了 02"),
        json.dumps({"summary": "第一批都做完了；没有意外。"}, ensure_ascii=False),
        _more([("03_c/z.py", "过")]),
        _turn("改好了 03"),
        json.dumps({"summary": "三件都做完了。"}, ensure_ascii=False),
        _more([], done=True),
    ]
    gateway = FakeModel(script=script, tokenizer=OfflineTokenCounter())

    assert autonomous(_args(tmp_path), tmp_path, gateway) == 0

    out = capsys.readouterr().out
    assert "回头看这一批：第一批都做完了" in out
    assert "续排：模型认为目标已经排完" in out
    assert "自主运行结束：完成 3/3 步" in out


def test_no_roll_时一次排完(tmp_path: Path, capsys) -> None:
    script = [
        _plan([("01_a/x.py", "过"), ("02_b/y.py", "过")]),
        _turn("改好了 01"),
        _turn("改好了 02"),
    ]
    gateway = FakeModel(script=script, tokenizer=OfflineTokenCounter())

    assert autonomous(_args(tmp_path, no_roll=True), tmp_path, gateway) == 0

    out = capsys.readouterr().out
    assert "回头看这一批" not in out
    assert "自主运行结束：完成 2/2 步" in out


def test_第一批的提示词说清粒度不变(tmp_path: Path) -> None:
    """只说「最多 2 步」，模型会把多件事并成一条步骤——实测那一步跑了 191 秒。

    所以「批的大小」和「步的粒度」必须在提示词里分开说。
    """
    script = [
        _plan([("01_a/x.py", "过"), ("02_b/y.py", "过")]),
        _turn("改好了 01"),
        _turn("改好了 02"),
        "都做完了。",
        _more([], done=True),
    ]
    gateway = FakeModel(script=script, tokenizer=OfflineTokenCounter())

    autonomous(_args(tmp_path), tmp_path, gateway)

    first = gateway.requests[0].messages[0].content
    assert "每一步的粒度不变" in first
    assert "最多排 2 步" in first
