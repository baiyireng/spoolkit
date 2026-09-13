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

from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.types import ChatRequest, Message
from spoolkit.context import templates as T
from spoolkit import limits

PENDING = "pending"
DONE = "done"
FAILED = "failed"

# 一步的 JSON 大约占多少**输出** token。
#
# 实测（27B，8192 窗口，50 题）：输出预算 2048 在第十八步处被切断
# （finish_reason=length），JSON 停在半个字符串上，解析出 0 步——整条自主
# 路线崩在第一步。2048 / 18 ≈ 114，这里留 25% 余量。
#
# 它只用来把「输出预算」换算成「一次能排几步」，本身不是限制：排不完就再排
# 一批（见 decompose）。
STEP_TOKENS = 140

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

# 分批时追加在后面的那一段。第一批不加——一次排得完的时候，提示词与
# 原来逐字相同，老测量还能对照。
BATCH_CONTINUE = T.DECOMPOSE_CONTINUE

# 分批推进（先做一批、回头看一眼、再排下一批）用的两段。
REFLECT_PROMPT = T.REFLECT
EXTEND_PROMPT = T.EXTEND

# 覆盖检查之后补排用的那一段。
DECOMPOSE_FILL = T.DECOMPOSE_FILL

# 分批推进时，第一批要补的一句（把「批的大小」和「步的粒度」分开说）。
BATCH_LIMIT_NOTE = T.DECOMPOSE_BATCH_LIMIT

