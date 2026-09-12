import json
from pathlib import Path

import pytest

from agents_dev.agents.dispatcher import plan_dispatch, run_delegated
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.edit import PendingChanges, write_file_spec
from agents_dev.tools.fs import read_file_spec
from agents_dev.tools.registry import ToolRegistry


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "结束", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _plan(**overrides) -> str:
    base = {
        "delegate": True,
        "reason": "涉及多个文件",
        "goal": "修复解析逻辑",
        "targets": ["parser.py"],
        "constraints": [],
        "acceptance": "pytest 全绿",
        "out_of_scope": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(write_file_spec(tmp_path, PendingChanges(tmp_path)))
    return registry


def _gateway(script):
    return FakeModel(script=script, tokenizer=OfflineTokenCounter())


def _verdict(passed: bool = True, reasons=None) -> str:
    return json.dumps(
        {
            "verdict": "pass" if passed else "fail",
            "reasons": reasons or ["验收标准满足"],
            "fix_goal": "" if passed else "按审查意见修正",
        },
        ensure_ascii=False,
    )


def _review_script(review_final: str = "审查通过", passed: bool = True) -> list:
    return [_turn("已实现"), _turn(review_final), _verdict(passed)]


def _busy() -> str:
    """一个不结束的回合：还在查，没给结论也没调用工具——用来制造「跑不完」。"""
    return json.dumps(
        {
            "thought": "还在看",
            "tool_calls": [
                {"name": "read_file", "arguments": {"path": "a.py"}}
            ],
            "state": None,
            "done": False,
            "final": None,
        },
        ensure_ascii=False,
    )


def test_不需要派发时直接返回不派发() -> None:
    plan = plan_dispatch(_gateway([_plan(delegate=False, reason="一行改动")]), "改个常量")
    assert plan.delegate is False
    assert plan.reason == "一行改动"


def test_要素齐全时允许派发() -> None:
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    assert plan.delegate is True
    assert plan.spec is not None
    assert plan.spec.acceptance == "pytest 全绿"


def test_缺少验收标准时被闸门拦下() -> None:
    plan = plan_dispatch(_gateway([_plan(acceptance="")]), "修复解析")
    assert plan.delegate is False
    assert "验收标准" in plan.blocked
    assert plan.spec is None


def test_计划不是合法JSON时退回自己完成() -> None:
    plan = plan_dispatch(_gateway(["不是 JSON"]), "修复解析")
    assert plan.delegate is False
    assert plan.reason


def test_计划里没给目标时沿用原任务() -> None:
    plan = plan_dispatch(_gateway([_plan(goal="")]), "原始任务描述")
    assert plan.spec is not None
    assert plan.spec.goal == "原始任务描述"


def test_分派计划请求带结构约束() -> None:
    gateway = _gateway([_plan()])
    plan_dispatch(gateway, "修复解析")
    assert gateway.requests[0].response_schema is not None
    assert "修复解析" in gateway.requests[0].messages[0].content


def test_不含可派发说明时执行直接报错(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    plan = plan_dispatch(_gateway([_plan(delegate=False)]), "改常量")
    with pytest.raises(ValueError):
        run_delegated(
            plan,
            _gateway([]),
            tokenizer,
            _registry(tmp_path),
            Config(project_root=tmp_path, context_window=4096),
        )


def test_先实现后审查并分别记录(tmp_path: Path) -> None:
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    result = run_delegated(
        plan,
        _gateway(_review_script()),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096),
    )
    assert result.implementer_final == "已实现"
    assert result.reviewer_final == "审查通过"
    assert any(line.startswith("[实现 1]") for line in result.trace)
    assert any(line.startswith("[审查 1]") for line in result.trace)
    assert result.review is not None and result.review.passed
    assert result.rejected is False


def test_审查说明里带上验收标准但不含实现者推理(tmp_path: Path) -> None:
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    gateway = _gateway(_review_script())
    run_delegated(
        plan,
        gateway,
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096),
    )
    review_prompt = [
        m for m in gateway.requests[1].messages if m.role == "user"
    ][0].content
    assert "pytest 全绿" in review_prompt
    assert "待审查的改动" in review_prompt


