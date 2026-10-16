"""计划相关命令：生成、推进、自主执行。

三条路径共用同一套「执行一步并结算」的逻辑。共用是刻意的：
各写一遍的结果，是策略在其中一条路径上失效且不报错，
只让用户以为「我明明设了只读」。
"""

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from spoolkit.agents.plan import (
    BATCH_LIMIT_NOTE,
    DONE,
    FAILED,
    PENDING,
    SELF,
    SUBAGENT,
    Plan,
    PlanStep,
    decompose,
    load_plan,
    narrow_scope,
    plan_path,
    render_step_prompt,
    save_plan,
    extend_plan,
    out_of_scope,
    reflect_progress,
    uncovered,
)
from spoolkit.cli.events import EventWriter
from spoolkit.llm.gateway import CountingGateway
from spoolkit import limits
from spoolkit import runlog
from spoolkit.cli.options import (
    report_policy,
    resolve_policy,
    resolve_scope,
    resolve_window,
)
from spoolkit.config import Config
from spoolkit.policy import AUTO
from spoolkit.cli.runtime import (
    LoopWiring,
    assemble_loop,
    build_approver,
    build_lessons,
    counter_for,
    open_memory,
    provider_gateway,
    bind_model_wait,
    settle_lessons,
)
from spoolkit.cli.settle import settle, undo_command, verification_header
from spoolkit.kernel.target import resolve_target
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.grant import Grants
from spoolkit.memory.gate import sanitize
from spoolkit.memory.lessons import record_lesson
from spoolkit.web.protocol import FINAL, START, STEP, USAGE


def _say(line: Any = "") -> None:
    """计划/自主这条路上**人读的散文**一律走 stderr。

    stdout 在这条路上是**事件通道**：`--events` 时探针、MCP 与网页壳按行读
    JSON（`web.protocol.parse_line`），一行散文就让录下来的文件变脏、解析器
    要么崩、要么被迫打补丁——而"录下来的文件能直接读"是排查的第一个手段。

    实测这条路上每一句进度原先都打在 stdout 上（`run --plan` / `run
    --autonomous` 都走这里），于是"stdout 上只有 JSON 行"这条约定在这两条路上
    从来没成立过。散文换出口、内容一个字不改：终端里 stderr 也接屏幕，人照样
    看得见；管道那一边拿到的 stdout 终于是干净的。
    """
    print(line, file=sys.stderr, flush=True)


def _events(args: argparse.Namespace) -> EventWriter | None:
    """这一次运行要不要发事件。

    开关与 `run` 那条路**同一个**（`args.events`），不另立一套：同一个策略在
    终端、网页与 MCP 上表现不同，那种差异不会报错，只会让人困惑。
    不开事件时 stdout 上不该出现 JSON 行，所以这里给 None。
    """
    return EventWriter() if getattr(args, "events", False) else None


def _ask_on_stderr(prompt: str) -> str:
    """确认问句也走 stderr，并把回答从 stdin 读回来。

    `input()` 把提示语写在 **stdout** 上（那是 CPython 的行为），events 模式下
    一行散文就让记录不干净；而这条路上 stdin 正是客户端用来回答的那条管道
    （`--ask-on-stdin`）。读不到（EOF/管道关了）时按「没回答」处理——
    比让 `input()` 抛 `EOFError` 把整轮炸掉强。
    """
    _say(prompt.rstrip("\n"))
    try:
        return sys.stdin.readline()
    except (EOFError, OSError):
        return ""