# 分批用的 schema：多一个 done，用来问「后面还有没有」。
BATCH_SCHEMA: dict[str, Any] = {
    **PLAN_SCHEMA,
    "properties": {**PLAN_SCHEMA["properties"], "done": {"type": "boolean"}},
}


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
    max_tokens: int | None = None,
    window: int | None = None,
    must_cover: Sequence[str] = (),
    trace: list[str] | None = None,
    extra: str = "",
) -> Plan:
    """让模型把目标拆成可验收的步骤序列。**一次排不完就再排一批。**

    为什么必须有分批：输出预算是单次生成的硬边界，而步骤数没有上限。
    实测 50 题那次，提示词 4375 token、输出在 2048 处被服务端截断
    （finish_reason=length），JSON 停在第十八步，解析出 0 步——自主路线
    不是「做得慢」，是**根本起不来**。分批之后每批都小到装得下，
    步骤数就不再受输出预算限制。

    预算口径：单次 output 取登记表里的 `decompose_output_budget`，
    知道窗口时再按「窗口 − 提示词」收一次口；每一步按 STEP_TOKENS 折算。
    被截断不重来整份，而是**把这一批要小一半再排**——截断说明的是
    「这批太大」，不是「模型不会排」。

    `must_cover` 是**覆盖检查**：目标说「把这 50 件事都做完」时，计划少一件
    不该靠人眼发现。实测两次 50 题的跑都只排出 49 步（漏了 `50_retry_count`），
    而两次都没人察觉。这里在排完之后对一遍名单，缺的补一轮；补两轮还缺，
    由调用方把名单报出来（报，不猜——它可能是有意不做的）。
    """
    budget = int(max_tokens or limits.resolve("decompose_output_budget", {})[0])
    planned: list[PlanStep] = []
    planned_text = "（无）"
    # 调用方追加的一句（分批推进时用来把「批的大小」和「步的粒度」分开说）。
    tail = extra or ""
    # 这一批被截断时，只把**下一批**要的步数压小，不动整段的预算。
    # 原先砍的是 budget 本身，于是后面的批次一路变小——实测一次 50 步的
    # 拆解因此从 3 次调用涨到 9 次，输入 token 多了两倍。
    ask_cap: int | None = None
    for _ in range(max(1, limit)):  # 最多上限批次；正常远用不到
        remaining = limit - len(planned)
        if remaining <= 0:
            break
        ask = _batch_size(budget, goal, context, planned_text, window, remaining)
        if ask_cap is not None:
            ask = min(ask, ask_cap)
        # 「一次排得完」：第一批就把全部要了。这条路要保住老行为——
        # 提示词与没有分批时逐字相同，它给几步就是几步，不再追问。
        single = not planned and ask >= remaining
        if single:
            # 一次排得完：提示词与「没有分批」时逐字相同。
            prompt = DECOMPOSE_PROMPT.format(
                goal=goal, context=context or "无", limit=limit
            )
            schema = PLAN_SCHEMA
        else:
            prompt = DECOMPOSE_PROMPT.format(
                goal=goal, context=context or "无", limit=ask
            ) + BATCH_CONTINUE.format(
                planned=planned_text,
                start=len(planned) + 1,
                ask=ask,
            )
            schema = BATCH_SCHEMA
        if tail:
            prompt = prompt + tail
        response = gateway.chat(
            ChatRequest(
                messages=(Message(role="user", content=prompt),),
                max_tokens=budget,
                response_schema=schema,
            )
        )
        if response.truncated and ask > 1:
            # 这一批没排完就被切断：把**这一批**砍半重排，而不是重排整份计划，
            # 也不把整段的预算一起砍掉。
            ask_cap = max(1, ask // 2)
            if trace is not None:
                trace.append(f"拆解：要 {ask} 步被截断，下一批压到 {ask_cap} 步")
            continue
        ask_cap = None
        fresh, done = _parse_batch(response.text, goal, limit=ask)
        if trace is not None:
            trace.append(f"拆解：要 {ask} 步 → 回来 {len(fresh)} 步")
        for step in fresh:
            step.index = len(planned) + 1
            planned.append(step)
        if done or not fresh or single:
            break
        if len(fresh) < ask:
            # 续批时说好「没排完就排满」，它没排满就是「后面没有了」。
            break
        planned_text = "\n".join(f"{s.index}. {s.goal}" for s in planned)

    # 覆盖检查：名单里有没有谁一条步骤都没摊上。
    for _ in range(2):
        missing = uncovered(Plan(goal=goal, steps=list(planned)), must_cover)
        if not missing:
            break
        ask = _batch_size(budget, goal, context, planned_text, window, len(missing))
        prompt = (
            DECOMPOSE_PROMPT.format(
                goal=goal, context=context or "无", limit=max(ask, len(missing))
            )
            + BATCH_CONTINUE.format(
                planned=planned_text or "（无）",
                start=len(planned) + 1,
                ask=max(ask, len(missing)),
            )
            + DECOMPOSE_FILL.format(ask=max(ask, len(missing)), missing="、".join(missing))
        )
        response = gateway.chat(
            ChatRequest(
                messages=(Message(role="user", content=prompt),),
                max_tokens=budget,
                response_schema=BATCH_SCHEMA,
            )
        )
        fresh, done = _parse_batch(response.text, goal, limit=max(ask, len(missing)))
        if trace is not None:
            trace.append(
                f"拆解：覆盖检查要补 {len(missing)} 个（{missing[0]}…），"
                f"回来 {len(fresh)} 步"
            )
        if not fresh:
            break
        for step in fresh:
            step.index = len(planned) + 1
            planned.append(step)
        planned_text = "\n".join(f"{s.index}. {s.goal}" for s in planned)
        if done:
            break
    return Plan(goal=goal, steps=planned)


def uncovered(plan: Plan, names: Sequence[str]) -> list[str]:
    """名单里没有任何步骤提到的名字。

    判定是**字面**的：目录名出现在 goal / acceptance / contract / scope 之一
    就算覆盖。这会漏报（一步可能用别的说法涵盖了某目录），但不会误伤——
    宁可漏报，也不要把「看起来覆盖了」当成证据。
    """
    text = "\n".join(
        " ".join([step.goal, step.acceptance, step.contract, *step.scope])
        for step in plan.steps
    )
    return [name for name in names if name not in text]


# --- 分批推进：先做一批、回头看一眼、再排下一批 ---------------------------
#
# 为什么要有它：一次排 50 步，等于把"实际会怎样"排除在决策之外——而真实
# 任务里，前三步做完之后你往往才知道后面该怎么做（某个接口不是那样、
# 某个约束计划里没料到）。分批推进让**每一批都能吃到前一批的实际结果**。
#
# 代价要说清：计划的总生成量并不因此变少（步骤数没变），反而多出每批一次
# 的"回头看"调用。它换的是**更准的后续计划**，不是更少的 token。

REFLECT_MAX_CHARS = 300  # 回头看那三句话的上限（它是提示词，不是文档）

# 回头看的输出形状：一段摘要 + 至多两条可迁移的教训。
REFLECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "lessons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string"},
                    "trigger": {"type": "string"},
                },
                "required": ["rule", "trigger"],
            },
        },
    },
    "required": ["summary"],
}