def test_审查者上下文里没有实现者的助手消息(tmp_path: Path) -> None:
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    gateway = _gateway(_review_script())
    run_delegated(
        plan,
        gateway,
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096),
    )
    review_roles = [m.role for m in gateway.requests[1].messages]
    assert "assistant" not in review_roles


def test_审查者拿不到写工具(tmp_path: Path) -> None:
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    gateway = _gateway(_review_script())
    run_delegated(
        plan,
        gateway,
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096),
    )
    reviewer_prompt = gateway.requests[1].messages[0].content
    assert "write_file" not in reviewer_prompt
    assert "replace_lines" not in reviewer_prompt


# --- 审查不通过 → 打回主循环 ---


def test_审查不通过时打回并再派一次修复(tmp_path: Path) -> None:
    """不通过不是终点：主循环看到理由后可以再派一次针对性的修复。"""
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    script = [
        _turn("第一版实现"),
        _turn("还差点意思"),
        _verdict(False, ["没有处理空输入"]),
        _plan(goal="补上空输入处理"),  # 主循环决定再派
        _turn("第二版实现"),
        _turn("这次可以了"),
        _verdict(True),
    ]
    result = run_delegated(
        plan,
        _gateway(script),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096),
    )
    assert result.rounds == 2
    assert result.rejected is False
    assert result.implementer_final == "第二版实现"
    assert any("[打回] 再派一次修复" in line for line in result.trace)


def test_主循环决定不修就停下(tmp_path: Path) -> None:
    """「不通过就重试」不能变成自动无限循环——有些问题重试也是同一个结果。"""
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    script = [
        _turn("第一版实现"),
        _turn("不行"),
        _verdict(False, ["方向不对"]),
        _plan(delegate=False, reason="这个问题重派也是同样的结果"),
    ]
    result = run_delegated(
        plan,
        _gateway(script),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096),
    )
    assert result.rounds == 1
    assert result.rejected is True
    assert any("[打回] 不再修" in line for line in result.trace)


def test_达到轮数上限就停(tmp_path: Path) -> None:
    """上限是硬闸门：模型一直说「再派一次」也不会超出预算。"""
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    script = []
    for _ in range(5):
        script += [_turn("实现"), _turn("不行"), _verdict(False), _plan()]
    result = run_delegated(
        plan,
        _gateway(script),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096, review_rounds=2),
    )
    assert result.rounds == 2
    assert result.rejected is True


def test_判定解析不出来按未通过处理(tmp_path: Path) -> None:
    """含糊的通过等于没有审查。"""
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    script = [
        _turn("实现"),
        _turn("嗯，还行吧"),
        "不是 JSON",
        _plan(delegate=False, reason="先不修"),
    ]
    result = run_delegated(
        plan,
        _gateway(script),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096),
    )
    assert result.rejected is True


def test_子智能体拿到自动验证(tmp_path: Path) -> None:
    """审查者本来就有 run_command，但它几乎从不主动跑——得替它接上。"""
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    seen = []

    def verify(changed=()):
        seen.append(1)
        from agents_dev.tools.types import ToolResult

        return ToolResult(ok=True, content="通过")

    # 必须真的改一次文件：验证只在「有过改动」之后才跑，
    # 纯结论性的回合不该顺带跑测试。
    edit = json.dumps(
        {
            "thought": "写文件",
            "tool_calls": [
                {
                    "name": "write_file",
                    "arguments": {"path": "a.py", "content": "x = 1\n"},
                }
            ],
            "state": None,
            "done": False,
            "final": None,
        },
        ensure_ascii=False,
    )
    run_delegated(
        plan,
        _gateway([edit, _turn("改完了"), _turn("审查通过"), _verdict(True)]),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096),
        verify=verify,
    )
    assert seen, "子智能体应当拿到自动验证"