@dataclass
class _Totals:
    """这一轮（可能多步）的用量合计。

    USAGE 事件与收尾那一条都读它：按步执行那条路每一步都新建一次循环，
    各步的用量不累计的话，客户端看到的永远是**最后一步**的账。
    """

    steps: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, result: Any) -> None:
        self.steps += int(getattr(result, "steps", 0) or 0)
        self.calls += int(getattr(result, "model_calls", 0) or 0)
        self.prompt_tokens += int(getattr(result, "prompt_tokens", 0) or 0)
        self.completion_tokens += int(getattr(result, "completion_tokens", 0) or 0)

    def emit(self, writer: EventWriter | None) -> None:
        if writer is None:
            return
        writer.emit(
            USAGE,
            steps=self.steps,
            calls=self.calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


def _emit_start(
    writer: EventWriter | None,
    *,
    session: str,
    goal: str,
    mode: str,
    policy: str = "",
    steps: int = 0,
) -> None:
    """开工那一条。字段与 `run` 那条路的 START 对齐（网页壳与 MCP 按它开一轮）。"""
    if writer is None:
        return
    writer.emit(
        START,
        session=str(session or ""),
        goal=str(goal or ""),
        policy=str(policy or ""),
        mode=mode,
        steps=int(steps),
    )


def _emit_step(writer: EventWriter | None, step: Any, plan: Any) -> None:
    """第几步 / 共几步 / 这一步做什么。

    `n` 是网页壳与探针读的那个键（`web.runner.trace_line` 按它画「第 N 步」），
    别改名。
    """
    if writer is None:
        return
    writer.emit(
        STEP,
        n=int(getattr(step, "index", 0) or 0),
        total=len(getattr(plan, "steps", ()) or ()),
        goal=str(getattr(step, "goal", "") or ""),
        executor=str(getattr(step, "executor", "") or ""),
    )


def _emit_final(
    writer: EventWriter | None,
    *,
    ok: bool,
    text: str,
    mode: str,
    completed: int = 0,
    total: int = 0,
    seconds: float = 0.0,
    **extra: Any,
) -> None:
    """收尾那一条。**每条显式出口都要发**：不发就是"卡住了"（客户端只等到进程消失）。

    形状与 `run` 那条路的 FINAL 一致（`ok` + `text`，网页壳据此画最后那个气泡），
    多带三个本来只存在于终端那句「自主运行结束：完成 3/3 步」里的读数
    （`mode` / `completed` / `total`）——它们原先没有机读形式。

    进程**带着异常**死掉时这里发不出来（没有 try/finally）：那一条由调用方兜底，
    `web.runner` 在子进程消失且没收到 final 时会补一条（`web/runner.py`）。
    """
    if writer is None:
        return
    writer.emit(
        FINAL,
        ok=bool(ok),
        text=str(text or ""),
        mode=mode,
        completed=int(completed),
        total=int(total),
        seconds=round(float(seconds), 1),
        **extra,
    )


def _survey(project_root: Path, goal: str, tokenizer) -> str:
    """拆解**之前**先收集环境信息。

    这一步不是可选的。实测：不看的拆解产出的是**形状完美、内容全错**的计划
    ——20 步、每步都有验收标准和范围，而范围写的是 `task_1/`…`task_20/`
    （真实目录是 `01_off_by_one`…）。那些目录根本不存在，于是每一步的
    范围闸门都会把改动挡在外面，整份计划等于没有。

    收集什么由目标决定：先给目录结构（名字对了，范围才可能对），
    再给检索到的相关代码片段——都不多，够它对齐名字即可。
    """
    lines: list[str] = []
    # 任务材料：说「要做什么」的那些文件（TASK/README/*.md）与验收测试。
    #
    # 这一段是**效率的关键**：拆解时读不到它们，步骤就只能写成
    # 「修复 02_empty_input 中的空输入处理缺陷」这种笼统的话（实测就是这样），
    # 于是每个执行者都得自己去翻一遍——每题多一轮 survey，而每轮还要把后续
    # 提示词一起撑大。契约在这里读一次，比在每个子任务里读一遍便宜得多。
    material_budget = int(limits.resolve("decompose_material_budget", {})[0])
    material: list[str] = []
    used = 0
    exhausted = False
    for home in sorted(p for p in project_root.iterdir() if p.is_dir()):
        if exhausted:
            break
        if home.name == ".agent":
            continue
        for item in sorted(home.iterdir()):
            if not item.is_file():
                continue
            name = item.name.lower()
            is_material = name.endswith(".md") or name.startswith("test_")
            if not is_material:
                continue
            try:
                body = item.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            block = f"### {home.name}/{item.name}\n{body}"
            cost = tokenizer.count(block)
            if used + cost > material_budget:
                # 预算用完了：说一句就够。原先每个目录报一行，50 个目录就是
                # 五十行噪音——而每一批拆解都要把这段材料重发一遍。
                exhausted = True
                break
            material.append(block)
            used += cost
    if exhausted:
        material.append(
            f"（材料预算 {material_budget} token 用完，只展开了 {used} token 的部分，"
            "其余条目没有展开——需要时直接看工作区里的文件）"
        )
    if material:
        lines.append("各题目的材料（题目说明与验收测试）：\n" + "\n\n".join(material))
    try:
        entries = sorted(
            item.name + ("/" if item.is_dir() else "")
            for item in project_root.iterdir()
            if item.name != ".agent"
        )
        if entries:
            lines.append("工作区根目录下的条目：" + "、".join(entries[:60]))
    except OSError:
        pass
    from spoolkit.index.rank import prefetch as prefetch_text
    from spoolkit.tools.dispatch import dispatch_history

    try:
        snippet = prefetch_text(project_root, goal, tokenizer)
    except Exception:
        snippet = ""
    if snippet:
        lines.append("检索到的相关片段：\n" + snippet)
    # 拆解那一步正是决定「谁来做」的时刻，把本工作区的派发战绩摆出来
    history = dispatch_history(project_root)
    if history:
        lines.append(history)
    return "\n\n".join(lines)


def _plan_phase(
    gateway,
    project_root: Path,
    args,
    limit: int | None = None,
    extra: str = "",
    context: str | None = None,
    out: Callable[[Any], None] = print,
) -> object:
    """拆解那一段：排完、对一遍覆盖、把这笔账说出来。

    账要说出来：拆解在实测里占墙钟的四分之一（50 题 3 次调用、约 5.5 分钟），
    而它原先只有总时间能看，「为什么慢」只能猜。

    覆盖检查也是这一步的活：目标说「把 50 道题都做对」，计划少一道，
    原先没有任何机制会发现。

    `out` 是这段散文的出口。`spool plan` 用它当**产品输出**（stdout，默认），
    而 `run --autonomous/--plan` 那条路上 stdout 是事件通道，调用方传 stderr
    出口（见 `_say`）——同一条路径两种通道，出口由调用方定。
    """
    counting = CountingGateway(gateway)
    # 覆盖名单由调用方给：什么算「必须覆盖」是关于目标的判断，harness 猜不准。
    # 早先这里自动拿「工作区顶层目录」当名单——那是为「N 道题摆成 N 个目录」
    # 这一种形状写的，放到真实项目上只会误报（src/ 覆盖了，docs/ 没覆盖，
    # 然后去补一堆没人要的步骤）。
    cover = tuple(
        part.strip()
        for part in str(getattr(args, "cover", "") or "").split(",")
        if part.strip()
    )
    started = time.time()
    steps_log: list[str] = []
    # 材料只读一次：分批推进时续排也要用它（不给就会退化成笼统的步骤）。
    surveyed = (
        context
        if context is not None
        else _survey(project_root, args.goal, counter_for(gateway))
    )
    plan = decompose(
        counting,
        args.goal,
        context=surveyed,
        # 分批推进时，第一批只排 batch 步；一次排满就没有「复盘这一批」的机会了。
        limit=int(limit if limit is not None else args.limit),
        # `plan` 子命令没有 --window，`run --autonomous` 有：两边都要能用。
        window=resolve_window(gateway, getattr(args, "window", 0)),
        must_cover=cover,
        trace=steps_log,
        extra=extra,
    )
    elapsed = time.time() - started
    for line in steps_log:
        out("  " + line)
    out(
        f"拆解：{counting.calls} 次调用 / {counting.prompt_tokens} 输入 + "
        f"{counting.completion_tokens} 输出 token / {elapsed:.0f}s"
    )
    missing = uncovered(plan, cover)
    if missing:
        # 补了两轮还缺就报出来——报，不猜：它可能是有意不做的。
        out(
            f"⚠ 计划没有覆盖这些（{len(missing)} 个）："
            + "、".join(missing[:10])
            + ("…" if len(missing) > 10 else "")
        )
    return plan


def make_plan(args: argparse.Namespace) -> int:
    """把一个较大目标拆成可验收的步骤序列并落盘。"""
    # 与 `run` 同一套解析：`--session` 留空时用指针里那条，没有就回到 `cli`
    # （计划和进度是会话级的，两条命令必须落在同一条会话上）。
    target = resolve_target(
        workspace=args.root, session=args.session, client="cli", session_fallback="cli"
    )
    args.root, args.session = str(target.root), target.session
    project_root = Path(args.root).resolve()
    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2
    bind_model_wait(gateway, args)

    plan = _plan_phase(gateway, project_root, args)
    if not plan.steps:
        print("没有拆出任何带验收标准的步骤。", file=sys.stderr)
        return 1

    # 计划写在**这次解析出来的会话**里，与上面那句打印同一个来源。
    # 这里原先写的是 `getattr(ctx.args, "session", "cli")`——`ctx` 在这个函数里
    # 根本不存在（`_plan_phase` 收的才是 ctx），于是 `spool plan` 在落盘前就
    # `NameError`，计划从来没被写下来；而"plan 排的计划 run --plan 找得到"是
    # 计划正文写死的约束。Task 5 评审的 Critical-1 抓到的就是它。
    save_plan(plan_path(project_root, str(args.session)), plan)
    print(plan.render())
    print(f"\n计划已保存到 {plan_path(project_root, str(args.session))}")
    return 0


@dataclass
class StepRun:
    """一次步骤运行的上下文。

    这些参数在一次运行里全程不变，打包在一起的理由和 LoopWiring 一样：
    散成位置参数时，调用方看不出自己漏传了什么，而漏传的表现是
    「策略静默失效」，不是报错。
    """

    args: argparse.Namespace
    project_root: Path
    gateway: object
    plan: object
    non_interactive: bool
    #: 事件出口（不给 = 这条运行只有散文）。
    #:
    #: `--events` 时由 `autonomous` / `advance_plan` 建一个 `EventWriter` 传进来：
    #: 循环内部的每一条事件（note / tool / guard_refresh …）与这一步的 STEP
    #: 都从它出去。原先这条路上根本没有这个口——`assemble_loop` 收不到
    #: `on_event`，于是计划/自主跑的网页壳与 MCP 只能看到一个黑盒：没有过程、
    #: 没有 STEP、最后连一条 FINAL 都没有。
    writer: EventWriter | None = None
    #: 这一轮（可能多步）的用量合计：USAGE 事件在最后发一次，值就是它。
    totals: _Totals = field(default_factory=_Totals)
    #: 最后一步的结算结果与撤销命令。FINAL 事件带它们（与 `run` 那条路的
    #: `settled` / `revert_command` 同两个字段）：网页壳据此摆一个真能点的
    #: 撤销按钮，而不是让人去正文里正则捞命令。
    settled: str = "none"
    revert_command: str = ""

    @property
    def on_event(self):
        """交给 `AgentLoop` 的那个回调（签名 `(kind, data)`，见 `EventWriter.handle`）。

        循环里那条 `{"n": state.step}` 的 step 事件**在这里被挡掉**：

        - 计划路径自己发 STEP（`n` = 计划序号、带 `total` / `goal`），
        - 而循环那条 `n` 是**本次 run 内**的步数，每一步都从 0 起。

        两条都发，读 `n` 的消费方（网页壳 `web/page.py`、`web/runner.py`、
        MCP `mcp/server.py`）会在一条两步骤的计划里画出
        「第 1/2 步 → 第 0 步 → 第 2/2 步 → 第 0 步」——两个编号空间混在
        一条事件流里，读者没法判断"第 0 步"是什么。

        循环那条在 `run` 路径上照旧发（那里它是唯一的 STEP，
        `tests/agent/test_loop_events.py` 钉着它的形状）。其余事件一条不挡：
        tool / note / guard_refresh 这些正是客户端要看的"过程"。
        """
        if self.writer is None:
            return None
        writer = self.writer

        def handle(kind: str, data: dict) -> None:
            if kind == STEP and set(data) == {"n"}:
                return
            writer.handle(kind, data)

        return handle


def execute_step(ctx: StepRun, step, scope: Sequence[str], approver=None, grants=None):
    """执行一个计划步骤，返回（结果，待落盘改动）。"""
    from spoolkit.cli.commands.run import _resolve_read_roots

    args, project_root, gateway = ctx.args, ctx.project_root, ctx.gateway
    plan = ctx.plan
    if ctx.writer is None:
        # 授权范围那一句只在散文通道上说（与 `run` 那条路的 guard 同一个口径）：
        # `--events` 时 stdout 只能有 JSON 行，而这份读数已经在 START 事件上。
        report_policy(resolve_policy(args, project_root), scope, out=_say)
    window = resolve_window(gateway, args.window)
    read_roots, _ = _resolve_read_roots(args, project_root)
    pending = PendingChanges(
        project_root, extra_roots=read_roots, run_id=runlog.take_run_id(args)
    )
    # 运行记录要点名「这是哪一轮」：按步执行那条路每一**步**落一次盘，
    # 所以一轮 = 一步，id 与 goal 都跟着这一步走。
    pending.run_session = getattr(args, "session", "cli")
    pending.run_goal = getattr(step, "goal", "") or getattr(args, "goal", "")
    memory = (
        None
        if args.no_memory
        else open_memory(
            project_root,
            window,
            getattr(args, "session", "cli"),
            model=f"{args.provider}:{args.model or '默认'}",
        )
    )
    _wiring_started = time.time()
    loop = assemble_loop(
        project_root,
        gateway,
        config=Config(
            project_root=project_root,
            context_window=window,
            max_steps=args.max_steps,
            subagent_steps=args.subagent_steps,
            session=getattr(args, "session", "cli"),
        ),
        wiring=LoopWiring(
            memory=memory,
            pending=pending,
            approver=approver,
            grants=grants,
            lessons=build_lessons(memory) if memory is not None else None,
            read_roots=read_roots,
            # 这一步要动哪儿由计划声明，执行时把它交给预取——
            # 否则预取只能靠整段提示词里的关键词猜，实测会猜偏到测试文件上。
            prefetch_anchors=tuple(step.scope or scope),
            # 事件出口（D3）：接上就按行发给探针 / MCP / 网页壳；不接就只有
            # trace 与收尾报告。**这条不能在计划路径上漏**：漏了的表现不是
            # 报错，是客户端看到一个没有过程的黑盒。
            on_event=ctx.on_event,
        ),
    )
    # 必须在**装配完那一刻**就读走：晚一步（比如等 run 回来再读）量到的
    # 就是整步墙钟，而它会顶着"装配循环"这个名字，把账搅成 0 其它时间。
    _wiring_seconds = time.time() - _wiring_started
    if step.executor == SUBAGENT:
        result = _delegate_step(ctx, step, loop, pending)
    else:
        # goal 用**短目标**，整段步骤提示词只作为提示词发一次：
        # 状态块里再重复一份，等于每次调用多付一遍。
        result = loop.run(step.goal, prompt=render_step_prompt(plan, step))
    result.wiring_seconds = _wiring_seconds

    if memory is not None:
        settle_lessons(memory, result.lessons_pushed, result.finished)

    # 用量进这一轮的合计：USAGE 是**整轮**的账，不是最后一步的账。
    ctx.totals.add(result)
    for line in result.trace:
        _say(line)
    _say(result.usage())
    return result, pending


def _delegate_step(ctx: StepRun, step, loop, pending) -> object:
    """把这一步交给子智能体：独立上下文实现 + 另一个独立上下文审查。

    为什么要有这一支：`render_step_prompt` 那条路**也是**按「派发契约」的形状
    在喂——goal + 验收标准 + 范围——但由 harness 直接跑，于是派发的决定权与
    **独立审查**都不见了（实测那条路里 `dispatch` 用了 0 次，而验收只剩
    「跑测试」这一种机械判断，它看不出「这处改得是不是偷懒了」）。

    成了哪一步由**审查结论**定，不是由「测试过了」定；改动仍然落在同一份
    待确认里，用户只确认一次。
    """
    from spoolkit.agent.loop import LoopResult
    from spoolkit.agent.state import TaskState
    from spoolkit.agents.dispatcher import DispatchPlan, run_delegated
    from spoolkit.agents.runtime import TaskSpec

    spec = TaskSpec(
        goal=step.goal,
        targets=step.scope,
        acceptance=step.acceptance,
        constraints=("只做这一步，不要做后续步骤的工作",),
    )
    plan = ctx.plan
    outcome = run_delegated(
        DispatchPlan(True, "计划里声明了由子智能体执行", spec=spec),
        ctx.gateway,
        loop.tokenizer,
        loop.registry,
        loop.config,
        verify=loop.verify,
    )

    review = outcome.review
    if outcome.too_big:
        finished = False
        final = f"【太大】{outcome.too_big}"
    elif review is None:
        finished = False
        final = outcome.implementer_final
    else:
        finished = review.passed
        verdict = "通过" if review.passed else ("没有结论" if review.inconclusive else "未通过")
        reasons = "；".join(review.reasons)
        final = f"独立审查{verdict}：{reasons}" if reasons else f"独立审查{verdict}"

    trace = [f"[派发] {line}" for line in outcome.trace]
    if len(pending):
        trace.append(f"[派发] 这次改动涉及 {len(pending)} 个文件，仍在待确认里")
    return LoopResult(
        finished=finished,
        final=final,
        state=TaskState(task_id=f"step-{step.index}", goal=step.goal),
        steps=0,
        resets=0,
        trace=trace,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        model_calls=outcome.model_calls,
    )


def record_step(
    ctx: StepRun, step, result, pending, scope: Sequence[str]
) -> tuple[str, str]:
    """记录步骤结果并按策略处理待落盘改动。

    返回 `(结算结果, 撤销命令)`：撤销命令必须在 settle **之前**取（settle 会把
    待确认清单清空，之后再问只会拿到「没有可退的」），而事件通道的 FINAL 要
    带上它——网页壳据此摆一个真能点的按钮（与 `run` 那条路同一个字段）。
    """
    project_root, plan = ctx.project_root, ctx.plan
    note = (result.final or "").strip().splitlines()[0][:80] if result.final else ""
    outcome = "none"
    undo = undo_command(pending)

    dropped: list[str] = []
    if len(pending):
        # 越界的路径要在 settle **之前**取：settle 会把待确认清空
        # （落盘或丢弃），之后 items() 就是空的了。
        policy = resolve_policy(ctx.args, project_root)
        if policy == AUTO and ctx.non_interactive:
            dropped = out_of_scope([item.path for item in pending.items()], scope)
        outcome, _written = settle(
            pending,
            policy,
            scope,
            baseline_path=project_root / ".agent" / "last_change.json",
            non_interactive=ctx.non_interactive,
            # 与命令行那条路同一条要求：未通过的判词与撤销命令要排在 diff 之前。
            header=verification_header(
                getattr(result, "verified", None),
                getattr(result, "verify_summary", ""),
                undo,
            ),
            # 人读的散文（diff、留档、留档被拒的理由）走 stderr：这条路上
            # stdout 是事件通道，一大片 diff 混进去就等于 `--events` 的记录
            # 不能再直接读（`settle_with_events` 里 diff 走 stderr 是同一个理由）。
            out=_say,
            # 确认问句同样不能落在 stdout 上（`input()` 的提示语写 stdout）。
            # 只在事件通道上换：散文通道保持 `input` 的原样提示。
            ask=_ask_on_stderr if ctx.writer is not None else input,
        )
    if dropped:
        # **越界被丢掉这件事必须进记录**。实测代价：第 3 步改的是
        # `cli/app.py`，而它声明的范围写成了不存在的 `cli.py`——改动被丢掉，
        # 模型只看到"自动验证失败"（它不知道自己的写入根本没落盘），
        # 于是"写 → 验证失败 → 重置 → 再写"转到督导叫停。
        # 记进 note 之后：计划渲染、回头看的总结、续排的提示词都看得到，
        # 下一批就能把范围写全、或者把这一步拆开。
        note = (note + f"；（{len(dropped)} 处改动超出本步范围被丢弃："
                       + "、".join(dropped[:3])
                       + ("…" if len(dropped) > 3 else "") + "）")[:200]
    plan.mark(step.index, DONE if result.finished else FAILED, note=note)
    # `record_step` 只收 ctx，没有独立的 args——会话名从 ctx.args 拿。
    save_plan(plan_path(project_root, getattr(ctx.args, "session", "cli")), plan)
    return outcome, undo


def advance_plan(args: argparse.Namespace, project_root: Path, gateway) -> int:
    """执行计划中下一个待办步骤，并记录结果。"""
    writer = _events(args)
    totals = _Totals()
    started = time.time()
    session = getattr(args, "session", "cli")
    plan = load_plan(plan_path(project_root, session))
    if plan is None:
        # 「没有计划」也要先发 START 再发 FINAL（与下面正常那条同一个形状）：
        # 只发一条 FINAL 的话，按 START 建占位的客户端会看到一个没有开头的收尾。
        _emit_start(
            writer,
            session=session,
            goal=str(getattr(args, "goal", "") or ""),
            mode="plan",
            policy=resolve_policy(args, project_root),
        )
        _say("还没有计划，请先用 `plan --goal` 生成。")
        _emit_final(
            writer,
            ok=False,
            text="还没有计划，请先用 `plan --goal` 生成。",
            mode="plan",
            seconds=time.time() - started,
        )
        return 2

    _emit_start(
        writer,
        session=session,
        goal=str(getattr(plan, "goal", "") or ""),
        mode="plan",
        policy=resolve_policy(args, project_root),
        steps=len(plan.steps),
    )

    blocked = plan.blocked_by()
    if blocked is not None:
        # 自动重试一次：把 failed 步骤改回 pending 重跑。
        # dogfood 暴露：步骤失败后 resume 直接阻塞，没有重试机会——
        # 很多失败是模型一次性抽风（patch 格式错、参数拼错），重试就能过。
        if blocked.retries < 1:
            blocked.status = PENDING
            blocked.retries += 1
            save_plan(plan_path(project_root, session), plan)
            _say(
                f"第 {blocked.index} 步上次失败，自动重试"
                f"（第 {blocked.retries} 次）：{blocked.goal}"
            )
            step = blocked
        else:
            _say(plan.render())
            _say(
                f"\n计划被第 {blocked.index} 步阻塞：{blocked.goal}\n"
                f"原因：{blocked.note or '未记录'}\n"
                f"已自动重试 {blocked.retries} 次仍未通过。"
                "先处理它，或者用 `plan --goal` 重新生成计划。"
            )
            _emit_final(
                writer,
                ok=False,
                text=(
                    f"计划被第 {blocked.index} 步阻塞：{blocked.goal}"
                    f"（{blocked.note or '未记录'}）"
                ),
                mode="plan",
                total=len(plan.steps),
                seconds=time.time() - started,
                blocked_step=int(blocked.index),
            )
            return 1
    else:
        step = plan.next_pending()
        if step is None:
            _say(plan.render())
            _say("\n计划已全部完成。")
            _emit_final(
                writer,
                ok=True,
                text="计划已全部完成。",
                mode="plan",
                completed=len(plan.steps),
                total=len(plan.steps),
                seconds=time.time() - started,
            )
            return 0

    _say(f"执行第 {step.index}/{len(plan.steps)} 步：{step.goal}")
    # 打印的是**这一步实际生效**的范围，不是运行级默认值：
    # 安全边界的输出报错比不输出更糟——看到「**」会以为整个项目都放行了。
    scope = resolve_scope(args, step.scope, default=())
    if writer is None:
        approver, grants = build_approver(project_root)
    else:
        # 事件通道：命令授权要么走 stdin 上问一句（客户端能回答，见
        # `--ask-on-stdin`），要么干脆不装（无人值守一律拒绝、把动作挂起）。
        # **绝不能装终端那个 approver**——它把问句打在 stdout 上（污染事件流）
        # 并 `input()` 等人，而这条通道上没有人会看见那句话。
        from spoolkit.cli.commands.run import _events_approver

        approver = (
            _events_approver(writer) if getattr(args, "ask_on_stdin", False) else None
        )
        grants = Grants(project_root / ".agent" / "grants.json")
    ctx = StepRun(
        args=args,
        project_root=project_root,
        gateway=gateway,
        plan=plan,
        non_interactive=False,
        writer=writer,
        totals=totals,
    )
    _emit_step(writer, step, plan)
    result, pending = execute_step(
        ctx,
        step,
        scope,
        approver=approver,
        grants=grants,
    )
    outcome, undo = record_step(ctx, step, result, pending, scope)
    _say(plan.render())
    # USAGE 在 FINAL 之前发（与 `run` 那条路同一个次序：账在收尾之前）。
    totals.emit(writer)
    _emit_final(
        writer,
        ok=bool(result.finished),
        text=str(getattr(result, "report_text", "") or result.final or ""),
        mode="plan",
        completed=1 if result.finished else 0,
        total=len(plan.steps),
        seconds=time.time() - started,
        concluded=bool(getattr(result, "concluded", False)),
        terminated_by=str(getattr(result, "terminated_by", "") or ""),
        failed_reason=str(getattr(result, "failed_reason", "") or ""),
        model_final=str(getattr(result, "model_final", "") or ""),
        # 结算结果与撤销命令：网页壳据此摆一个真能点的按钮（与 `run` 那条路
        # 的 FINAL 同两个字段）。撤销命令在 settle 之前取——之后清单就空了。
        settled=outcome,
        revert_command=undo,
    )
    return 0 if result.finished else 1


import re

_SINGLE_FILE_PATTERNS = [
    re.compile(r"单个\s+\S+\.(?:html?|py|js|ts|tsx|jsx|css|json|md|txt|sh|go|rs|java|c|cpp|h)\b"),
    re.compile(r"一个\s+\S+\.(?:html?|py|js|ts|tsx|jsx|css|json|md|txt|sh|go|rs|java|c|cpp|h)\b"),
    re.compile(r"单个文件.*\.(?:html?|py|js|ts|tsx|jsx|css|json|md|txt)\b"),
]


def _looks_like_single_file_task(goal: str) -> bool:
    """检测目标是否是「新建单个文件」类任务。

    dogfood 暴露：分步改同一文件每步生成 patch 容易死循环。这类任务
    一次写完比拆成 9 步更靠谱。检测靠关键词——不调模型，规则就够。

    匹配：「做一个网页版贪吃蛇游戏：单个 index.html...」
    不匹配：「把 src/ 下的测试都修好」（多文件）
    """
    for pattern in _SINGLE_FILE_PATTERNS:
        if pattern.search(goal):
            return True
    return False


def autonomous(args: argparse.Namespace, project_root: Path, gateway) -> int:
    """自己拆解目标并逐步做完。

    唯一不能交给模型的是**授权范围**：如果它既能提范围又能批范围，
    就等于自己给自己发许可证。所以范围必须由 --scope 给出，
    模型只能在里面收窄，不能扩大。

    **每条显式出口都要发 FINAL**（`--events` 时）：客户端是等这一条来收尾的，
    少了它，一次正常的「没能拆出步骤」看起来与"进程挂了"没有区别。进程级异常
    没有 try/finally 兜（与 `run` 那条路同一个口径），由 `web.runner` 在子进程
    消失且没收到 final 时补一条。
    """
    writer = _events(args)
    totals = _Totals()
    started = time.time()
    session = getattr(args, "session", "cli")
    granted = tuple(part.strip() for part in args.scope.split(",") if part.strip())
    _emit_start(
        writer,
        session=session,
        goal=args.goal,
        mode="autonomous",
        policy=resolve_policy(args, project_root),
    )
    if not granted:
        _say(
            "自主模式必须用 --scope 指定授权范围。\n"
            "授权必须来自你——让模型自己定范围，等于它自己给自己发许可证。\n"
            "例如：--scope scratch_lab/"
        )
        _emit_final(
            writer,
            ok=False,
            text="自主模式必须用 --scope 指定授权范围。",
            mode="autonomous",
            seconds=time.time() - started,
        )
        return 2

    batch = int(getattr(args, "batch", 0) or 0)
    rolling = batch > 0 and not getattr(args, "no_roll", False)
    # 单文件新建任务不需要分步——分步改同一文件每步生成 patch 容易死循环
    #（dogfood：贪吃蛇 9 步计划第 2 步 write_file 死循环）。
    # 检测到就跳过拆解，构造单步计划直接执行，一次写完绕开 patch。
    if _looks_like_single_file_task(args.goal):
        _say("检测到单文件任务，跳过拆解直接执行（一次写完，绕开 patch 死循环）")
        plan = Plan(
            goal=args.goal,
            steps=[PlanStep(
                index=1, goal=args.goal,
                acceptance="文件创建成功且内容完整可运行",
                scope=granted, executor=SELF,
            )],
        )
        save_plan(plan_path(project_root, session), plan)
        _say(f"授权范围：{'、'.join(granted)}")
        _say(plan.render())
        ctx = StepRun(
            args=args, project_root=project_root,
            gateway=gateway, plan=plan, non_interactive=True,
            writer=writer, totals=totals,
        )
        if _run_one_step(ctx, granted, plan.steps[0], plan):
            _say(f"\n自主运行结束：完成 1/1 步。")
            totals.emit(writer)
            _emit_final(
                writer,
                ok=True,
                text="自主运行结束：完成 1/1 步。",
                mode="autonomous",
                completed=1,
                total=1,
                seconds=time.time() - started,
                settled=ctx.settled,
                revert_command=ctx.revert_command,
            )
            return 0
        _say(plan.render())
        totals.emit(writer)
        _emit_final(
            writer,
            ok=False,
            text="自主运行结束：完成 0/1 步。",
            mode="autonomous",
            completed=0,
            total=1,
            seconds=time.time() - started,
            settled=ctx.settled,
            revert_command=ctx.revert_command,
        )
        return 1
    # 分批推进时第一批只排 batch 步：一次排满就没有「复盘这一批」的机会了。
    # 材料读一次，第一批与后面的续排共用——不给续排材料，它排出来的
    # 步骤会退化成"修复 04 目录下的编程题"这种话（实测）。
    context = _survey(project_root, args.goal, counter_for(gateway))
    plan = _plan_phase(
        gateway,
        project_root,
        args,
        limit=min(int(args.limit), batch) if rolling else int(args.limit),
        # 分批推进时第一批必须补这一句：只说"最多 N 步"会让模型把多件事
        # 并成一条步骤（实测那一步跑了 191 秒）。
        extra=BATCH_LIMIT_NOTE.format(limit=batch) if rolling else "",
        context=context,
        # 自主路径的散文走 stderr（stdout 是事件通道）。
        out=_say,
    )
    if not plan.steps:
        _say("没能拆出任何带验收标准的步骤。")
        _emit_final(
            writer,
            ok=False,
            text="没能拆出任何带验收标准的步骤。",
            mode="autonomous",
            seconds=time.time() - started,
        )
        return 1
    save_plan(plan_path(project_root, session), plan)
    _say(f"授权范围：{'、'.join(granted)}")
    _say(plan.render())

    completed = 0
    ctx = StepRun(
        args=args,
        project_root=project_root,
        gateway=gateway,
        plan=plan,
        non_interactive=True,
        writer=writer,
        totals=totals,
    )
    # 教训要写进记忆库：分批推进的复盘结论只有存下来，才能在后面的同类任务里
    # 被主动推送（那才是"在任务中成长"）。--no-memory 时不建，也不留状态。
    lesson_memory = (
        None
        if getattr(args, "no_memory", False)
        else open_memory(
            project_root,
            resolve_window(gateway, getattr(args, "window", 0)),
            session,
            model=f"{getattr(args, 'provider', '')}:{getattr(args, 'model', '') or '默认'}",
        )
    )
    # 分批推进：先做一批 → 复盘这一批 → 再排下一批。
    #
    # 这样每一批都能吃到前一批的**实际结果**（某个接口不是那样、某个约束
    # 计划里没料到），而不是把五十步一次排死。要看完整目标时才用 --no-roll。
    cursor = 0
    failed_step = 0
    while True:
        steps = plan.steps[cursor:]
        if not steps:
            break
        for step in steps:
            cursor = plan.steps.index(step) + 1
            if not _run_one_step(ctx, granted, step, plan):
                failed_step = int(step.index)
                break
            completed += 1
        if failed_step:
            break
        if not rolling or completed >= int(getattr(args, "limit", 0) or 0):
            break
        # 这一批做完了：复盘这一批，再排下一批。
        reflection, lessons = reflect_progress(gateway, plan.goal, plan.steps)
        if reflection:
            _say(f"\n这一批复盘：{reflection}")
        stored = _keep_lessons(lessons, lesson_memory)
        if stored:
            _say(f"（记下 {stored} 条教训——后面同类任务会主动推给它）")
        fresh = extend_plan(
            gateway,
            plan,
            reflection,
            limit=min(batch, max(0, int(getattr(args, "limit", 0) or 0) - len(plan.steps))),
            context=context,
            window=resolve_window(gateway, getattr(args, "window", 0)),
            trace=[],
        )
        if not fresh:
            # 它说没有更多步骤了。这是**它的判断**，如实报出来。
            _say("\n续排：模型认为目标已经排完（没有更多步骤）。")
            break
        for step in fresh:
            step.index = len(plan.steps) + 1
            plan.steps.append(step)
        save_plan(plan_path(project_root, session), plan)
        _say(plan.render())

    _say(plan.render())
    summary = f"\n自主运行结束：完成 {completed}/{len(plan.steps)} 步。"
    _say(summary)
    totals.emit(writer)
    _emit_final(
        writer,
        ok=not failed_step and completed == len(plan.steps),
        text=summary.strip(),
        mode="autonomous",
        completed=completed,
        total=len(plan.steps),
        seconds=time.time() - started,
        # 最后一步的结算结果与撤销命令（多步时也只给最后一个：撤销按钮
        # 一次退的是一整轮运行，见 `undo_command` 的 run_id 口径）。
        settled=ctx.settled,
        revert_command=ctx.revert_command,
        **({"failed_step": failed_step} if failed_step else {}),
    )
    return 0 if not failed_step and completed == len(plan.steps) else 1


def _keep_lessons(lessons, memory) -> int:
    """把复盘得来的教训写进教训库，返回写了几条。

    没有记忆会话（`--no-memory`）时就丢掉——这是刻意的：不写记忆的运行
    不该静默留下状态。

    教训的**触发词**由模型给（"什么时候该把这条推出来"）。没有它这条教训
    永远不会被推送，所以解析时已经把没触发词的丢掉了。
    """
    if not lessons or memory is None:
        return 0
    for rule, trigger in lessons:
        # 这是 `archive_task` 之外的第二个直接写库点，同样要过写入侧脱敏：
        # 密钥不能因为「从复盘来的」就落进记忆库（设计稿 §5.3 ①）。
        clean_rule, _ = sanitize(rule)
        record_lesson(
            memory._conn,
            clean_rule,
            trigger,
            source="分批推进的回头看",
        )
    return len(lessons)


def _run_one_step(ctx, granted: tuple[str, ...], step, plan) -> bool:
    """跑一步并记账。返回它是否做完了。"""
    blocked = plan.blocked_by()
    if blocked is not None:
        _say(
            f"\n计划被第 {blocked.index} 步阻塞：{blocked.goal}"
            f"（{blocked.note or '未记录'}）"
        )
        return False

    effective = _effective_scope(granted, step)
    _emit_step(ctx.writer, step, plan)
    _say(f"\n执行第 {step.index}/{len(plan.steps)} 步：{step.goal}")
    _step_started = time.time()
    result, pending = execute_step(
        ctx,
        step,
        effective,
        approver=None,
        grants=Grants(path=ctx.project_root / ".agent" / "grants.json"),
    )
    _step_seconds = time.time() - _step_started
    ctx.settled, ctx.revert_command = record_step(ctx, step, result, pending, effective)
    _settle_seconds = time.time() - _step_started - _step_seconds
    _report_step_cost(result, _step_seconds, _settle_seconds)
    return bool(result.finished)


def _report_step_cost(result, step_seconds: float, settle_seconds: float) -> None:
    """把这个**步**的墙钟拆开。

    前面那笔"4.6 秒/步没人认领"是从残差推出来的，而残差推不出结论——
    所以这里把每一步的墙钟当场拆成几笔打出来。谁占大头一眼就看得到，
    不必再去猜。
    """
    known = (
        result.wiring_seconds
        + result.model_seconds
        + result.tool_seconds
        + result.assemble_seconds
        + result.verify_seconds
        + result.state_seconds
        + result.supervisor_seconds
        + settle_seconds
    )
    other = max(0.0, step_seconds + settle_seconds - known)
    _say(
        f"本步墙钟 {step_seconds + settle_seconds:.1f}s ｜ "
        f"模型 {result.model_seconds:.1f} 装配循环 {result.wiring_seconds:.1f} "
        f"装配上下文 {result.assemble_seconds:.1f} 工具 {result.tool_seconds:.1f} "
        f"验证 {result.verify_seconds:.1f} 落盘 {result.state_seconds:.1f} "
        f"收尾 {settle_seconds:.1f} 其它 {other:.1f}"
    )


def _effective_scope(granted: tuple[str, ...], step) -> tuple[str, ...]:
    """把这一步的范围收窄到授权之内。

    提议被全部驳回时回退到你授予的范围，而不是回退成「什么都不许」。
    授权来自你；步骤提议只是模型想进一步收窄的意愿，它不该有
    把整步变成只读的能力。

    另外**永远允许写 `.agent/`**：那是 agent 自己的工作目录（计划、进度、
    索引、scratch、基线）。不加这一条会出现一个自相矛盾：提示词里让它把
    临时脚本写进 `.agent/scratch/`，而范围闸门又把那处改动丢掉——实测就
    这么丢过（一个真实需求里它想写个验证脚本，结果被挡，只看到"验证失败"）。
    这不是给模型扩权：`.agent/` 不是用户的代码。
    """
    effective, rejected = narrow_scope(granted, step.scope or granted)
    if rejected:
        _say(
            f"第 {step.index} 步提出的范围超出授权，已收窄："
            + "、".join(rejected)
        )
    allowed = tuple(effective or granted)
    if not any(item.startswith(".agent") for item in allowed):
        allowed = allowed + (".agent/",)
    return allowed


def enter_autonomous(args: argparse.Namespace, project_root: Path) -> int:
    """装配网关后进入自主模式。"""
    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2
    bind_model_wait(gateway, args)
    return autonomous(args, project_root, gateway)


def enter_advance(args: argparse.Namespace, project_root: Path) -> int:
    """装配网关后推进计划。"""
    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2
    bind_model_wait(gateway, args)
    return advance_plan(args, project_root, gateway)
