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
from agents_dev.context import templates as T

PENDING = "pending"
DONE = "done"
FAILED = "failed"

# 这一步由谁执行。见 PlanStep.executor。
SELF = "self"
SUBAGENT = "subagent"

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
                    "contract": {"type": "string"},
                    "executor": {"type": "string", "enum": ["self", "subagent"]},
                },
                "required": ["goal", "acceptance", "scope", "executor"],
            },
        }
    },
    "required": ["steps"],
}

DECOMPOSE_PROMPT = T.DECOMPOSE


@dataclass
class PlanStep:
    """计划中的一步。"""

    index: int
    goal: str
    acceptance: str
    # 这一步**不能动的接口 / 必须满足的断言**，从验收测试里读出来。
    # 它存在的理由只有一个：让执行者不必自己再读一遍测试。实测里那一步
    # 每次要多花一轮（3 轮 vs 2 轮），而轮数是这块成本的主体。
    contract: str = ""
    scope: tuple[str, ...] = ()
    # 这一步**谁来做**。拆解时就要定：不写这一维，harness 就等于替所有步骤
    # 决定「都自己做」——那正是「按步注入」被诟病的地方：每一步都按派发契约
    # 的形状喂给一个全新上下文，但派发的决定权与独立审查都不见了。
    #
    #   self     —— 主循环自己做：小改动、改一处、有明确验收标准的活。
    #   subagent —— 交给实现者做、另一独立上下文审查：要读很多文件、
    #               但验收标准清楚的活（细节不该占着主循环的上下文）。
    executor: str = SELF
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

    def blocked_by(self) -> PlanStep | None:
        """找出第一处失败。计划必须在失败处停住。

        把失败当成「已处理」直接跳过，后面的步骤就是在坏地基上继续盖——
        而且表面上进度还在涨，看起来一切正常。
        """
        return next((step for step in self.steps if step.status == FAILED), None)

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
                contract=str(item.get("contract") or "").strip(),
                scope=tuple(
                    str(item).strip()
                    for item in (item.get("scope") or [])
                    if str(item).strip()
                ),
                # 缺省按「自己做」：派发是要付固定成本的选择，不该由缺省打开。
                executor=(
                    SUBAGENT
                    if str(item.get("executor") or "").strip().lower() == SUBAGENT
                    else SELF
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


def _literal_prefix(pattern: str) -> str:
    """取模式里第一个通配符之前的字面前缀。"""
    for index, char in enumerate(pattern):
        if char in "*?[":
            return pattern[:index]
    return pattern


def narrow_scope(
    granted: Sequence[str], proposed: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把模型提出的范围收窄到你授予的范围之内。

    返回（生效范围，被拒绝的范围）。

    授权必须来自外部：**如果模型既能提范围又能批范围，它就自己给自己发了许可证。**
    所以这里只做「收窄」，不做「扩权」——模型可以把一步限制在
    `scratch_lab/parser.py`，但不能把一步放宽到整个项目。

    判定用字面前缀比较，保守但可解释：前缀不在你授予的范围内就拒绝。
    这不是完整的 glob 包含判定（那在一般情况下不可判定），但方向是安全的——
    拿不准就拒绝，而不是拿不准就放行。
    """
    allowed: list[str] = []
    rejected: list[str] = []
    for pattern in proposed:
        if not granted:
            rejected.append(pattern)
            continue
        prefix = _literal_prefix(pattern)
        if any(prefix.startswith(_literal_prefix(item)) for item in granted):
            allowed.append(pattern)
        else:
            rejected.append(pattern)
    return tuple(allowed), tuple(rejected)


def render_step_prompt(plan: Plan, step: PlanStep) -> str:
    """把某一步渲染成交给主循环的任务，并带上它在整体里的位置。

    带上位置很重要：主循环只知道当前这一步，不知道自己在长链条的哪一环，
    很容易为了完成当前一步而破坏前面步骤的产物。
    """
    done = "、".join(
        item.goal for item in plan.steps if item.status == DONE
    ) or "无"
    # 把这一步的 scope 交出去。**它本来就有**（拆解时 schema 要求每步声明范围，
    # 那道闸门还用它决定自动落盘边界），但执行时没给执行者看——实测后果很实在：
    # 模型不知道要改哪个文件，于是每步先花一轮 survey 自己翻（每任务 3 轮 vs
    # 单题模式的 2 轮），而那一轮把后续几轮的提示词也一起撑大了。
    scope = "、".join(step.scope) if step.scope else "（没声明）"
    contract = f"契约（不能动的接口与必须满足的断言）：{step.contract}\n" if step.contract else ""
    return (
        f"项目目标：{plan.goal}\n"
        f"已完成：{done}\n"
        f"涉及：{scope}\n"
        f"本次只做这一步：{step.goal}\n"
        f"{contract}"
        f"验收标准：{step.acceptance}\n"
        "不要顺手做后面步骤的事。"
    )