def test_审查者没跑完不算判不通过(tmp_path: Path) -> None:
    """审查者自己撞上限时，它的 final 是「已达步数上限」。

    把这句话当成「审查判定不合格」会把一处正确的实现判死——实测第一次
    真实运行就是这样：三个文件都改对了，却被打回重做一轮。
    """
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    # 子智能体只有 1 步预算：实现者一步做完，审查者一步做不完
    script = [
        _turn("实现完了"),
        _busy(),
    ]
    result = run_delegated(
        plan,
        _gateway(script),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096, subagent_steps=1),
        max_rounds=2,
    )
    assert result.review is not None
    assert result.review.inconclusive is True
    assert result.rejected is False, "没结论不等于判不通过"
    assert result.rounds == 1, "审查没结论就不该再派修复"
    assert any("审查无结论" in line for line in result.trace)


def test_子角色不覆盖主循环的检查点(tmp_path: Path) -> None:
    """主循环和子智能体跑在同一个工作区，共用 task.json 会让进度互相覆盖。

    实测：长任务里派发一次，主循环的检查点就变成了子智能体的状态——
    崩溃或续跑时读到的会是别人的进度。
    """
    from agents_dev.agents.runtime import IMPLEMENTER, TaskSpec, run_role

    checkpoint = tmp_path / ".agent" / "tasks" / "task.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text('{"marker": "主循环的进度"}', encoding="utf-8")

    run_role(
        IMPLEMENTER,
        TaskSpec(goal="改一处", acceptance="跑通"),
        _gateway([_turn("改完了")]),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096, supervise=False),
    )
    assert "主循环的进度" in checkpoint.read_text(encoding="utf-8")


def test_实现者说太大就不送审(tmp_path: Path) -> None:
    """子智能体有权说「这件事我独立做不完」，而且该早说。

    实测那次批量派发：实现者烧光预算、撞上重复保护，回来的信号无从行动；
    而主循环需要的是「这块对它太大，拆小再派」。
    """
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    result = run_delegated(
        plan,
        _gateway(
            [
                _turn(
                    "【太大】要改 40 个文件，我一轮只有 20 步。"
                    "建议拆成 01-10 / 11-20 两批，先做第一批。"
                )
            ]
        ),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096, supervise=False),
    )
    assert "太大" in result.too_big
    assert result.review is None, "规模问题不该送去审查——那是在审一个半成品"
    assert any("不送审" in line for line in result.trace)


def test_实现者撞上限也不送审(tmp_path: Path) -> None:
    """没做完就送审，会把「任务太大」误报成「实现不合格」。"""
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    looking = json.dumps(
        {
            "thought": "先看看",
            "tool_calls": [{"name": "read_file", "arguments": {"path": "x.py"}}],
            "state": None,
            "done": False,
            "final": None,
        },
        ensure_ascii=False,
    )
    result = run_delegated(
        plan,
        _gateway([looking] * 3),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(
            project_root=tmp_path,
            context_window=4096,
            subagent_steps=1,
            supervise=False,
        ),
    )
    assert result.too_big
    assert "步预算" in result.too_big
    assert result.review is None


def test_派发战绩记在共用内核里(tmp_path: Path) -> None:
    """派发有两条路（会话内的 dispatch 工具、计划里 executor=subagent 的步骤）。

    记账写在工具那一层，另一条路就永远是空的——实测就是这么漏掉的
    （计划路径跑完一步，日志文件根本没生成）。所以记在 run_delegated 里。
    """
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    run_delegated(
        plan,
        _gateway(_review_script("审查通过", passed=True)),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096, supervise=False),
    )
    log = tmp_path / ".agent" / "dispatch-log.md"
    assert log.exists(), "直接调 run_delegated 也要记上"
    assert "[通过]" in log.read_text(encoding="utf-8")