MAX_REFLECT_LESSONS = 2


def reflect_progress(
    gateway: ModelGateway,
    goal: str,
    steps: Sequence[PlanStep],
) -> tuple[str, list[tuple[str, str]]]:
    """回头看一眼：返回（摘要, 教训列表）。

    输入只有**已完成步骤的目标 + 实际结果**（note 是循环记的第一行产出），
    所以它不会被自己的计划复述带偏——它看的是事实。

    摘要给下一批拆解用；教训进教训库，**在后面的任务里主动推送**——
    这是"让它在任务中成长"最直接的一条落地。

    解析不出来时退回「摘要=原文、教训为空」：**回头看不能挡住推进**。
    """
    done = [step for step in steps if step.status == DONE]
    if not done:
        return "", []
    results = "\n".join(
        f"- {step.goal}｜结果：{step.note or '（没记结果）'}" for step in done
    )
    response = gateway.chat(
        ChatRequest(
            messages=(
                Message(
                    role="user",
                    content=REFLECT_PROMPT.format(goal=goal, results=results),
                ),
            ),
            max_tokens=500,
            response_schema=REFLECT_SCHEMA,
        )
    )
    return _parse_reflection(response.text)


def _parse_reflection(text: str) -> tuple[str, list[tuple[str, str]]]:
    """解析回头看。**宽容**：格式坏了也不能让整次运行停在这里。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text.strip()[:REFLECT_MAX_CHARS], []
    if not isinstance(payload, dict):
        return text.strip()[:REFLECT_MAX_CHARS], []
    summary = str(payload.get("summary") or "").strip()[:REFLECT_MAX_CHARS]
    lessons: list[tuple[str, str]] = []
    for item in payload.get("lessons") or []:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule") or "").strip()
        trigger = str(item.get("trigger") or "").strip()
        if not rule or not trigger:
            # 没有触发词的教训永远不会被推出来——存了等于没存。
            continue
        lessons.append((rule, trigger))
        if len(lessons) >= MAX_REFLECT_LESSONS:
            break
    return summary, lessons


def extend_plan(
    gateway: ModelGateway,
    plan: Plan,
    reflection: str,
    limit: int = 8,
    context: str = "",
    window: int | None = None,
    trace: list[str] | None = None,
) -> list[PlanStep]:
    """按回头看的结论排下一批，返回新步骤（索引接在已排步骤之后）。

    调用方负责把它追加进 plan。返回空列表表示"模型认为做完了"——
    这是**它的判断**，所以调用方要把"没有更多步骤了"如实报出来。
    """
    budget = int(limits.resolve("decompose_output_budget", {})[0])
    done_text = "\n".join(
        f"{step.index}. {step.goal}（{step.status}）" for step in plan.steps
    )
    prompt = (
        # 材料必须一起给：不给的话，续排出来的步骤会退化成"修复 04 目录下的
        # 编程题"这种笼统的话（实测），而第一批之所以具体，正是因为看到了
        # 各目录的 TASK.md 与验收测试。
        DECOMPOSE_PROMPT.format(goal=plan.goal, context=context or "（无）", limit=limit)
        + EXTEND_PROMPT.format(
            done=done_text or "（无）",
            reflection=reflection or "（无）",
            start=len(plan.steps) + 1,
            ask=limit,
        )
    )
    response = gateway.chat(
        ChatRequest(
            messages=(Message(role="user", content=prompt),),
            max_tokens=budget,
            response_schema=BATCH_SCHEMA,
        )
    )
    fresh, done = _parse_batch(response.text, plan.goal, limit=limit)
    if trace is not None:
        trace.append(
            f"续排：要 {limit} 步 → 回来 {len(fresh)} 步"
            + ("（它认为做完了）" if done else "")
        )
    return fresh


def _batch_size(
    budget: int,
    goal: str,
    context: str,
    planned_text: str,
    window: int | None,
    remaining: int,
) -> int:
    """这一批排几步。

    知道窗口时按「窗口 − 提示词 − 余量」收口：不知道窗口就只按登记表里的
    输出预算算。余量留给分词误差，宁可少排一步，也不要撞服务端的拒绝。
    """
    usable = budget
    if window:
        prompt_tokens = _estimate_tokens(goal, context, planned_text)
        usable = min(usable, max(1, window - prompt_tokens - 256))
    return max(1, min(remaining, usable // STEP_TOKENS))


def _estimate_tokens(*texts: str) -> int:
    """粗估长度：拉丁按 4 字符 / token、其余按 1.5 字符 / token。

    这里只需要「够保守地不小看它」。真正精确的分词在调用方手里，
    而它不该被拆解这一步反向依赖——估偏了只是少排一步。
    """
    total = 0.0
    for text in texts:
        ascii_chars = sum(1 for char in text if ord(char) < 128)
        total += ascii_chars / 4 + (len(text) - ascii_chars) / 1.5
    return int(total)


def _parse_batch(text: str, goal: str, limit: int) -> tuple[list[PlanStep], bool]:
    """解析一批：返回（步骤，是否已经排完）。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [], False
    steps = parse_plan(text, goal, limit=limit).steps
    return steps, bool(payload.get("done"))


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


