"""默认命令：跑一次任务。

三条分支——派发、计划、自主——在装配网关之后分流。共同的部分
（窗口解析、网关装载、授权询问器）放在这里，避免各分支重复。
"""

import argparse
import sys
from pathlib import Path

from spoolkit.agents.dispatcher import plan_dispatch, run_delegated
from spoolkit.config import Config
from spoolkit.cli.commands.plan import advance_plan, autonomous
from spoolkit import diagnosis
from spoolkit import runlog
from spoolkit.check.numbers import render as render_numbers
from spoolkit.check.numbers import actionable, mismatched, summarize, untraceable
from spoolkit.cli.options import (
    report_policy,
    resolve_policy,
    resolve_scope,
    resolve_window,
)
from spoolkit.cli.runtime import (
    LoopWiring,
    assemble_loop,
    build_approver,
    web_access,
    web_search_backend,
    build_lessons,
    distiller_for,
    open_memory,
    prompt_for_turn,
    provider_gateway,
    bind_model_wait,
    settle_lessons,
    show_history,
)
from spoolkit.cli.settle import (
    pending_actions_note,
    settle,
    undo_command,
    verification_header,
)
from spoolkit.cli.events import EventWriter, settle_with_events
from spoolkit.kernel.target import resolve_target
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.memory.transcript import ASSISTANT, USER, record_message
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.grant import Grants
from spoolkit.web.protocol import AWAIT, CONFIRM, FINAL, NOTE, START, TOOL, USAGE


def _overrides(args: argparse.Namespace, project_root: Path) -> dict[str, float]:
    """本次运行生效的能力标定覆盖。

    来源两层，从弱到强：工作区保存的（`.agent/limits.json`）→ 命令行显式给的。
    默认值在 `limits.KNOBS` 里（按本机 27B 实测的那一套，不是普适真理）。
    取用时一律走 `Config.limit()`，这样「现在是多少、从哪来」永远答得出来。
    """
    from spoolkit import limits

    merged = limits.load_overrides(project_root)
    if getattr(args, "max_targets", 0):
        merged["max_targets"] = float(args.max_targets)
    if getattr(args, "review_limit", 0):
        merged["review_limit"] = float(args.review_limit)
    return merged


def run(args: argparse.Namespace) -> int:
    """执行一次任务。"""
    # 会话与工作区只在这里定一次（内核的解析顺序：显式 > 当前目录向上找 > 指针 >
    # 登记表 > 当前目录）。`--session` 留空时才轮到指针，`cli` 是这条入口一直以来的
    # 默认名——留着它，老会话与老计划才接得上。
    target = resolve_target(
        workspace=args.root, session=args.session, client="cli", session_fallback="cli"
    )
    args.root, args.session = str(target.root), target.session
    project_root = Path(args.root).resolve()
    # 一次入口 = 一轮：id 在这里发一次，后面每一次落盘都记进同一份运行记录。
    # 计划那条路每一步都会新建待确认清单，但它们共用这一个 id——用户反悔时
    # 要退掉的是**整次调用**，不是最后一步。
    args.run_id = runlog.take_run_id(args)
    overrides = _overrides(args, project_root)
    read_roots, bad_roots = _resolve_read_roots(args, project_root)
    if bad_roots:
        for item in bad_roots:
            print(f"--allow-read 指向的目录不存在: {item}", file=sys.stderr)
        return 2

    # 走计划时目标来自计划文件，命令行不该再强制要求填一次。
    if not args.plan and not args.goal.strip():
        print("需要 --goal，或者用 --plan 推进已有计划。", file=sys.stderr)
        return 2

    gateway = provider_gateway(args, project_root)
    if gateway is None:
        return 2
    bind_model_wait(gateway, args)

    window = resolve_window(gateway, args.window)
    # --events 模式下 stdout 上只能有 JSON 行。散文会被解析器忽略，
    # 但录下来的文件会变脏，调试时难读——而那正是我们的第一个观察手段。
    if not args.events:
        print(f"上下文窗口：{window} token")

    if args.plan:
        return advance_plan(args, project_root, gateway)
    if args.autonomous:
        # autonomous 是无人值守模式：policy 留空时会沿用工作区默认（通常是 ask），
        # 写操作全被拒、白跑一趟。这里自动提权为 auto——边界仍由 --scope 守着，
        # 没给 scope 它自己会拒绝。用户显式给了 --policy 就尊重用户的选择。
        if not args.policy:
            args.policy = "auto"
            if not args.events:
                print("autonomous 模式：未指定 --policy，自动提权为 auto（边界仍由 --scope 守）")
        return autonomous(args, project_root, gateway)

    if not args.events:
        report_policy(resolve_policy(args, project_root), resolve_scope(args))
        # 有人来敲门时要看得见：`--events` 模式下 stdout 只能有 JSON 行，
        # 所以只在散文模式提示（网页壳那条路自己会显示待确认）。
        from spoolkit.cli.commands.bridge import pending_notice

        notice = pending_notice(project_root)
        if notice:
            print(notice)
    pending = PendingChanges(project_root, extra_roots=read_roots, run_id=args.run_id)
    # 运行记录要知道这一轮是谁、为了什么跑的：`.agent/runs/<id>/run.json` 里
    # 记下它们，事后（`spool revert --run <id>`）才答得出「这是哪一轮」。
    pending.run_session = args.session
    pending.run_goal = args.goal
    if args.events:
        return _events_mode(args, project_root, gateway, window, pending)
    if args.delegate:
        return _delegated(args, project_root, gateway, window, pending)
    return _standard(args, project_root, gateway, window, pending)


