import json
from pathlib import Path

from agents_dev.agents.plan import (
    DONE,
    FAILED,
    PENDING,
    Plan,
    PlanStep,
    decompose,
    load_plan,
    parse_plan,
    plan_path,
    render_step_prompt,
    save_plan,
)
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter


def _gateway(script: list[str]) -> FakeModel:
    return FakeModel(script=script, tokenizer=OfflineTokenCounter())


def _payload(*pairs) -> str:
    return json.dumps(
        {"steps": [{"goal": g, "acceptance": a} for g, a in pairs]},
        ensure_ascii=False,
    )


def test_解析出带验收标准的步骤() -> None:
    plan = parse_plan(
        _payload(("建立包结构", "目录存在"), ("实现解析", "pytest 通过")), "做点东西"
    )
    assert [s.goal for s in plan.steps] == ["建立包结构", "实现解析"]
    assert plan.steps[0].acceptance == "目录存在"
    assert plan.steps[0].index == 1


def test_缺少验收标准的步骤被丢弃() -> None:
    raw = json.dumps(
        {"steps": [{"goal": "调研一下", "acceptance": ""}, {"goal": "写代码", "acceptance": "能跑"}]},
        ensure_ascii=False,
    )
    plan = parse_plan(raw, "目标")
    assert [s.goal for s in plan.steps] == ["写代码"]


def test_非法JSON退化为空计划() -> None:
    assert parse_plan("不是 JSON", "目标").steps == []


def test_步骤数量受上限约束() -> None:
    raw = _payload(*[(f"步骤{i}", "通过") for i in range(20)])
    assert len(parse_plan(raw, "目标", limit=3).steps) == 3


def test_取出下一个待办步骤() -> None:
    plan = parse_plan(_payload(("甲", "a"), ("乙", "b")), "目标")
    assert plan.next_pending().goal == "甲"
    plan.mark(1, DONE)
    assert plan.next_pending().goal == "乙"


def test_全部完成后没有待办() -> None:
    plan = parse_plan(_payload(("甲", "a")), "目标")
    plan.mark(1, DONE)
    assert plan.next_pending() is None


def test_失败会阻塞计划而不是被跳过() -> None:
    plan = parse_plan(_payload(("甲", "a"), ("乙", "b")), "目标")
    plan.mark(1, FAILED, note="测试没过")
    blocked = plan.blocked_by()
    assert blocked is not None
    assert blocked.index == 1


def test_没有失败时不被阻塞() -> None:
    plan = parse_plan(_payload(("甲", "a")), "目标")
    plan.mark(1, DONE)
    assert plan.blocked_by() is None


def test_失败发生在中间时后续步骤仍待办但计划被阻塞() -> None:
    plan = parse_plan(_payload(("甲", "a"), ("乙", "b"), ("丙", "c")), "目标")
    plan.mark(1, DONE)
    plan.mark(2, FAILED)
    assert plan.next_pending().index == 3
    assert plan.blocked_by().index == 2


def test_进度可读() -> None:
    plan = parse_plan(_payload(("甲", "a"), ("乙", "b")), "目标")
    plan.mark(1, DONE)
    assert plan.progress() == "1/2 步已完成"


def test_标记不存在的步骤会报错() -> None:
    plan = parse_plan(_payload(("甲", "a")), "目标")
    try:
        plan.mark(9, DONE)
    except KeyError:
        return
    raise AssertionError("应当抛出 KeyError")


def test_渲染包含标记与验收标准() -> None:
    plan = parse_plan(_payload(("甲", "目录存在")), "做大目标")
    plan.mark(1, DONE, note="已完成")
    text = plan.render()
    assert "做大目标" in text
    assert "✓ 1. 甲" in text
    assert "目录存在" in text


def test_失败状态也会被记录() -> None:
    plan = parse_plan(_payload(("甲", "a")), "目标")
    plan.mark(1, FAILED, note="测试没过")
    assert plan.steps[0].finished is True
    assert "测试没过" in plan.render()


def test_计划可持久化并读回(tmp_path: Path) -> None:
    plan = parse_plan(_payload(("甲", "a"), ("乙", "b")), "目标")
    plan.mark(1, DONE, note="做完了")
    path = plan_path(tmp_path)
    save_plan(path, plan)
    restored = load_plan(path)
    assert restored is not None
    assert restored.render() == plan.render()


def test_读取不存在的计划返回空(tmp_path: Path) -> None:
    assert load_plan(plan_path(tmp_path)) is None


def test_分解请求带结构约束与目标() -> None:
    gateway = _gateway([_payload(("甲", "a"))])
    plan = decompose(gateway, "做一个工具")
    assert plan.goal == "做一个工具"
    assert gateway.requests[0].response_schema is not None
    assert "做一个工具" in gateway.requests[0].messages[0].content


def test_步骤提示带上整体位置() -> None:
    plan = parse_plan(_payload(("第一步", "a"), ("第二步", "b")), "总目标")
    plan.mark(1, DONE)
    text = render_step_prompt(plan, plan.steps[1])
    assert "总目标" in text
    assert "已完成：第一步" in text
    assert "本次只做这一步：第二步" in text
    assert "不要顺手做后面步骤的事" in text


def test_计划是可变对象但步骤状态可追踪() -> None:
    step = PlanStep(index=1, goal="甲", acceptance="a")
    assert step.status == PENDING
    assert step.finished is False
