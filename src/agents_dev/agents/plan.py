"""任务分解与项目级计划。

主循环一次只做一件事，这没错——错的是指望它自己从一个大目标里悟出
「那件事的序列」。小模型尤其不擅长这件事：它会把第一步做完，然后忘记
还有第二步。

所以分解要显式化，而且要落盘：计划是项目级状态，跨会话存活。
每完成一步要留下证据，否则「做到哪了」只能靠翻对话记录。

每一步都必须带验收标准，理由和分派闸门一样：没有可执行的判断依据，
这一步就无法被判定完成，计划会退化成一张永远打不完的清单。
"""

import json
import fnmatch
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.types import ChatRequest, Message

PENDING = "pending"
DONE = "done"
FAILED = "failed"

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "acceptance": {"type": "string"},
                    "scope": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["goal", "acceptance", "scope"],
            },
        }
    },
    "required": ["steps"],
}

DECOMPOSE_PROMPT = """你负责把一个较大的目标拆成可逐步执行的任务序列。

目标：{goal}

已有信息：
{context}

要求：
- 每一步都必须是「一次能在有限上下文里做完」的单元；
- 每一步都必须给出可执行的验收标准（能跑什么、看什么、判定依据是什么）；
- 每一步都要声明 scope：这一步允许改动哪些路径（目录前缀或通配符）。
  范围要尽量窄——它是这一步能自动落盘的边界，写宽了就失去意义；
- 顺序要正确：后面的步骤可以依赖前面步骤的产物；
- 步骤数量控制在 {limit} 步以内，宁可少而准，不要凑数；
- 不要写「调研一下」「优化一下」这类无法验收的步骤。

只输出 JSON。"""


@dataclass
class PlanStep:
    """计划中的一步。"""

    index: int
    goal: str
    acceptance: str
    scope: tuple[str, ...] = ()
    status: str = PENDING
    note: str = ""

    @property
    def finished(self) -> bool:
        return self.status in (DONE, FAILED)


@dataclass
class Plan:
    """一个项目级计划。"""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)

    def next_pending(self) -> PlanStep | None:
        return next((step for step in self.steps if step.status == PENDING), None)

    def progress(self) -> str:
        done = sum(1 for step in self.steps if step.status == DONE)
        return f"{done}/{len(self.steps)} 步已完成"

    def mark(self, index: int, status: str, note: str = "") -> None:
        for step in self.steps:
            if step.index == index:
                step.status = status
                step.note = note
                return
        raise KeyError(f"计划里没有第 {index} 步")

    def render(self) -> str:
        marks = {DONE: "✓", FAILED: "✗", PENDING: "·"}
        lines = [f"目标：{self.goal}", f"进度：{self.progress()}"]
        for step in self.steps:
            lines.append(
                f"{marks.get(step.status, '?')} {step.index}. {step.goal}"
                f"（验收：{step.acceptance}）"
            )
            if step.note:
                lines.append(f"    {step.note}")
        return "\n".join(lines)


def parse_plan(text: str, goal: str, limit: int = 10) -> Plan:
    """解析分解结果。解析不出来就返回空计划，由调用方决定是否重试。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return Plan(goal=goal)

    steps: list[PlanStep] = []
    for item in (payload.get("steps") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        step_goal = str(item.get("goal") or "").strip()
        acceptance = str(item.get("acceptance") or "").strip()
        # 没有验收标准的步骤直接丢弃：留着它，计划就退化成了待办清单。
        if not step_goal or not acceptance:
            continue
        steps.append(
            PlanStep(
                index=len(steps) + 1,
                goal=step_goal,
                acceptance=acceptance,
                scope=tuple(
                    str(item).strip()
                    for item in (item.get("scope") or [])
                    if str(item).strip()
                ),
            )
        )
    return Plan(goal=goal, steps=steps)


def decompose(
    gateway: ModelGateway,
    goal: str,
    context: str = "",
    limit: int = 10,
    max_tokens: int = 2048,
) -> Plan:
    """让模型把目标拆成可验收的步骤序列。"""
    response = gateway.chat(
        ChatRequest(
            messages=(
                Message(
                    role="user",
                    content=DECOMPOSE_PROMPT.format(
                        goal=goal, context=context or "无", limit=limit
                    ),
                ),
            ),
            max_tokens=max_tokens,
            response_schema=PLAN_SCHEMA,
        )
    )
    return parse_plan(response.text, goal, limit=limit)


def plan_path(project_root: Path) -> Path:
    return project_root / ".agent" / "plan.json"


def save_plan(path: Path, plan: Plan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"goal": plan.goal, "steps": [asdict(step) for step in plan.steps]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_plan(path: Path) -> Plan | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Plan(
        goal=payload.get("goal", ""),
        steps=[
            PlanStep(**{**item, "scope": tuple(item.get("scope") or ())})
            for item in payload.get("steps", [])
        ],
    )


def path_in_scope(path: str, scope: Sequence[str]) -> bool:
    """路径是否落在允许范围内。

    scope 里每一项可以是目录前缀（`src/foo`）或通配模式（`tests/**`）。
    空 scope 表示不允许任何自动改动——宁可退回逐项确认，也不要默认放行。
    """
    for pattern in scope:
        cleaned = pattern.rstrip("/")
        if path == cleaned or path.startswith(cleaned + "/"):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def out_of_scope(paths: Sequence[str], scope: Sequence[str]) -> list[str]:
    """列出超出允许范围的路径。"""
    return [path for path in paths if not path_in_scope(path, scope)]


def render_step_prompt(plan: Plan, step: PlanStep) -> str:
    """把某一步渲染成交给主循环的任务，并带上它在整体里的位置。

    带上位置很重要：主循环只知道当前这一步，不知道自己在长链条的哪一环，
    很容易为了完成当前一步而破坏前面步骤的产物。
    """
    done = "、".join(
        item.goal for item in plan.steps if item.status == DONE
    ) or "无"
    return (
        f"项目目标：{plan.goal}\n"
        f"已完成：{done}\n"
        f"本次只做这一步：{step.goal}\n"
        f"验收标准：{step.acceptance}\n"
        "不要顺手做后面步骤的事。"
    )