def _events_approver(writer: EventWriter, reader=None):
    """events 模式下的命令授权询问器：**把问题发出去，再从 stdin 读回答**。

    为什么必须补这个：原先 events 模式（= 网页壳与聊天桥）**没有 approver**，
    工具于是回模型一句"当前无人可询问（无人值守运行）"。真实后果在 QQ 那轮里
    看得很清楚——它想跑一条 `find` 找文件，被拒 → 换个写法再试 → 连续四次
    之后触发重复保护、整轮收尾，**任务在原地死掉**，而用户那边只看到
    "任务没做完"。

    没人回答时（`--ask-on-stdin` 没给）仍然是拒绝：这里绝不在没有人的地方干等。
    """
    source = reader if reader is not None else sys.stdin

    def approver(argv, reason: str) -> str:
        command = " ".join(argv)
        writer.emit(AWAIT, count=1, command=command, reason=reason)
        answer = ""
        try:
            answer = (source.readline() or "").strip().lower()
        except (OSError, ValueError):  # pragma: no cover - 流关了就当拒绝
            answer = ""
        once = ("y", "o", "yes", "ok")
        always = ("a", "s", "always")
        writer.emit(CONFIRM, applied=answer in once + always, count=1, command=command)
        if answer in once:
            return "once"
        if answer in always:
            return "always"
        if answer == "b":
            return "block"
        return "deny"

    return approver


def _events_escalation_reviewer(writer: EventWriter, reader=None):
    """events 模式下风险分级放行的批复通道（§3.2.3 的「网页壳 / 聊天通道」）。

    两个事件、一条回答，与 §3.2.3 的落点一一对应：

    - `AWAIT` + `escalation` 短字段：网页壳/桥**复用既有的等待通道**
      （`web/runner.py` 的 `awaiting` / `awaiting_detail`）与确认卡片；
      MCP 那侧复用 `status.awaiting > 0` 这个既有信号，不新增通道。
    - `NOTE`：五件摘要的**逐行**文本（人真的能读，`--events` 下它也只是一行
      JSON 的 text，不会污染 stdout 的协议）。
    - 回答从 stdin 读一行，四档批复词（批准 / 只批几处 / 撤销 / 改方案）。

    **没有 `--ask-on-stdin` 就不注入这一份**——那就是无人值守：不弹问题、
    把动作挂起为待批、成果照落盘（绝不静默放行，也不一律拒绝）。
    """
    source = reader if reader is not None else sys.stdin

    def review(request, text: str) -> tuple[str, str]:
        # `escalation` 的**字段**与 `count` 平级塞进 AWAIT：网页壳把它整份
        # 存进 `awaiting_detail`（`web/runner.py:_absorb`），页面与桥都按
        # 字段名取——套一层嵌套的话，两边都得先知道有这个壳。
        writer.emit(
            AWAIT,
            count=1,
            escalation=request.trigger,
            action=request.action,
            impact=request.summary.get("impact", ""),
            conclusion=request.summary.get("conclusion", ""),
            advice=request.summary.get("advice", ""),
            options=request.summary.get("options", ""),
            rollback=request.summary.get("rollback", ""),
            fingerprint=request.fingerprint,
        )
        writer.emit(NOTE, text=text, ok=False, kind="escalation")
        answer = ""
        try:
            answer = (source.readline() or "").strip()
        except (OSError, ValueError):  # pragma: no cover - 流关了就当没人回答
            answer = ""
        writer.emit(CONFIRM, applied=bool(answer), count=1)
        return answer, "web"

    return review


