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
from agents_dev.llm.types import ChatResponse


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


# --- 拆解的分批：步骤数不该被**单次输出预算**卡住 -------------------------
#
# 实测（27B、8192 窗口、50 题）：提示词 4375 token，输出在 2048 处被服务端
# 截断（finish_reason=length），JSON 停在第十八步的半个字符串上，解析出
# 0 步。`run --autonomous --limit 50` 直接回一句「没能拆出任何带验收标准的
# 步骤」——自主路线不是慢，是起不来。


def _batch(*pairs, done: bool = False) -> str:
    return json.dumps(
        {
            "steps": [
                {"goal": g, "acceptance": a, "scope": ["x/"], "executor": "self"}
                for g, a in pairs
            ],
            "done": done,
        },
        ensure_ascii=False,
    )


def test_一次排得完时不加续排提示() -> None:
    """小计划的提示词与「没有分批」时逐字相同——老的测量才还能对照。"""
    gateway = _gateway([_batch(("甲", "a"), ("乙", "b"), done=True)])
    plan = decompose(gateway, "小事", limit=3, max_tokens=2048)
    assert len(plan.steps) == 2
    assert len(gateway.requests) == 1
    assert "已经排好的步骤" not in gateway.requests[0].messages[0].content


def test_排不完就接着排下一批() -> None:
    # 每批 280//140 = 2 步，于是 20 步要排很多批；脚本给两批就够看出接续。
    gateway = _gateway(
        [
            _batch(("甲", "a"), ("乙", "b")),
            _batch(("丙", "c"), ("丁", "d"), done=True),
        ]
    )
    plan = decompose(gateway, "大事", limit=20, max_tokens=280)
    assert [s.goal for s in plan.steps] == ["甲", "乙", "丙", "丁"]
    assert [s.index for s in plan.steps] == [1, 2, 3, 4]
    assert len(gateway.requests) == 2
    second = gateway.requests[1].messages[0].content
    # 续批要看到「已经排好的」并只接着排，不许重排
    assert "已经排好的步骤" in second
    assert "1. 甲" in second
    assert "从第 3 步开始" in second


def test_说排完了就不再问() -> None:
    gateway = _gateway([_batch(("甲", "a"), done=True)])
    plan = decompose(gateway, "小事", limit=20, max_tokens=280)
    assert len(plan.steps) == 1
    assert len(gateway.requests) == 1


def test_空批次结束循环不留死循环() -> None:
    gateway = _gateway(["{}", "{}"])
    plan = decompose(gateway, "小事", limit=20, max_tokens=280)
    assert plan.steps == []
    assert len(gateway.requests) == 1


def test_步骤数只受limit约束不受输出预算约束() -> None:
    """输出预算小到一次只排一步，也照样排满 limit 步。"""
    gateway = _gateway([_batch((f"第{i}步", "通过")) for i in range(1, 6)])
    plan = decompose(gateway, "大事", limit=5, max_tokens=140)
    assert [s.goal for s in plan.steps] == [f"第{i}步" for i in range(1, 6)]


def test_窗口小就少排几步():
    from agents_dev.agents.plan import _batch_size

    # 不知道窗口：只看预算
    assert _batch_size(3072, "目标", "", "（无）", None, 50) == 21
    # 窗口 4096 而材料就占了 4000：留给生成的位置不够，一批只排得下 1 步
    small = _batch_size(3072, "目标", "材料" * 3000, "（无）", 4096, 50)
    assert small == 1


def test_被截断就把这一批砍半重排() -> None:
    """截断说明的是「这批太大」，不是「模型不会排」——所以砍半，不重头来。"""

    class TruncatingFake(FakeModel):
        def __init__(self, script, tokenizer):
            super().__init__(script, tokenizer)
            self.calls = 0

        def chat(self, request):
            self.calls += 1
            if self.calls == 1:
                self.requests.append(request)
                return ChatResponse(
                    text='{"steps": [{"goal": "半截',
                    prompt_tokens=10,
                    completion_tokens=2048,
                    truncated=True,
                )
            return super().chat(request)

    gateway = TruncatingFake(
        [_batch(("甲", "a"), done=True)], OfflineTokenCounter()
    )
    plan = decompose(gateway, "大事", limit=20, max_tokens=2048)
    assert [s.goal for s in plan.steps] == ["甲"]
    assert len(gateway.requests) == 2
    # 第一次问 14 步（2048/140），砍半之后问 7 步
    assert "最多 14 步" in gateway.requests[0].messages[0].content
    assert "最多 7 步" in gateway.requests[1].messages[0].content


def test_步骤提示带上整体位置() -> None:
    plan = parse_plan(_payload(("第一步", "a"), ("第二步", "b")), "总目标")
    plan.mark(1, DONE)
    text = render_step_prompt(plan, plan.steps[1])
    assert "总目标" in text
    assert "已完成：第一步" in text
    assert "本次只做这一步：第二步" in text
    assert "不要顺手做后面步骤的事" in text


def test_已完成清单不会随进度无限膨胀() -> None:
    """这一行随进度线性增长，而**每一轮**都要带着它。

    实测 50 题那次：267 次调用，第 43 步的提示词里这一行已经几百 token，
    而状态块里本来就有一份按同一标定值截断的版本，完整清单在
    .agent/progress.md——这里是重复付款，不是信息。
    """
    raw = _payload(*[(f"第{i}步", "通过") for i in range(1, 31)])
    plan = parse_plan(raw, "总目标", limit=30)
    for step in plan.steps[:20]:
        plan.mark(step.index, DONE)

    text = render_step_prompt(plan, plan.steps[20], done_inline=5)
    assert "20 步已完成" in text
    assert "第16步" in text  # 最近 5 条里有
    assert "第1步" not in text  # 很久以前的，不在这儿摊
    assert "progress.md" in text


def test_已完成不多时照旧全列() -> None:
    plan = parse_plan(_payload(("甲", "a"), ("乙", "b"), ("丙", "c")), "目标")
    plan.mark(1, DONE)
    plan.mark(2, DONE)
    text = render_step_prompt(plan, plan.steps[2], done_inline=5)
    assert "已完成：甲、乙" in text


def test_计划是可变对象但步骤状态可追踪() -> None:
    step = PlanStep(index=1, goal="甲", acceptance="a")
    assert step.status == PENDING
    assert step.finished is False
