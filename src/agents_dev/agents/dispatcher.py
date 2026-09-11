"""分派器：决定这次改动由主循环自己做，还是交给子智能体。

判断不由启发式规则做，而是让模型产出一份**结构化任务说明**，再由闸门决定
能不能派发。理由是启发式只能看改动规模，看不出「这次改动是否值得独立上下文」；
而结构化说明顺带解决了另一件事——它就是子智能体唯一的信息来源。

闸门是关键：没有验收标准就不派发。没有可执行的判断依据，实现者做到什么
程度都算完成，审查者也无从判断，整条流水线会退化成两个模型互相附和。
"""

import json
from dataclasses import dataclass, field
from typing import Any

from agents_dev.agents.runtime import (
    IMPLEMENTER,
    REVIEWER,
    TaskSpec,
    run_role,
)
from agents_dev.config import Config
from agents_dev.context import templates as T
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.types import ChatRequest, Message
from agents_dev.tools.registry import ToolRegistry

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "delegate": {"type": "boolean"},
        "reason": {"type": "string"},
        "goal": {"type": "string"},
        "targets": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "acceptance": {"type": "string"},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["delegate", "reason", "goal"],
}

PLAN_PROMPT = T.DISPATCH


@dataclass(frozen=True)
class DispatchPlan:
    """一次分派决策。"""

    delegate: bool
    reason: str
    spec: TaskSpec | None = None
    blocked: str = ""


def _parse_plan(text: str, goal: str) -> DispatchPlan:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return DispatchPlan(False, "无法解析分派计划，改为自己完成")
    if not isinstance(payload, dict):
        return DispatchPlan(False, "分派计划格式不对，改为自己完成")

    reason = str(payload.get("reason", "")).strip()
    if not payload.get("delegate"):
        return DispatchPlan(False, reason or "不需要派发")

    spec = TaskSpec(
        goal=str(payload.get("goal") or goal).strip(),
        targets=tuple(payload.get("targets") or ()),
        constraints=tuple(payload.get("constraints") or ()),
        acceptance=str(payload.get("acceptance") or "").strip(),
        out_of_scope=tuple(payload.get("out_of_scope") or ()),
    )
    problem = spec.validate()
    if problem is not None:
        # 闸门拦下就退回自己完成，而不是带着缺验收标准的说明去派发。
        return DispatchPlan(False, f"分派被拦下：{problem}", blocked=problem)
    return DispatchPlan(True, reason, spec=spec)


def plan_dispatch(
    gateway: ModelGateway,
    goal: str,
    context: str = "",
    max_tokens: int = 1024,
) -> DispatchPlan:
    """让模型产出分派计划，并经闸门校验。"""
    response = gateway.chat(
        ChatRequest(
            messages=(
                Message(
                    role="user",
                    content=PLAN_PROMPT.format(goal=goal, context=context or "无"),
                ),
            ),
            max_tokens=max_tokens,
            response_schema=PLAN_SCHEMA,
        )
    )
    return _parse_plan(response.text, goal)


@dataclass
class DelegatedResult:
    """一次分派执行的完整结果。"""

    plan: DispatchPlan
    implementer_final: str = ""
    reviewer_final: str = ""
    trace: list[str] = field(default_factory=list)


def run_delegated(
    plan: DispatchPlan,
    gateway: ModelGateway,
    tokenizer,
    registry: ToolRegistry,
    config: Config,
    artifacts: str = "",
) -> DelegatedResult:
    """先让实现者做，再让审查者在独立上下文里验。

    写者与验者用同一个模型的两个独立上下文——不是不同模型。
    审查者拿到的是需求、约束、验收标准和改动差异，**不含实现者的推理过程**，
    否则它只是在附和那个推理。
    """
    if not plan.delegate or plan.spec is None:
        raise ValueError("这份计划不包含可派发的任务说明")

    result = DelegatedResult(plan=plan)

    implemented = run_role(
        IMPLEMENTER, plan.spec, gateway, tokenizer, registry, config
    )
    result.implementer_final = implemented.final
    result.trace.extend(f"[实现] {line}" for line in implemented.trace)

    review_spec = TaskSpec(
        goal=f"独立审查这次改动：{plan.spec.goal}",
        targets=plan.spec.targets,
        constraints=plan.spec.constraints,
        acceptance=plan.spec.acceptance,
        out_of_scope=plan.spec.out_of_scope,
        artifact=artifacts or implemented.final,
    )
    reviewed = run_role(REVIEWER, review_spec, gateway, tokenizer, registry, config)
    result.reviewer_final = reviewed.final
    result.trace.extend(f"[审查] {line}" for line in reviewed.trace)
    return result