def _terminal_escalation_reviewer(out=print, ask=input, tty=None):
    """终端上的批复通道（§3.2.3 的「终端」那一栏）。

    五件各一行 + 一条撤销命令，默认答案是**「不执行这个动作」**（沿用现有
    `[y/N]` 的默认 N）。注意默认不再是「不落盘」——成果已经落在工作区里了；
    这里批的只是那个动作的执行权。

    `tty` 是「有人在不在」的判据。行首给的不是 TTY（管道、无人值守的调度器）
    时**不弹问题**：返回空词，由主循环按「挂起待批」处理。不加这道判断，
    一个没人回答的 `input()` 会把运行挂在原地——那是最坏的一种无人值守。
    """
    import sys as _sys

    if tty is None:
        try:
            tty = bool(_sys.stdin and _sys.stdin.isatty())
        except (ValueError, OSError):  # pragma: no cover - 流坏了就当没人
            tty = False

    def review(request, text: str) -> tuple[str, str]:
        if not tty:
            return "", ""
        out(text)
        try:
            answer = ask("执行这个动作吗？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        return answer, "terminal"

    return review


def _events_settle_header(result, undo: str) -> str:
    """`--events` 通道里打在 diff 之前的那一段（未通过时才非空）。

    与终端那条路（`_settle_pending` → `settle(header=…)`）**只差一个字段**：
    **撤销命令不在这里给**。理由（问题②的最后一处残余）：FINAL 事件带着收尾
    报告，报告里已经有那一行，而且它排在「等你确认 / 已应用」之后——这一份
    header 在 diff **之前**，多带一遍就等于同一条撤销信息在事件流里出现两次，
    而且顺序也读反了（用户先看到退路、后看到结果）。终端那条路照旧带着它：
    那里 diff 是随后 `settle` 打的，用户需要在这一大片 diff 之前就看见退路。
    """
    return verification_header(
        getattr(result, "verified", None),
        getattr(result, "verify_summary", ""),
        undo,
        with_undo=False,
    )


def _emit_deferred_verify(writer: EventWriter, deferred: list[dict]) -> None:
    """把循环攒下的「最终验证」那一条阶段行**在结算之后**发出去。

    为什么非得推迟（现场见 `tests/fixtures/interact_probe/web-real-reports.md`）：
    它验的是待确认清单里那批内容，而这条通道上用户要先点「应用」这一轮才算落地。
    当场发的话，用户读到的次序是「最终验证 → … / 等你确认：1 处改动 / 已应用 1 处
    改动」——一个阶段跑在自己前面。

    为什么走 `handle` 而不是 `emit(TOOL, **payload)`：第三档那一条 payload 里带一个
    `kind` 字段（`verifier_no_checker`），它与 `emit(self, kind, ...)` 的位置参数
    重名，`emit` 会抛 `TypeError: got multiple values for argument 'kind'`——整轮
    运行在收尾那一刻以「进程退出，退出码 1」收场（probe 的 create-hello 实测踩过）。
    `EventWriter.handle(kind, data)` 就是为这种 payload 准备的。

    发完清空：它是一次性的推迟出口，留着会让下一次调用重复发一遍。
    """
    for payload in deferred:
        writer.handle(TOOL, payload)
    deferred.clear()


def _events_mode(args, project_root, gateway, window, pending) -> int:
    """`--events` 模式：stdout 上只有 JSON 行。

    与散文模式共用同一套装配与同一个主循环，只换了输入输出通道。
    两条路径的判定必须一致——同一个策略在终端和网页里表现不同，
    那种差异不会报错，只会让人困惑。
    """
    writer = EventWriter()
    before = len(diagnosis.load_requests(project_root))
    policy = resolve_policy(args, project_root)
    scope = resolve_scope(args)
    overrides = _overrides(args, project_root)
    # 「最终验证」那一条阶段行的**推迟出口**：它验的是"待确认清单里那批内容"，
    # 而这条通道上用户要先点「应用」这一轮才算落地。所以循环把它攒在这里，
    # 由下面 `settle_with_events` 之后再发（阶段顺序是验收的一部分，现场见
    # tests/fixtures/interact_probe/web-real-reports.md）。
    deferred_verify: list[dict] = []
    writer.emit(
        START, session=args.session, goal=args.goal, policy=policy, window=window
    )

    memory = (
        None
        if args.no_memory
        else open_memory(
            project_root,
            window,
            args.session,
            model=f"{args.provider}:{args.model or '默认'}",
            overrides=overrides,
        )
    )
    # 聊天区域要有历史可看：网页壳按 /state 回放它。原先这条路径只开了记忆、
    # **不记往来**，于是页面刷新之后一片空白——"我上次说到哪儿"全靠回忆。
    # 注意完整 transcript 只进给人看的区域；模型拿的是截断后的最近几轮。
    if memory is not None:
        record_message(memory._conn, args.session, USER, args.goal)
    prompt = prompt_for_turn(memory, args.session, args.goal, overrides)
    loop = assemble_loop(
        project_root,
        gateway,
        config=Config(
            project_root=project_root,
            context_window=window,
            max_steps=args.max_steps,
            subagent_steps=args.subagent_steps,
            supervise=not args.no_supervise,
            step_ceiling=args.step_ceiling,
            # 能力标定：工作区的覆盖 + 命令行显式给的那两个
            overrides=overrides,
            session=args.session,
            # `--review`：显式要求跑一次语义审查（L3 触发条件第 5 条）。
            semantic_review=bool(getattr(args, "review", False)),
        ),
        wiring=LoopWiring(
            memory=memory,
            distiller=distiller_for(args, gateway, memory),
            pending=pending,
            # 命令权限申请也要能问出去：桥/网页壳给了 --ask-on-stdin 时，把问题
            # 作为事件发出去、从 stdin 读回答（用户那边就是"回 y 允许这一次"）。
            # 不给就保持原样——无人值守一律拒绝，绝不在没有人的地方等。
            approver=_events_approver(writer) if getattr(args, "ask_on_stdin", False) else None,
            # 风险分级放行的批复通道与命令授权共用同一个开关：都要能在
            # stdin 上问一句、把回答读回来。不给开关就是无人值守（§3.2.3）。
            escalation_review=(
                _events_escalation_reviewer(writer)
                if getattr(args, "ask_on_stdin", False)
                else None
            ),
            grants=Grants(project_root / ".agent" / "grants.json")
            if getattr(args, "ask_on_stdin", False)
            else None,
            lessons=build_lessons(memory) if memory is not None else None,
            on_event=writer.handle,
            read_roots=_resolve_read_roots(args, project_root)[0],
            web=web_access(args, project_root),
            search=web_search_backend(args, project_root),
            mcp=not getattr(args, "no_mcp", False),
            from_channel=getattr(args, "from_channel", False),
            defer_final_verify=deferred_verify,
        ),
    )
    result = loop.run(args.goal, resume=args.resume, prompt=prompt)
    _report_numbers(loop, result, writer)
    _maybe_file_diagnosis(project_root, before, result, writer)

    if memory is not None:
        settle_lessons(memory, result.lessons_pushed, result.finished)
        record_message(
            memory._conn,
            args.session,
            ASSISTANT,
            result.final or "（没有产出）",
            meta=f"{result.usage()}，{'完成' if result.finished else '未完成'}",
        )

    writer.emit(
        USAGE,
        steps=result.steps,
        calls=result.model_calls,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    # 落盘**之前**取一次撤销命令：`settle` 一跑，待确认清单就空了，
    # 之后再问就只会拿到「没有可退的」。网页壳那个可点的撤销动作要的就是它。
    undo = undo_command(pending)
    outcome = settle_with_events(
        pending,
        policy,
        scope,
        project_root / ".agent" / "last_change.json",
        writer,
        # 验证未通过时，判词必须**排在 diff 事件之前**发出去：网页壳按事件顺序
        # 渲染，diff 一大，埋在后面的判词就没人读到。
        header=_events_settle_header(result, undo),
    )
    # 「最终验证」这一条阶段行**现在才发**（它验的正是刚刚被用户应用的那批内容）。
    _emit_deferred_verify(writer, deferred_verify)
    # 挂起待批的动作清单（§3.2.3）：它在**落盘之后**才说得准（哪些动作真的
    # 被拦下、哪些成果真的落了盘），所以走在这条通道的最后、FINAL 之前。
    _settle_pending(args, project_root, pending, result, writer=writer)
    # 「撤销这一轮」这个动作的**结构化读数**（问题④）：网页壳据此摆一个真能点的
    # 按钮，桥据此认「回 y 撤销」——两条通道都不必去正文里正则捞命令。
    # 它在 FINAL 上（最后一条），所以那条动作提示落在「等你确认 / 已应用」之后。
    writer.emit(
        FINAL,
        ok=result.finished,
        text=result.final or "",
        settled=outcome,
        revert_command=undo,
        revert_run_id=pending.run_id if undo else "",
    )
    return 0 if result.finished else 1


def _standard(args, project_root, gateway, window, pending) -> int:
    """普通路径：主循环自己完成。"""
    overrides = _overrides(args, project_root)
    approver, grants = build_approver(project_root)
    before = len(diagnosis.load_requests(project_root))
    read_roots, _ = _resolve_read_roots(args, project_root)
    memory = None
    distiller = None
    if not args.no_memory:
        memory = open_memory(
            project_root,
            window,
            args.session,
            model=f"{args.provider}:{args.model or '默认'}",
            overrides=overrides,
        )
        distiller = distiller_for(args, gateway, memory)

    loop = assemble_loop(
        project_root,
        gateway,
        config=Config(
            project_root=project_root,
            context_window=window,
            max_steps=args.max_steps,
            subagent_steps=args.subagent_steps,
            supervise=not args.no_supervise,
            step_ceiling=args.step_ceiling,
            overrides=overrides,
            session=args.session,
            # `--review`：显式要求跑一次语义审查（L3 触发条件第 5 条）。
            semantic_review=bool(getattr(args, "review", False)),
        ),
        wiring=LoopWiring(
            memory=memory,
            distiller=distiller,
            pending=pending,
            approver=approver,
            grants=grants,
            # 终端上的批复通道：有 TTY 才弹问题，没有就按挂起待批处理
            # （§3.2.3 的超时与无人值守行为）。
            escalation_review=_terminal_escalation_reviewer(),
            lessons=build_lessons(memory) if memory is not None else None,
            read_roots=read_roots,
            web=web_access(args, project_root),
            search=web_search_backend(args, project_root),
            mcp=not getattr(args, "no_mcp", False),
            from_channel=getattr(args, "from_channel", False),
        ),
    )
    checkpoint = loop.config.task_path("task")
    if memory is not None:
        if not args.no_history:
            show_history(memory, args.session, args.history)
        record_message(memory._conn, args.session, USER, args.goal)
    if checkpoint.exists() and not args.resume:
        print(
            f"发现未完成的检查点（{checkpoint}）。"
            "加 --resume 可以接着做，不加则从零开始。"
        )

    result = loop.run(args.goal, resume=args.resume)
    _report_numbers(loop, result)
    _maybe_file_diagnosis(project_root, before, result)
    if memory is not None:
        settle_lessons(memory, result.lessons_pushed, result.finished)
        record_message(
            memory._conn,
            args.session,
            ASSISTANT,
            result.final or "（没有产出）",
            meta=f"{result.usage()}，{'完成' if result.finished else '未完成'}",
        )

    for line in result.trace:
        print(line)
    print("---")
    print(result.usage())
    print("---")
    print(result.final)

    _settle_pending(args, project_root, pending, result)
    return 0 if result.finished else 1


def _delegated(args, project_root, gateway, window, pending) -> int:
    """派发路径：先判断值不值得派发，值得就交实现者做、审查者独立验。"""
    plan = plan_dispatch(gateway, args.goal)
    print(f"分派判断：{'派发' if plan.delegate else '自己完成'} —— {plan.reason}")
    if not plan.delegate:
        print("未派发，请去掉 --delegate 让主循环自己完成。")
        return 0

    read_roots, _ = _resolve_read_roots(args, project_root)
    registry = _plain_registry(project_root, pending, read_roots)
    # 子智能体和主循环共用同一套自动验证：审查者本来就有 run_command，
    # 但实测模型几乎从不主动跑测试——指望它凭自觉去验是不现实的。
    from spoolkit.tools.verify import make_verifier

    # 审查者要审的是「改动差异」，不是实现者的总结文字。不给 diff 的话，
    # 它只能靠反复读文件自己还原改了什么——实测它为此翻到步数上限。
    artifacts = "\n\n".join(
        f"--- {change.path} ---\n{change.diff}" for change in pending.items()
    )
    delegated = run_delegated(
        plan,
        gateway,
        OfflineTokenCounter(),
        registry,
        Config(
            project_root=project_root,
            context_window=window,
            max_steps=args.max_steps,
            subagent_steps=args.subagent_steps,
            session=args.session,
        ),
        artifacts=artifacts,
        verify=make_verifier(project_root, pending),
    )
    for line in delegated.trace:
        print(line)
    print("--- 实现 ---")
    print(delegated.implementer_final)
    print("--- 独立审查 ---")
    print(delegated.reviewer_final)
    if delegated.rejected:
        print("--- 审查未通过 ---")
        print("改动还在待确认清单里，没有自动落盘。理由：")
        for reason in delegated.review.reasons:
            print(f"  - {reason}")
    elif delegated.review is not None and delegated.review.inconclusive:
        print("--- 审查没有结论 ---")
        print("审查者自己没跑完，所以这次改动既没被否定也没被确认：")
        for reason in delegated.review.reasons:
            print(f"  - {reason}")
    elif delegated.too_big:
        # 没送审，就没有「审查结论」。不单列这一支的话，上面那两行都不成立，
        # 用户会以为这次派发是正常结束的。
        print("--- 这块活太大，子智能体没做完 ---")
        print(delegated.too_big)
        print("拆小之后再派，或者加 --subagent-steps 再来一次。")
    _settle_pending(args, project_root, pending, delegated)
    return 0


def _plain_registry(project_root, pending, read_roots=()):
    """派发路径用的注册表：不带记忆与教训，子智能体只拿任务说明。"""
    from spoolkit.cli.runtime import attach_index
    from spoolkit.tools.edit import register_edit_tools
    from spoolkit.tools.diagnosis import (
        read_diagnosis_spec,
        request_diagnosis_spec,
    )
    from spoolkit.tools.exec import run_command_spec
    from spoolkit.tools.fs import list_dir_spec, read_file_spec
    from spoolkit.tools.registry import ToolRegistry
    from spoolkit.tools.search import search_code_spec
    from spoolkit.tools.stats import dir_stats_spec
    from spoolkit.tools.calc import calc_spec

    registry = ToolRegistry()
    # 与主循环一致：读工具要能看到待确认的改动，否则子智能体读到的
    # 是改之前的文件，而它跑测试看到的是改之后的——两套矛盾的世界。
    registry.register(read_file_spec(project_root, pending, read_roots))
    registry.register(list_dir_spec(project_root, pending, read_roots))
    registry.register(search_code_spec(project_root, pending, read_roots))
    registry.register(dir_stats_spec(project_root, read_roots))
    registry.register(calc_spec())
    registry.register(request_diagnosis_spec(project_root))
    registry.register(read_diagnosis_spec(project_root))
    # 必须把 pending 传进去：否则子智能体改完代码再跑测试，测到的是**旧代码**
    # （改动还没落盘），它会以为自己的修复没生效，然后去改一个已经改对的函数。
    # trial_workspace 这个模块存在的全部理由就是这个，别在这一条路径上漏掉。
    registry.register(run_command_spec(project_root, pending))
    # 与主循环共用同一份按产出类型分派的验证入口，否则会出现「同一个项目里
    # 主循环验得了 HTML、子智能体验不了」这种分叉。
    from spoolkit.tools.verify import verify_file_spec

    registry.register(verify_file_spec(project_root, pending, read_roots))
    # 与主循环共用同一套判定，否则会出现「同一个项目里子智能体有一把
    # 主循环没有的工具」这种分叉，而它只会在跑偏时才暴露。
    register_edit_tools(registry, project_root, pending)
    attach_index(project_root, registry, OfflineTokenCounter(), pending)
    return registry


def _resolve_read_roots(args, project_root: Path) -> tuple[tuple[Path, ...], list[str]]:
    """把配置里的关联目录和 `--allow-read` 合成可读根。

    配置来自本机 `spool extra add`（`.agent/read-roots.json`）；命令行是一次性
    叠加。模型自己不能加目录。工作区内的路径本来就读得到，重复授权只会
    让提示词变长。
    """
    from spoolkit import readroots

    roots: list[Path] = []
    bad: list[str] = []

    def _take(raw) -> None:
        target = Path(raw).expanduser()
        if not target.exists() or not target.is_dir():
            bad.append(str(raw))
            return
        resolved = target.resolve()
        if resolved == project_root or project_root in resolved.parents:
            return
        if resolved not in roots:
            roots.append(resolved)

    for item in readroots.existing(project_root):
        resolved = item.resolve()
        if resolved == project_root or project_root in resolved.parents:
            continue
        if resolved not in roots:
            roots.append(resolved)
    for raw in getattr(args, "allow_read", []) or []:
        _take(raw)
    return tuple(roots), bad


def _maybe_file_diagnosis(
    project_root, before: int, result, writer: EventWriter | None = None
) -> str:
    """任务做不下去、而且确认过是环境问题时，替它把诊断请求登记下来。

    触发条件刻意收得很紧，三条同时成立才登记：

    1. **任务没做成**——自己想办法绕过去了就不必查环境；
    2. **这一步确认过是环境问题**（验证器给出的判定，不是猜的）；
    3. **它自己没登记过**——它主动申请过就不重复。

    为什么要循环替它做：实测模型在自己被卡住时会去改配置绕过，
    根本想不起来用这个通道。文字提醒在这台模型上反复被验证为无效，
    能推得动的只有「循环替它做」。
    """
    if result.finished or not getattr(result, "environment_blocked", False):
        return ""
    if len(diagnosis.load_requests(project_root)) > before:
        return ""

    recent = [line for line in result.trace if "自动验证" in line][-3:]
    request = diagnosis.store_request(
        project_root,
        question="任务因环境问题无法继续，请确认是不是环境本身坏了",
        hypothesis="本机的测试环境存在异常，与本次改动无关，需要真实环境权限才能查证",
        evidence="\n".join(recent) or "（没有留下更多线索）",
    )
    line = (
        f"已替你登记诊断请求 {request.id}：任务因环境问题无法继续。"
        "它需要由具备真实环境权限的会话验证，你可以继续或等报告。"
    )
    if writer is not None:
        writer.emit("tool", name="诊断登记", ok=True, detail=line)
    else:
        print(line)
    return request.id


def _report_numbers(loop, result, writer: EventWriter | None = None) -> None:
    """交付物里的数字有没有出处。**只标注，不阻断**。

    把可疑当成错误会误伤——很多数字是合理的推导；而把编造当成正常才是真的
    危险。所以它只负责把「在工具输出里找不到出处」的那几处挑出来。
    """
    log = getattr(loop, "sources", None)
    if log is None or not result.final:
        return
    claims = untraceable(result.final, log.items(), log.facts())
    wrong = mismatched(result.final, log.facts())
    if not claims and not wrong:
        return
    if writer is not None:
        writer.emit(
            NOTE, text=summarize(claims, wrong), ok=not actionable(claims, wrong)
        )
        return
    print("---")
    print(render_numbers(claims, wrong))


def _settle_pending(args, project_root, pending, result=None, writer=None) -> None:
    # 挂起待批的动作清单：**成果已经落盘**，挂起的只是那些没执行的动作
    # （§3.2.3）。所以它必须在 `settle` 的 early-return **之前**说一次——
    # 一条「有动作在等批复」的话被漏掉，用户就以为这一轮是普通收工。
    actions = pending_actions_note(project_root)
    if actions:
        if writer is not None:
            writer.emit(NOTE, text=actions, ok=False, kind="pending_actions")
        else:
            print("---")
            print(actions)
    # 只有整份改动都落完、暂存也空了才真的没话说：暂存里那几份正文同样需要
    # 一句交代（`settle` 会替它说话），否则进程一退出就是静默丢弃。
    if not len(pending) and not pending.staged_count():
        return
    if writer is None:
        print("---")
    settle(
        pending,
        resolve_policy(args, project_root),
        resolve_scope(args),
        baseline_path=project_root / ".agent" / "last_change.json",
        # 验证未通过时这一段打在 diff **之前**：判词不能埋在一大片 diff 底下
        # （用户接受了一份没通过验证的改动，就是这么发生的）。
        header=verification_header(
            getattr(result, "verified", None),
            getattr(result, "verify_summary", ""),
            undo_command(pending),
        ),
    )
