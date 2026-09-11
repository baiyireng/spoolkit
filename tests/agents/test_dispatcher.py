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
        _gateway([_turn("已实现"), _turn("审查通过")]),
        OfflineTokenCounter(),
        _registry(tmp_path),
        Config(project_root=tmp_path, context_window=4096),
    )
    assert result.implementer_final == "已实现"
    assert result.reviewer_final == "审查通过"
    assert any(line.startswith("[实现]") for line in result.trace)
    assert any(line.startswith("[审查]") for line in result.trace)


def test_审查说明里带上验收标准但不含实现者推理(tmp_path: Path) -> None:
    plan = plan_dispatch(_gateway([_plan()]), "修复解析")
    gateway = _gateway([_turn("已实现"), _turn("审查通过")])
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
    gateway = _gateway([_turn("已实现"), _turn("审查通过")])
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
    gateway = _gateway([_turn("已实现"), _turn("审查通过")])
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