def render_step_prompt(
    plan: Plan, step: PlanStep, done_inline: int | None = None
) -> str:
    """把某一步渲染成交给主循环的任务，并带上它在整体里的位置。

    带上位置很重要：主循环只知道当前这一步，不知道自己在长链条的哪一环，
    很容易为了完成当前一步而破坏前面步骤的产物。

    「已完成」只列**最近的几条**：这一行随进度线性增长，而每一轮都要付一次。
    实测 50 题那次，第 43 步的这一步提示词里它已经占掉几百 token，而 267 次
    调用每次都带着它——完整清单在 `.agent/progress.md`，状态块里也有一份
    按同一标定值截断的版本，没有必要在这里再摊一遍。
    """
    finished = [item.goal for item in plan.steps if item.status == DONE]
    limit = (
        int(limits.knob("done_inline").default) if done_inline is None else done_inline
    )
    recent = finished[-max(1, limit) :]
    if not finished:
        done = "无"
    elif len(finished) <= len(recent):
        done = "、".join(recent)
    else:
        done = (
            f"{len(finished)} 步已完成，最近的：{'、'.join(recent)}"
            "（完整清单见 .agent/progress.md）"
        )
    # 把这一步的 scope 交出去。**它本来就有**（拆解时 schema 要求每步声明范围，
    # 那道闸门还用它决定自动落盘边界），但执行时没给执行者看——实测后果很实在：
    # 模型不知道要改哪个文件，于是每步先花一轮 survey 自己翻（每任务 3 轮 vs
    # 单题模式的 2 轮），而那一轮把后续几轮的提示词也一起撑大了。
    scope = "、".join(step.scope) if step.scope else "（没声明）"
    contract = f"契约（不能动的接口与必须满足的断言）：{step.contract}\n" if step.contract else ""
    return (
        f"项目目标：{_goal_digest(plan.goal)}\n"
        f"已完成：{done}\n"
        f"涉及：{scope}\n"
        f"本次只做这一步：{step.goal}\n"
        f"{contract}"
        f"验收标准：{step.acceptance}\n"
        "不要顺手做后面步骤的事。"
    )


def _goal_digest(goal: str, cap: int = 90) -> str:
    """整条目标压成第一句。

    它每一轮都要随步骤提示词走一遍，而整条目标往往有一两百 token（例如
    "这个工作区里放着 50 道编程题……每道题在它自己的目录里跑 pytest 通过"），
    其中可执行的部分（改哪个文件、验收是什么）在这一步的契约与验收标准里
    已经写清了。留下第一句是为了不让执行者失去"这事在整个链条里的位置"。
    """
    text = goal.strip()
    for mark in ("。", "\n"):
        cut = text.find(mark)
        if 0 < cut:
            text = text[:cut]
            break
    return text if len(text) <= cap else text[:cap] + "…"
