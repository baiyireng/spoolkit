"""编排工具：模型在**一步**里提交一份调用计划，由这里执行并聚合成一段结果。

## 它解决的是什么

「先搜一遍 → 再读几处 → 再跑一条命令确认」这类活，在既有路径上是**一步一个
调用**：每一步都要过一次模型（一份完整的输入 + 一次完整的输出），十几步之后
上下文里还堆着十几份原始输出。而模型真正需要的往往只是它们的**结论**。

`orchestrate` 把这一串压进**一步**：模型写一份小计划（调谁、为什么调、谁依赖
谁、结果怎么合成一段），这里按依赖关系执行，只把**聚合后的那一段**回灌。

## 三条纪律

1. **判据不另造**：能不能并行 / 要不要串行 / 要不要隔离，一律取
   `tools/capabilities.py` 的纯函数（`can_parallel` / `requires_serial` /
   `needs_isolation`）；要不要请批取那份声明里的 `needs_approval`。
   **没声明过的工具**（外挂 MCP 的名字由用户配置决定）按那份最保守的
   `UNKNOWN` 走：串行 + 护栏，不并行。
2. **成果照落盘**：会改工作区的分支**串行**执行，走的是主循环单调用**同一条
   路**（`PendingChanges` → 护栏 → 请示 → 审计）。整批算**一个运行单元**：
   `pending.run_id` 只有一个，所以收尾只有**一条**整轮回退命令
   （`spool revert --run <id>`），中途**绝不**出现空工作区（§2.5：拦声明与
   动作、不拦成果）。
3. **失败隔离**：某条分支失败 / 被拦 / 被挂起**不拖垮整批**——记下它的状态，
   继续跑与它无依赖关系的分支；依赖它的那几条标成「跳过」。聚合里**如实
   报告哪几条失败 / 被拦 + 已经给过什么替代**。

## 聚合预算

聚合结果有上限（`orchestrate_aggregate_chars`）：超了按分支分配、超出部分
截断并**如实标注**。理由很直接——把 N 份原始输出全灌进上下文正是这一步要省
的那笔钱，无上限的聚合等于把省下的又花回去。

## 这一层不做什么

它**不判断**「这个动作该不该拦」：那是 `agent/review_gate.py` 与
`agent/loop.py` 的事，这里只把分支送进那条既有通道（`BranchRunner.serial`）。
它也不建自己的副本：`needs_isolation` 的分支交给工具**自己既有的**试跑副本
机制（`tools/exec.py` 的 `trial_workspace`），编排层只标注这一条走了隔离。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from spoolkit.tools import capabilities as C
from spoolkit.tools.types import (
    BLOCKED_PREFIX,
    NEXT_STEP_MARKER,
    REVERSE_CLAUSE,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)

# ---- 分支状态（事件、聚合、读数都用这一份，不另立一套）----------------------

#: 执行成功（读了就是读到了，写了就是进待确认清单了）。
STATUS_OK = "成功"
#: 正文已暂存（大重写等令牌），**不是失败**但也不算落成。
STATUS_STAGED = "已暂存"
#: 执行了，失败。
STATUS_FAILED = "失败"
#: 被护栏 / 策略硬拦，**没有执行**。
STATUS_BLOCKED = "被拦"
#: 需要用户批准而无人可问 → 挂起待批，**没有执行**（成果不受影响）。
STATUS_SUSPENDED = "挂起待批"
#: 依赖的分支没成功，这一条**没有执行**。
STATUS_SKIPPED = "跳过"

#: 事件流的两种 kind。`subcall` 是**每个子调用**一条（前端据此画调用链与
#: 调用声明），`orchestrate` 是整批的收尾一行（note 的子类）。
#:
#: 它们都落在**过程区**，不是结论区：调用的思路与状态进事件流，最终答复仍由
#: 主循环的收尾报告给。
EVENT_SUBCALL = "subcall"
EVENT_BATCH_KIND = "orchestrate"

#: 单条分支最少留多少字。低于它的分支只留状态行——把 40 字的结果截成 10 字
#: 只是噪声，不如如实说「这条因为预算没展开」。
MIN_BRANCH_CHARS = 80


# ---- 计划 -------------------------------------------------------------------


@dataclass(frozen=True)
class PlanCall:
    """计划里的一条调用。`needs` 是它依赖的调用 id（**整体注入**，不做字段级取值）。"""

    id: str
    tool: str
    args: dict[str, Any]
    why: str
    needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    """一份调用计划。为什么这么编排、调什么、结果怎么合成。"""

    why: str
    calls: tuple[PlanCall, ...]
    aggregate: str = ""


@dataclass(frozen=True)
class Branch:
    """一条分支的执行结果。"""

    call: PlanCall
    status: str
    seconds: float
    body: str = ""
    reason: str = ""
    parallel: bool = False
    isolation: bool = False
    usage: Usage | None = None
    #: 这次调用真正被注入过哪些上游输出（`{{id}}` 占位符被换掉的那几个）。
    injected: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_OK, STATUS_STAGED)


@dataclass(frozen=True)
class BatchOutcome:
    """整批的结果：模型看 `text`，判据与读数看其余字段。"""

    branches: tuple[Branch, ...]
    text: str
    seconds: float
    truncated: bool = False
    usage: Usage | None = None

    @property
    def ok(self) -> bool:
        """有一条成功就算这一批有产出——**不能因为别的分支失败就报整批失败**：
        失败隔离的语义就是「其余的照做」，报成功才对得上真正发生过的事。"""
        return any(branch.ok for branch in self.branches)

    @property
    def executed(self) -> bool:
        """这一批**有没有一条真的执行过**（跑了但失败也算执行过）。

        一条都没有（全是被拦 / 挂起 / 跳过）时，主循环那一步不该吃步数预算：
        「被拦的动作没有产出、也不换来新信息」（`loop.blocked_refund_limit`）。
        判据用的是本层自己的状态名，不认字符串前缀。
        """
        return any(
            branch.status not in (STATUS_BLOCKED, STATUS_SUSPENDED, STATUS_SKIPPED)
            for branch in self.branches
        )

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for branch in self.branches:
            tally[branch.status] = tally.get(branch.status, 0) + 1
        return tally

    def evidence(self) -> str:
        """判据用的原始证据（**不给模型看**，`ToolResult.raw` 那个出口）。"""
        return json.dumps(
            {
                "seconds": round(self.seconds, 3),
                "truncated": self.truncated,
                "branches": [
                    {
                        "id": branch.call.id,
                        "tool": branch.call.tool,
                        "why": branch.call.why,
                        "needs": list(branch.call.needs),
                        "status": branch.status,
                        "seconds": round(branch.seconds, 3),
                        "parallel": branch.parallel,
                        "isolation": branch.isolation,
                        "injected": list(branch.injected),
                        "reason": branch.reason,
                    }
                    for branch in self.branches
                ],
            },
            ensure_ascii=False,
        )


# ---- 校验 -------------------------------------------------------------------


def plan_shape() -> str:
    """计划**照抄得出来**的形状。模型写错时给的就是这一段。"""
    return (
        'orchestrate 的计划形状：{"why":"为什么这么编排",'
        '"calls":[{"id":"a1","tool":"工具名","args":{},"why":"想拿什么","needs":[]}],'
        '"aggregate":"怎么把这些结果合成一段"}'
    )


def _next_step(text: str) -> str:
    """一行以 `NEXT_STEP_MARKER` 开头的、**能照做**的下一步。

    复用那个标记是有意的：主循环硬拦「原样重提」时认的就是这个前缀，会把
    修正后的形状原样端回给模型（`loop._retry_block_message`）。
    """
    return f"{NEXT_STEP_MARKER}{text}"


def _shape_of(call_id: str) -> str:
    return (
        f'{{"id":"{call_id or "a1"}","tool":"<工具名>","args":{{…}},'
        f'"why":"<这次想拿什么>","needs":[]}}'
    )


def _first_problem(raw: Any, registry: Any, max_calls: int) -> tuple[list[PlanCall], str]:
    """把计划的外壳与每一条调用校一遍。返回（调用列表，错误说明）。

    错误一律带上「可用什么 + 照抄什么」，与 `tools/registry.py` 的拒绝话术
    同一个口径（`_reject`）：只说「不行」会让模型在原地换着花样重试。
    """
    if not isinstance(raw, dict):
        return [], f"计划必须是 JSON 对象（这次给的是 {type(raw).__name__}）。\n" + _next_step(
            plan_shape()
        )
    why = raw.get("why")
    if not isinstance(why, str) or not why.strip():
        return [], (
            "计划缺少 why：**为什么这么编排**要写出来，它是给人看调用思路的那一句。\n"
            + _next_step(plan_shape())
        )
    calls = raw.get("calls")
    if not isinstance(calls, list) or not calls:
        return [], (
            "计划的 calls 必须是非空数组（这次一个调用都没有，那就没必要编排）。\n"
            + _next_step(plan_shape())
        )
    if len(calls) > max_calls:
        return [], (
            f"这次计划带了 {len(calls)} 条调用，超出一次能编排的上限（{max_calls} 条）。"
            "编排省的是「来回几轮」，不是「一次做完整个任务」：批太大时聚合会被"
            "截断、失败的影响面也大。拆成两批（这一批先做前面那几条），"
            "或者把其中几条留到下一步。"
            "（这个数是能力标定值，`limits --set orchestrate_max_calls=…` 可以调大。）\n"
            + _next_step(
                '把 calls 缩到 %d 条以内：%s' % (max_calls, plan_shape())
            )
        )

    parsed: list[PlanCall] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(calls):
        where = f"calls[{index}]"
        if not isinstance(item, dict):
            return [], (
                f"{where} 必须是对象（这次给的是 {type(item).__name__}）。\n"
                + _next_step(_shape_of(""))
            )
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            return [], (
                f"{where} 缺少 id：每条调用要有**本次计划内唯一**的短名字"
                "（a1 / b1 这种），needs 与聚合都按它指代。\n"
                + _next_step(_shape_of(""))
            )
        call_id = call_id.strip()
        if call_id in seen:
            return [], (
                f"{where} 的 id 与 calls[{seen[call_id]}] 重复（都是 {call_id}）："
                "id 是依赖与聚合的把手，重名之后 needs 指哪一条就说不清了。\n"
                + _next_step(f"把这一条的 id 改成别的短名字（例如 c{index + 1}）。")
            )
        seen[call_id] = index

        tool = item.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            return [], (
                f"调用 {call_id} 缺少 tool（要调哪个工具）。\n"
                + _next_step(_shape_of(call_id))
            )
        tool = tool.strip()
        if tool == "orchestrate":
            return [], (
                f"调用 {call_id}：**编排里不能再编排**——一步一次就够了，"
                "嵌套不会多省一步，却会把两层的失败隔离与聚合预算叠在一起，"
                "读数再也说不清。把里层那几条直接平铺到这次的 calls 里。\n"
                + _next_step(
                    '把 calls 里那一条 {"tool":"orchestrate"} 展开成它的分支。'
                )
            )
        spec = registry.get(tool)
        if spec is None:
            available = _available_line(registry)
            return [], (
                f"调用 {call_id}（{tool}）：未知工具——当前工具表里没有它。\n"
                f"可用工具: {available}\n"
                + _next_step(
                    f"把这一条的 tool 换成上面清单里的一个名字：{_shape_of(call_id)}"
                )
            )
        args = item.get("args", {})
        if not isinstance(args, dict):
            return [], (
                f"调用 {call_id}（{tool}）的 args 必须是对象（参数名 → 值）。\n"
                + _next_step(
                    f'args 照这个工具的参数表写：{_shape_of(call_id).replace("{…}", "{…}")}'
                )
            )
        problem = registry.validate(tool, args)
        if problem is not None:
            return [], f"调用 {call_id}（{tool}）参数校验失败: {problem}"

        call_why = item.get("why")
        if not isinstance(call_why, str) or not call_why.strip():
            return [], (
                f"调用 {call_id}（{tool}）缺少 why：**每一次调用都要写清想拿到什么**，"
                "不然聚合出来的一段读不出这一步在干什么、失败时也无从判断是工具"
                "的问题还是理解错了。\n"
                + _next_step(_shape_of(call_id))
            )
        needs = item.get("needs", [])
        if needs is None:
            needs = []
        if not isinstance(needs, list) or any(
            not isinstance(one, str) or not one.strip() for one in needs
        ):
            return [], (
                f"调用 {call_id} 的 needs 必须是**调用 id 的数组**（这次给的是 "
                f"{type(needs).__name__}）。\n"
                + _next_step(
                    f'needs 里只放这次计划里别的调用的 id，例如 ["a1"]；'
                    f"没有依赖就写 []：{_shape_of(call_id)}"
                )
            )
        parsed.append(
            PlanCall(
                id=call_id,
                tool=tool,
                args=dict(args),
                why=call_why.strip(),
                needs=tuple(one.strip() for one in needs),
            )
        )

    ids = [call.id for call in parsed]
    for call in parsed:
        unknown = [one for one in call.needs if one not in seen]
        if unknown:
            return [], (
                f"调用 {call.id} 的 needs 里点了不存在的调用 id: "
                f"{'、'.join(unknown)}。\n"
                f"这次计划里的 id: {'、'.join(ids)}\n"
                + _next_step(
                    "needs 只能点这次计划里别的调用的 id（不能点自己、也不能点"
                    "上一步的调用）：把不存在的那个换成上面清单里的一个，"
                    "或者去掉它。"
                )
            )
        if call.id in call.needs:
            return [], (
                f"调用 {call.id} 的 needs 里点了它自己：那是一条永远等不到依赖的"
                "调用。\n"
                + _next_step(f"把 {call.id} 从它自己的 needs 里去掉。")
            )
    cycle = _find_cycle(parsed)
    if cycle:
        return [], (
            f"计划里的 needs 形成了循环依赖：{' → '.join(cycle)}。"
            "循环里没有一条等得到依赖，整批都执行不了。\n"
            + _next_step(
                "把环打断：其中一条改成不依赖对方（依赖只用于「B 要 A 的结果」，"
                "不需要 A 的结果就不要写 needs）。"
            )
        )
    return parsed, ""


def _available_line(registry: Any) -> str:
    """可用工具清单（拒绝话术里的「支持参数清单」那一半）。"""
    names = list(registry.names())
    return "、".join(names) if names else "（当前没有注册任何工具）"


def _find_cycle(calls: Sequence[PlanCall]) -> list[str]:
    """找一条循环依赖（返回路径，含首尾重复的那一项；没有就返回空表）。"""
    graph = {call.id: [one for one in call.needs] for call in calls}
    state: dict[str, int] = {}
    stack: list[str] = []

    def walk(node: str) -> list[str]:
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, ()):
            if state.get(nxt, 0) == 1:
                return stack[stack.index(nxt) :] + [nxt]
            if state.get(nxt, 0) == 0:
                found = walk(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return []

    for call in calls:
        if state.get(call.id, 0) == 0:
            found = walk(call.id)
            if found:
                return found
    return []


def parse_plan(
    raw: Any, registry: Any, *, max_calls: int
) -> tuple[Plan | None, str]:
    """校验整批计划。通过返回 `(plan, "")`，不通过返回 `(None, 拒绝话术)`。

    校验三件事（缺一条都会让执行层拿到一个说不清的批次）：

    1. **工具存在**：不存在就把可用清单摆出来（照抄形状给在下一步）；
    2. **参数合法**：复用注册表的参数校验（`registry.validate`），
       拒绝话术与单调用**逐字相同**——支持哪些参数、照什么形状改；
    3. **依赖说得通**：id 唯一、needs 指向本次计划里真实存在的 id、没有环。
    """
    calls, problem = _first_problem(raw, registry, max_calls)
    if problem:
        return None, problem
    aggregate = raw.get("aggregate", "")
    if not isinstance(aggregate, str):
        aggregate = ""
    return (
        Plan(why=str(raw.get("why", "")).strip(), calls=tuple(calls), aggregate=aggregate),
        "",
    )


# ---- 执行 -------------------------------------------------------------------


@dataclass
class BranchRunner:
    """分支执行通道。**由主循环提供**（工具层拿不到护栏 / 请示 / 审计）。

    - `read`：可以并行的**只读**分支。实现必须线程安全（不碰主循环的护栏
      状态机、不弹问题）；
    - `serial`：会改工作区 / 要请批 / 要隔离的分支。实现走的是**单调用那条
      同一条路**（`AgentLoop._invoke_guarded`）；
    - `status`：这次结果属于哪一档（成功 / 已暂存 / 失败 / 被拦 / 挂起待批）；
    - `note`：把结果记进主循环的账（进度、写落成）。
    """

    read: Callable[[ToolCall], ToolResult]
    serial: Callable[[ToolCall], ToolResult]
    status: Callable[[ToolCall, ToolResult], str]
    note: Callable[[ToolCall, ToolResult], None] = lambda call, result: None
    emit: Callable[[str, dict], None] = lambda kind, data: None


def _inject(args: Any, upstream: dict[str, str]) -> tuple[Any, tuple[str, ...]]:
    """把上游分支的输出**整体**注入这次调用的参数（`{{id}}` 占位符）。

    依赖有两种用法，都要能表达，而且都不引入新语法：

    - **只要顺序**（「先写 A 再读 B」那种）：`needs` 写了、参数里不出现占位符，
      注入就是空操作——它只负责排序；
    - **要上游的输出**：在参数里写 `{{a1}}`，整段（**整体**，不做字段级取值）
      换成 a1 的结果。值**正好等于** `{{a1}}` 时整段就是它（可以塞进
      `write_file.content`、`replace_text.new_text` 这类正文参数）；
      夹在别的文字里（`"上面是结果：{{a1}}"`）则就地拼接。

    为什么不做字段级模板（`{{a1.line3}}`）：那要另一套语法，27B 写不对，
    而收益只是省几个字——真需要细分时，模型多写一条调用比学一门模板语言便宜。
    """
    if not upstream:
        return args, ()
    used: list[str] = []

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            for name, body in upstream.items():
                if f"{{{{{name}}}}}" in value:
                    if name not in used:
                        used.append(name)
                    value = value.replace(f"{{{{{name}}}}}", body)
            return value
        if isinstance(value, list):
            return [walk(one) for one in value]
        if isinstance(value, dict):
            return {key: walk(one) for key, one in value.items()}
        return value

    return walk(args), tuple(used)


def _parallel_ok(tool: str) -> bool:
    """这一条能不能进**并行池**：只读，而且不需要任何请示。

    判据全部来自能力声明，这里一条都不新造：
    - `can_parallel` 为假（会写 / 会破坏）→ 串行；
    - `needs_approval`（执行前要请批）→ 串行：请批本身是**有先后**的人机往返，
      并行去问会把「哪一条被问了」说出来都对不上；
    - `delegates`（派出去的子智能体手里有写工具）→ 串行：它不是只读，
      两条并行的派发会同时往同一份待确认清单里写；
    - **没声明过**的工具：`can_parallel` 已经是「最保守」那一份的答案（假），
      所以自动落到串行。
    """
    if not C.can_parallel(tool):
        return False
    declared = C.declared(tool)
    if declared is None:
        return False
    return not (declared.needs_approval or declared.delegates)


def _batches(items: Sequence[Any], size: int) -> list[list[Any]]:
    """按上限切块。`size` 至少 1（配置里填 0 时按 1 处理，不许变成无上限）。"""
    step = max(1, int(size))
    return [list(items[index : index + step]) for index in range(0, len(items), step)]


def run_plan(
    plan: Plan,
    *,
    registry: Any,
    runner: BranchRunner,
    max_parallel: int,
    aggregate_chars: int,
) -> BatchOutcome:
    """按依赖执行整批，返回聚合后的一段与逐条状态。

    调度规则（顺序即语义）：

    1. 每一轮挑出「依赖都已经跑完」的那些；依赖里有不成功的 → 这一条**跳过**
       （不执行、不报错，聚合里说明原因）；
    2. 可并行的只读分支按 `max_parallel` 切块并发跑；
    3. 串行分支（写 / 请批 / 隔离）**一条一条**跑，顺序就是计划里的顺序；
    4. 一条分支自己的失败 **不影响**别的分支——只有依赖它的那几条被跳过。
    """
    emit_lock = threading.Lock()

    def emit(kind: str, data: dict) -> None:
        # 事件在并行池里是从**多个线程**发出的：锁住整条写出，行与行不会交错。
        with emit_lock:
            runner.emit(kind, data)

    emit(
        "note",
        {
            "text": (
                f"编排开始：{len(plan.calls)} 条调用，"
                f"并行上限 {max(1, int(max_parallel))} 条。"
                f"为什么这么编排：{plan.why}"
            ),
            "ok": True,
            "kind": EVENT_BATCH_KIND,
            "phase": "start",
            "calls": len(plan.calls),
            "why": plan.why,
            "max_parallel": max(1, int(max_parallel)),
        },
    )

    started = time.time()
    done: dict[str, Branch] = {}
    order = [call.id for call in plan.calls]

    while len(done) < len(plan.calls):
        ready = [
            call
            for call in plan.calls
            if call.id not in done and all(one in done for one in call.needs)
        ]
        if not ready:  # 校验过环，这里只是防御：剩下的都等不到依赖
            break
        blocked_deps = [
            call
            for call in ready
            if any(not done[one].ok for one in call.needs)
        ]
        for call in blocked_deps:
            broken = [one for one in call.needs if not done[one].ok]
            branch = Branch(
                call=call,
                status=STATUS_SKIPPED,
                seconds=0.0,
                reason=(
                    "依赖的 "
                    + "、".join(f"{one}（{done[one].status}）" for one in broken)
                    + " 没有成功，所以这一条没有执行"
                ),
            )
            done[call.id] = branch
            emit(EVENT_SUBCALL, _event_of(branch, order))
        runnable = [call for call in ready if call.id not in done]
        if not runnable:
            continue
        parallel_calls = [call for call in runnable if _parallel_ok(call.tool)]
        serial_calls = [call for call in runnable if not _parallel_ok(call.tool)]
        for chunk in _batches(parallel_calls, max_parallel):
            for branch in _run_parallel(chunk, runner, emit, order, done):
                done[branch.call.id] = branch
        for call in serial_calls:
            branch = _run_one(
                call,
                runner,
                emit,
                order,
                parallel=False,
                upstream=_upstream_of(call, done),
            )
            done[branch.call.id] = branch

    branches = tuple(done[call.id] for call in plan.calls)
    text, truncated = _aggregate(plan, branches, aggregate_chars)
    seconds = time.time() - started
    outcome = BatchOutcome(
        branches=branches,
        text=text,
        seconds=seconds,
        truncated=truncated,
        usage=_sum_usage(branches),
    )
    counts = outcome.counts()
    emit(
        "note",
        {
            "text": (
                f"编排结束：{_tally_text(counts)}，耗时 {seconds:.1f}s，"
                f"聚合 {len(text)} 字"
                + ("（**已按预算截断**）" if truncated else "")
            ),
            "ok": outcome.ok,
            "kind": EVENT_BATCH_KIND,
            "phase": "end",
            "counts": counts,
            "seconds": round(seconds, 3),
            "aggregate_chars": len(text),
            "truncated": truncated,
            "ok_branches": sorted(
                branch.call.id for branch in branches if branch.ok
            ),
            "bad_branches": sorted(
                branch.call.id
                for branch in branches
                if branch.status in (STATUS_FAILED, STATUS_BLOCKED, STATUS_SUSPENDED)
            ),
            "skipped_branches": sorted(
                branch.call.id for branch in branches if branch.status == STATUS_SKIPPED
            ),
        },
    )
    return outcome


def _event_of(branch: Branch, order: Sequence[str]) -> dict:
    """一条子调用事件的形状：**id / 工具 / why / 状态 / 耗时 / 被拦原因**。

    这六样就是「调用链 + 调用声明」的全部素材：前端按 id 与 needs 画链，按
    why 显示它当时想干什么，按状态与原因决定标红还是标灰。事件在**过程区**，
    不进结论段。
    """
    return {
        "id": branch.call.id,
        "tool": branch.call.tool,
        "why": branch.call.why,
        "needs": list(branch.call.needs),
        "status": branch.status,
        "ok": branch.ok,
        "seconds": round(branch.seconds, 3),
        "reason": branch.reason,
        "parallel": branch.parallel,
        "isolation": branch.isolation,
        "injected": list(branch.injected),
        "index": order.index(branch.call.id) if branch.call.id in order else -1,
    }


def _run_parallel(
    calls: Sequence[PlanCall],
    runner: BranchRunner,
    emit: Callable[[str, dict], None],
    order: Sequence[str],
    done: dict[str, Branch],
) -> list[Branch]:
    """一个并发块。**只有只读分支会走到这里**（见 `_parallel_ok`）。"""
    upstream = {call.id: _upstream_of(call, done) for call in calls}
    if len(calls) == 1:
        return [
            _run_one(
                calls[0],
                runner,
                emit,
                order,
                parallel=True,
                upstream=upstream[calls[0].id],
            )
        ]
    workers = min(len(calls), max(1, len(calls)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            # `emit` 与 `order` 是**关键字**传的：`pool.submit` 只吃位置参数，
            # 而 `_run_one` 的 `parallel` 是 keyword-only——按位置传会撞在
            # 「多给了一个参数」上（冒烟一次就抓到了）。
            pool.submit(
                _run_one,
                call,
                runner,
                emit,
                order,
                parallel=True,
                upstream=upstream[call.id],
            )
            for call in calls
        ]
        return [future.result() for future in futures]


def _run_one(
    call: PlanCall,
    runner: BranchRunner,
    emit: Callable[[str, dict], None],
    order: Sequence[str],
    *,
    parallel: bool,
    upstream: dict[str, str] | None = None,
) -> Branch:
    """跑一条分支。**任何异常都收成一条失败分支**——一条分支炸掉不能带走整批。"""
    args, injected = _inject(dict(call.args), upstream or {})
    tool_call = ToolCall(name=call.tool, arguments=args, intent=call.why)
    started = time.time()
    try:
        result = runner.read(tool_call) if parallel else runner.serial(tool_call)
        seconds = time.time() - started
        status = runner.status(tool_call, result)
        branch = Branch(
            call=call,
            status=status,
            seconds=seconds,
            body=result.content,
            reason="" if status in (STATUS_OK, STATUS_STAGED) else result.content,
            parallel=parallel,
            isolation=C.needs_isolation(call.tool),
            usage=result.usage,
            injected=injected,
        )
        runner.note(tool_call, result)
    except Exception as exc:  # noqa: BLE001 - 分支异常不许拖垮整批
        branch = Branch(
            call=call,
            status=STATUS_FAILED,
            seconds=time.time() - started,
            reason=f"分支执行异常: {type(exc).__name__}: {exc}",
            parallel=parallel,
            isolation=C.needs_isolation(call.tool),
            injected=injected,
        )
    emit(EVENT_SUBCALL, _event_of(branch, order))
    return branch


def _upstream_of(call: PlanCall, done: dict[str, Branch]) -> dict[str, str]:
    """这条调用能拿到的上游输出（只取成功的那几条；失败的会在依赖判定里跳过）。"""
    return {
        name: done[name].body
        for name in call.needs
        if name in done and done[name].ok
    }


def _sum_usage(branches: Sequence[Branch]) -> Usage | None:
    """分支自己花的模型开销（派发 / research）也要记账。

    漏掉它，用量表报的就是「主循环自己花了多少」，而人会把它读成「这一步
    花了多少」——编排把一次派发藏进了一步之后，这个口径更容易被读错。
    """
    spent = [branch.usage for branch in branches if branch.usage is not None]
    if not spent:
        return None
    return Usage(
        prompt_tokens=sum(one.prompt_tokens for one in spent),
        completion_tokens=sum(one.completion_tokens for one in spent),
        calls=sum(one.calls for one in spent),
    )


# ---- 聚合 -------------------------------------------------------------------


def _tally_text(counts: dict[str, int]) -> str:
    order = (STATUS_OK, STATUS_STAGED, STATUS_FAILED, STATUS_BLOCKED, STATUS_SUSPENDED, STATUS_SKIPPED)
    parts = [f"{status} {counts[status]}" for status in order if counts.get(status)]
    return " / ".join(parts) if parts else "（没有分支）"


def _fit_bodies(bodies: Sequence[str], budget: int) -> list[int]:
    """按预算分配每条分支能留多少字：先平均分，剩下的补给被截断的那些。

    返回值是**每条能留多少个字符**。分配是确定性的（同样输入同样输出），
    所以聚合读数可复现。
    """
    count = len(bodies)
    if count == 0:
        return []
    budget = max(0, int(budget))
    per = budget // count
    sizes = [min(len(body), per) for body in bodies]
    leftover = budget - sum(sizes)
    if leftover > 0:
        for index, body in enumerate(bodies):
            if sizes[index] >= len(body):
                continue
            give = min(leftover, len(body) - sizes[index])
            sizes[index] += give
            leftover -= give
            if leftover <= 0:
                break
    return sizes


def _shrink(sizes: Sequence[int], over: int) -> list[int]:
    """超预算了：从**最长的那几条**里扣回来（扣到 0 为止）。

    为什么不「一超就把所有正文丢掉」：那是一条悬崖——超 1 个字与超 1000 个字
    得到同一个结果（所有正文都没了），而模型看到的是一整批「未展开」。逐步
    扣能把损失摊开，也正是聚合预算该有的样子：**先说有多少，再说细节留多少**。
    """
    sizes = list(sizes)
    remaining = max(0, over)
    while remaining > 0:
        widest = max(range(len(sizes)), key=lambda index: sizes[index])
        if sizes[widest] <= 0:
            break
        step = max(1, remaining // max(1, sum(1 for one in sizes if one > 0)))
        take = min(step, sizes[widest], remaining)
        sizes[widest] -= take
        remaining -= take
    return sizes


def _aggregate(
    plan: Plan, branches: Sequence[Branch], aggregate_chars: int
) -> tuple[str, bool]:
    """把各分支压成**一段紧凑上下文**，并如实说明截断。

    聚合里必须出现三样东西，缺一样都会让模型下一步做错事：
    ① 每一条的状态（哪条成功、哪条失败 / 被拦 / 挂起 / 跳过）；
    ② 失败那几条**已经给过的替代**（原文里的「照做的下一步」，不重新组织语言）；
    ③ §2.5 的反向条款（拦的是这一次动作，不是「不许删文件」）。

    ①②③ 是**必需**的，可省的只有各分支的正文——所以预算是这么花的：先扣掉
    必需部分，剩下的均分给正文；连必需部分都装不下时压成一行摘要，**不截断
    那三样**（宁可超出预算，也不让模型看不见「哪几条没成」与那条条款）。
    """
    budget = max(0, int(aggregate_chars))
    counts: dict[str, int] = {}
    for branch in branches:
        counts[branch.status] = counts.get(branch.status, 0) + 1
    lead = (
        f"编排完成（一步内 {len(branches)} 条调用）：{_tally_text(counts)}。"
        f"为什么这么编排：{plan.why}"
    )
    chain = [
        _chain_line(branch) for branch in branches
    ]
    head = lead + "\n调用链：\n" + "\n".join(chain) + "\n"

    footer: list[str] = []
    bad = [
        branch
        for branch in branches
        if branch.status in (STATUS_FAILED, STATUS_BLOCKED, STATUS_SUSPENDED)
    ]
    if bad:
        footer.append(
            "需要你知道的（失败 / 被拦 / 挂起，各条独立、其余分支照常做完了）："
        )
        for branch in bad:
            remedies = _remedies_of(branch.reason)
            footer.append(
                f"- {branch.call.id}（{branch.call.tool}）{branch.status}："
                f"{_flat(branch.reason, 240)}"
            )
            if remedies:
                footer.append(f"  已经给过的替代：{remedies[0]}")
    skipped = [branch for branch in branches if branch.status == STATUS_SKIPPED]
    if skipped:
        footer.append("跳过（依赖没成功，所以没有执行）：")
        for branch in skipped:
            footer.append(f"- {branch.call.id}（{branch.call.tool}）：{branch.reason}")
    footer.append(REVERSE_CLAUSE)
    if plan.aggregate:
        footer.append(f"这次编排的聚合口径（你自己的声明）：{plan.aggregate}")
    tail = "\n".join(footer) + "\n"

    def compose(sizes: Sequence[int]) -> tuple[str, bool]:
        lines: list[str] = []
        cut = False
        for branch, size in zip(branches, sizes):
            body = branch.body or ""
            kept = body[:size]
            if len(body) > size:
                cut = True
                if size >= MIN_BRANCH_CHARS:
                    kept += f"…（这一条超出聚合预算，截断 {len(body) - size} 字）"
                else:
                    # 分不到正文的分支只留状态行：把结果截成几十个字只是噪声，
                    # 不如如实说「它没被展开」。
                    kept = f"（正文因聚合预算未展开；{len(body)} 字）"
            lines.append(
                f"[{branch.call.id}] {branch.call.tool}（{branch.status}）\n{kept}"
            )
        return head + "\n".join(lines) + "\n" + tail, cut

    fixed = len(head) + len(tail)
    sizes = _fit_bodies(
        [branch.body for branch in branches], max(0, budget - fixed)
    )
    text, truncated = compose(sizes)
    if budget > 0 and len(text) > budget:
        # 「…截断 N 字」这几行自己也要占字，所以按正文长度算的额度会超。
        # 逐步从最长的那几条里扣回来（不是一把全丢，见 `_shrink`），直到装下。
        for _ in range(8):
            sizes = _shrink(sizes, len(text) - budget)
            text, cut = compose(sizes)
            truncated = truncated or cut
            if len(text) <= budget or not any(sizes):
                break
        if len(text) > budget:
            # 连「状态 + 条款」都装不下：压成一行摘要交付，**不截那三样**。
            text = (
                lead
                + " 状态："
                + "；".join(
                    f"{branch.call.id} {branch.call.tool}={branch.status}"
                    for branch in branches
                )
                + "\n"
                + tail
                + f"…（聚合预算 {budget} 字装不下逐条正文；这里**没有**截断分支"
                "状态、替代与 §2.5 条款——那是下一步必须知道的）\n"
            )
            truncated = True
    return text, truncated


def _chain_line(branch: Branch) -> str:
    """调用链里的一行：id / 工具 / 状态 / 耗时 / 依赖 / 意图。"""
    needs = "、".join(branch.call.needs) or "无"
    mark = "✓" if branch.ok else "✗"
    extra = ""
    if branch.isolation and branch.status not in (
        STATUS_BLOCKED,
        STATUS_SUSPENDED,
        STATUS_SKIPPED,
    ):
        # 只有**真的跑过**的分支才说「走了隔离」：被拦 / 挂起 / 跳过的那几条
        # 连工具都没进，说了会让人以为副本已经建过了。
        extra += " ｜ 走隔离（工具自己的试跑副本）"
    if not branch.parallel and C.can_parallel(branch.call.tool):
        extra += " ｜ 串行（要请批 / 要派发）"
    if branch.injected:
        extra += f" ｜ 已注入上游输出：{'、'.join(branch.injected)}"
    return (
        f"- {branch.call.id} {branch.call.tool} {mark} {branch.status} "
        f"{branch.seconds:.1f}s ｜ 依赖：{needs} ｜ 意图：{branch.call.why}{extra}"
    )


def _remedies_of(text: str) -> list[str]:
    """从被拦 / 失败的话术里抽出「照做的下一步」那几行（能照抄的调用形状）。"""
    return list(
        dict.fromkeys(
            line.strip()
            for line in (text or "").splitlines()
            if line.strip().startswith(NEXT_STEP_MARKER)
        )
    )


def _flat(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat[:limit] + "…" if len(flat) > limit else flat


# ---- 注册表里的那一件工具 ---------------------------------------------------


class Orchestrator:
    """`orchestrate` 与主循环之间的接线。

    **为什么要有这个类**：它是注册表里的一件工具，但它的分支必须走主循环那套
    护栏 / 请示 / 审计（`AgentLoop` 里那些）——工具层拿不到它们。所以工具的实现
    持有一个「主循环的分支通道」，由 `AgentLoop` 在构造时接上（`attach`）。
    **没接上时不假装能跑**：如实说「编排通道没接通，这一步只能一次一个调用」，
    并把原计划的形状端回去，而不是静默地一个一个跑（那会悄悄吃掉步数预算）。
    """

    def __init__(self, registry: Any, config: Any) -> None:
        self.registry = registry
        self.config = config
        self._loop: Any | None = None
        self._lock = threading.Lock()

    def attach(self, loop: Any) -> None:
        self._loop = loop

    @property
    def attached(self) -> bool:
        return self._loop is not None

    def handler(self, args: dict) -> ToolResult:
        max_calls = int(self.config.limit("orchestrate_max_calls"))
        plan, problem = parse_plan(
            args.get("plan"), self.registry, max_calls=max_calls
        )
        if plan is None:
            return ToolResult(ok=False, content=problem)
        if self._loop is None:
            return ToolResult(
                ok=False,
                content=(
                    "编排通道没有接通（这条运行路径上只能一次一个调用），"
                    "这一次计划**没有执行**。\n"
                    + _next_step(
                        "把计划拆成单步调用：一次发一个 tool_calls，"
                        "或者直接用那个工具。"
                    )
                ),
            )
        runner = self._loop.orchestration_runner()
        outcome = run_plan(
            plan,
            registry=self.registry,
            runner=runner,
            max_parallel=int(self.config.limit("orchestrate_max_parallel")),
            aggregate_chars=int(self.config.limit("orchestrate_aggregate_chars")),
        )
        return ToolResult(
            ok=outcome.ok,
            # 一条分支都没执行时带上硬拦前缀：主循环的「被拦的一步不占预算」
            # 判的就是它（`loop._is_blocked`）——整批没跑，这一步就不该吃预算。
            content=outcome.text
            if outcome.executed
            else BLOCKED_PREFIX + outcome.text,
            raw=outcome.evidence(),
            usage=outcome.usage,
        )


def plan_parameters() -> dict[str, Any]:
    """`plan` 的参数 schema。

    刻意只有**一个**参数，而且形状就是那份小计划本身：
    - `why`（计划级）与每条的 `why` 都是必填——「调用思路」正是这个工具要
      给出来的东西，缺了它就只剩一串工具名；
    - `needs` 是**调用 id 的数组**：依赖的输出**整体注入**下游，不做字段级
      取值模板（字段级要另一套语法，27B 写不对，收益也不值那个复杂度）。
    """
    return {
        "type": "object",
        "properties": {
            "why": {"type": "string", "description": "为什么这么编排（一句话）"},
            "calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "本次计划内唯一的短名（a1/b1）"},
                        "tool": {"type": "string", "description": "要调的工具名"},
                        "args": {"type": "object", "description": "那个工具的参数，照它的参数表写"},
                        "why": {"type": "string", "description": "这次调用想拿到什么"},
                        "needs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "依赖的调用 id（没有就写 []）。依赖的输出**整体**注入"
                                "这一条的参数：在参数里写 {{id}} 会换成那一条的结果；"
                                "只想要顺序、不要输出时不写占位符"
                            ),
                        },
                    },
                    "required": ["id", "tool", "args", "why"],
                },
                "description": "一次要发的调用，按依赖关系排好",
            },
            "aggregate": {"type": "string", "description": "怎么把这些结果合成一段"},
        },
        "required": ["why", "calls", "aggregate"],
    }


def orchestrate_spec(orch: Orchestrator) -> ToolSpec:
    """构造编排工具。它绑定的是**主循环那一套**注册表与分支通道。"""
    return ToolSpec(
        name="orchestrate",
        description=(
            "**一步内发出并执行多个工具调用**：写一份小计划（每条：调谁、参数、"
            "想拿到什么、依赖哪几条），系统按依赖执行——只读的几条**并行**跑，"
            "会改文件的**串行**跑（改动照样进待确认清单、照样可整轮回退），"
            "然后把各条结果压成**一段**交回给你。"
            "用它把「取数 + 归纳」这类原本要十几轮的活压到一轮："
            "先搜/读几处、再据此写文件，就是一份典型计划。"
            "每一次调用都必须写 why（想拿什么），计划本身也要写 why 与 aggregate"
            "（结果怎么合成一段）。某一条失败或被拦**不影响**别的分支，"
            "聚合里会如实说明哪几条没成、给了什么替代。"
            "一条计划最多 8 条调用（可用 limits 调）。依赖用 id 指代：写了 needs "
            "就保证顺序，参数里写 {{id}} 则把那一条的输出**整体**注入进来"
            "（例如先 read_file 得到 a1，再 write_file 用 content=\"{{a1}}\"）。"
        ),
        parameters={
            "type": "object",
            "properties": {"plan": plan_parameters()},
            "required": ["plan"],
            "additionalProperties": False,
        },
        handler=orch.handler,
        # 索引里只占一行：这个工具是**每个请求都要付的税**的一部分，所以
        # brief 写到「一步内批量执行」就够，完整形状走 tool_help("orchestrate")。
        brief="一步内批量执行多个工具调用",
        group="派",
    )
