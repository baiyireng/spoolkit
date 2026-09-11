"""最小命令行入口。

当前阶段用脚本化假模型驱动，因此整个闭环在无 GPU 环境下即可运行。
接入 llama.cpp 后，只需把 build_loop 中的 FakeModel 换成真实网关。
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from agents_dev.agent.loop import AgentLoop
from agents_dev.agents.dispatcher import plan_dispatch, run_delegated
from agents_dev.agents.plan import (
    DONE,
    FAILED,
    decompose,
    load_plan,
    narrow_scope,
    out_of_scope,
    plan_path,
    render_step_prompt,
    save_plan,
)
from agents_dev.config import Config
from agents_dev.index.indexer import index_project
from agents_dev.index.rank import prefetch as prefetch_text
from agents_dev.index.tools import file_symbols_spec, find_callers_spec, find_symbol_spec
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.providers import (
    ProviderConfig,
    ProviderError,
    describe_providers,
    load_gateway,
    provider_names,
)
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.memory.distill import distill
from agents_dev.memory.session import MemorySession
from agents_dev.memory.lessons import match_lessons, prune_lessons, record_outcome
from agents_dev.memory.store import init_memory_schema
from agents_dev.memory.store import check_binding, record_session
from agents_dev.memory.transcript import (
    ASSISTANT,
    USER,
    recent_messages,
    record_message,
    render_transcript,
)
from agents_dev.memory.tools import recall_spec
from agents_dev.net import system_proxy
from agents_dev.store.db import init_schema, open_db
from agents_dev.tools.edit import PendingChanges, replace_lines_spec, write_file_spec
from agents_dev.tools.exec import run_command_spec
from agents_dev.tools.grant import Grants
from agents_dev.tools.edit import load_baseline, revert
from agents_dev.tools.fs import list_dir_spec, read_file_spec
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.search import search_code_spec
from agents_dev.cli.approval import apply_with_audit, review_and_apply
from agents_dev.cli.settle import settle
from agents_dev.policy import (
    DEFAULT_POLICY,
    POLICIES,
    describe as describe_policy,
    load_policy,
    policy_path,
    save_policy,
)

PREFETCH_BUDGET = 400

# 云端模型的窗口动辄上百万 token。直接采用会让整套「短上下文特化」的设计
# 失去意义——配额永远用不完，截断逻辑永远不触发，也就永远测不出问题。
# 所以给自动探测加一个上限，保留设计前提，同时仍远大于原来写死的 8192。
MAX_AUTO_WINDOW = 32768
DEFAULT_WINDOW = 8192


def resolve_window(gateway: ModelGateway, requested: int) -> int:
    """决定本次运行使用多大的上下文窗口。

    显式指定优先；否则问供应商；问不到才退回默认值。
    这个值不该由使用者猜：同一个配置文件下换模型或换 KV cache 量化，
    窗口都会变，只有服务端知道真实值。
    """
    if requested > 0:
        return requested
    detected = gateway.context_window()
    if not detected:
        return DEFAULT_WINDOW
    return min(detected, MAX_AUTO_WINDOW)


def resolve_scope(
    args: argparse.Namespace,
    step_scope: Sequence[str] = (),
    default: Sequence[str] = (),
) -> tuple[str, ...]:
    """决定本次运行允许自动落盘的范围。

    优先级：命令行的 --scope > 计划步骤声明的 scope > default。

    默认回退成空而不是全项目。「没声明范围」应该意味着「不许自动落盘」，
    会走逐项确认；把「没声明」当成「全都允许」是危险的默认值。

    更实际的原因是：策略是持久化的。一旦把 auto 存下来，之后每一次
    非计划运行都会变成**整个项目免确认**——那不是用户的持续选择，
    只是他某一次的选择被默默放大了。要走自动就显式给 --scope。
    """
    if args.scope:
        return tuple(part.strip() for part in args.scope.split(",") if part.strip())
    if step_scope:
        return tuple(step_scope)
    return tuple(default)


def resolve_policy(args: argparse.Namespace, project_root: Path) -> str:
    """命令行指定的策略优先，否则用已保存的。"""
    if args.policy:
        return args.policy
    return load_policy(policy_path(project_root))


def report_policy(policy: str, scope: Sequence[str]) -> None:
    print(f"授权策略：{policy} —— {describe_policy(policy)}")
    if policy == "auto":
        print(
            f"自动落盘范围：{'、'.join(scope)}"
            if scope
            else "自动落盘范围：（未指定 --scope，改动仍会逐项确认）"
        )


def _attach_index(project_root: Path, registry: ToolRegistry, tokenizer):
    """建立（或复用）代码索引，注册索引工具并返回预取函数。

    索引是可选增强：建索引失败不应让整个 agent 起不来，
    所以这里只做最保守的处理，失败时退化为无索引模式。
    """
    try:
        conn = open_db(project_root / ".agent" / "index.db")
        init_schema(conn)
        index_project(project_root, conn)
    except Exception:
        return None

    registry.register(find_symbol_spec(project_root, conn))
    registry.register(file_symbols_spec(conn))
    registry.register(find_callers_spec(conn))
    return lambda goal: prefetch_text(conn, goal, tokenizer, PREFETCH_BUDGET)


def assemble_loop(
    project_root: Path,
    gateway: ModelGateway,
    window: int = 4096,
    max_steps: int = 10,
    subagent_steps: int = 20,
    memory=None,
    distiller=None,
    pending=None,
    approver=None,
    grants=None,
    lessons=None,
) -> AgentLoop:
    """用给定网关装配完整循环：注册全部工具、建索引、接上预取。"""
    registry = ToolRegistry()
    registry.register(read_file_spec(project_root))
    registry.register(list_dir_spec(project_root))
    registry.register(search_code_spec(project_root))
    registry.register(run_command_spec(project_root, pending, approver, grants))
    if pending is not None:
        registry.register(write_file_spec(project_root, pending))
        registry.register(replace_lines_spec(project_root, pending))
    if memory is not None:
        # 主循环用的注册表在这里构造，所以 recall 也必须在这里注册，
        # 否则提示词会提到一个只有派发路径才有的工具。
        registry.register(recall_spec(memory))

    tokenizer = OfflineTokenCounter()
    config = Config(
        project_root=project_root,
        context_window=window,
        max_steps=max_steps,
        subagent_steps=subagent_steps,
    )
    return AgentLoop(
        gateway=gateway,
        tokenizer=tokenizer,
        registry=registry,
        config=config,
        prefetch=_attach_index(project_root, registry, tokenizer),
        memory=memory,
        lessons=lessons,
        distiller=distiller,
    )


def build_memory(project_root: Path, window: int, session_id: str = "cli"):
    """在 .agent 下建立记忆库与热记忆文件。"""
    state_dir = project_root / ".agent"
    conn = open_db(state_dir / "memory.db")
    init_memory_schema(conn)
    return MemorySession(
        conn=conn,
        hot_path=state_dir / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=window,
        session_id=session_id,
    )


def open_memory(
    project_root: Path, window: int, session_id: str, model: str = ""
) -> MemorySession:
    """建立记忆会话，并在登记前检查工作区绑定。

    检查必须在登记之前：登记会写入当前工作区，先登记就把
    「上次绑定在哪」这个信息当场覆盖掉了，检查也就永远查不出问题。
    """
    memory = build_memory(project_root, window, session_id)
    warning = check_binding(memory._conn, session_id, str(project_root))
    if warning:
        print(f"提示：{warning}")
    record_session(memory._conn, session_id, str(project_root), model, window)
    return memory


def show_history(memory: MemorySession, session_id: str, limit: int) -> None:
    """把最近几轮显示到聊天区域。

    这是**给人看**的，不进入模型的上下文。两者混为一谈会得出错误结论
    （「那就把历史塞回上下文」），而短上下文正是靠不塞历史才成立的。
    """
    rows = recent_messages(memory._conn, session_id, limit)
    print("── 会话历史 ──")
    print(render_transcript(rows))
    print("──────────────")


def build_lessons(memory: MemorySession):
    """构造教训推送器。

    在任务开始时推一次：模型不知道自己缺什么，所以不能等它来查；
    但也不该每轮重推——教训是场景级的，不是步骤级的。
    """

    def provider(goal: str) -> list[tuple[int, str]]:
        return [(item.id, item.text) for item in match_lessons(memory._conn, goal)]

    return provider


def settle_lessons(memory: MemorySession, pushed, succeeded: bool) -> None:
    """按任务结果更新被推送教训的置信度，并清理长期无效的。"""
    if not pushed:
        return
    record_outcome(memory._conn, list(pushed), succeeded=succeeded)
    pruned = prune_lessons(memory._conn)
    if pruned:
        print(f"已把 {len(pruned)} 条长期无效的教训移出推送池（仍保留可检索）。")


def build_approver(project_root: Path):
    """构造命令授权询问器。返回 (approver, grants)。

    只在交互路径上用。无人值守时传入的 approver 为 None，
    工具会拒绝并向模型说明「需要用户手动执行」。
    """
    grants = Grants(path=project_root / ".agent" / "grants.json")

    def approver(argv, reason: str) -> str:
        print(f"\n模型请求执行一条白名单外的命令：\n  {' '.join(argv)}")
        print(f"原因：{reason}")
        answer = input("[s]本轮允许 / [a]永久允许 / [n]拒绝 → ").strip().lower()
        if answer in ("a", "always"):
            return "always"
        if answer in ("s", "y", "session"):
            return "session"
        return "deny"

    return approver, grants


def build_loop(project_root: Path, script: list[str], window: int = 4096) -> AgentLoop:
    """装配一个由脚本化假模型驱动的循环（离线可用）。"""
    tokenizer = OfflineTokenCounter()
    return assemble_loop(
        project_root,
        FakeModel(script=script, tokenizer=tokenizer),
        window=window,
    )


def _run(args: argparse.Namespace) -> int:
    project_root = Path(args.root).resolve()

    # 走计划时目标来自计划文件，命令行不该再强制要求填一次。
    if not args.plan and not args.goal.strip():
        print("需要 --goal，或者用 --plan 推进已有计划。", file=sys.stderr)
        return 2

    script: tuple[str, ...] = ()
    if args.provider == "fake":
        script_path = Path(args.script)
        if not script_path.exists():
            print(f"假模型需要 --script，文件不存在: {script_path}", file=sys.stderr)
            return 2
        raw = json.loads(script_path.read_text(encoding="utf-8"))
        script = tuple(
            item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            for item in raw
        )

    provider_config = ProviderConfig(
        project_root=project_root,
        model=args.model,
        base_url=args.base_url,
        proxy=args.proxy if args.proxy else system_proxy(),
        script=script,
        env=dict(os.environ),
    )
    try:
        gateway = load_gateway(args.provider, provider_config)
    except ProviderError as exc:
        print(f"无法装载供应商 {args.provider}: {exc}", file=sys.stderr)
        return 2

    window = resolve_window(gateway, args.window)
    print(f"上下文窗口：{window} token")

    if args.plan:
        return _advance_plan(args, project_root, gateway)
    if args.autonomous:
        return _run_autonomous(args, project_root, gateway)
    report_policy(resolve_policy(args, project_root), resolve_scope(args))

    memory = None
    distiller = None
    pending = PendingChanges(project_root)
    approver, grants = build_approver(project_root)
    registry = ToolRegistry()
    registry.register(read_file_spec(project_root))
    registry.register(list_dir_spec(project_root))
    registry.register(search_code_spec(project_root))
    registry.register(run_command_spec(project_root))
    registry.register(write_file_spec(project_root, pending))
    registry.register(replace_lines_spec(project_root, pending))
    _attach_index(project_root, registry, OfflineTokenCounter())

    if args.delegate:
        plan = plan_dispatch(gateway, args.goal)
        print(f"分派判断：{'派发' if plan.delegate else '自己完成'} —— {plan.reason}")
        if not plan.delegate:
            print("未派发，请去掉 --delegate 让主循环自己完成。")
            return 0
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
            ),
        )
        for line in delegated.trace:
            print(line)
        print("--- 实现 ---")
        print(delegated.implementer_final)
        print("--- 独立审查 ---")
        print(delegated.reviewer_final)
        if len(pending):
            print("---")
            settle(
                pending,
                resolve_policy(args, project_root),
                resolve_scope(args),
                baseline_path=project_root / ".agent" / "last_change.json",
            )
        return 0
    if not args.no_memory:
        memory = open_memory(
            project_root, window, args.session, model=f"{args.provider}:{args.model or '默认'}"
        )
        registry.register(recall_spec(memory))
        # 假模型没有多余脚本条目可分给归纳调用，因此只在真实供应商下启用。
        if args.provider != "fake":
            distiller = lambda state, final: distill(gateway, state, final=final)

    loop = assemble_loop(
        project_root,
        gateway,
        window=window,
        max_steps=args.max_steps,
        subagent_steps=args.subagent_steps,
        memory=memory,
        distiller=distiller,
        pending=pending,
        approver=approver,
        grants=grants,
        lessons=build_lessons(memory) if memory is not None else None,
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

    if len(pending):
        print("---")
        settle(
            pending,
            resolve_policy(args, project_root),
            resolve_scope(args),
            baseline_path=project_root / ".agent" / "last_change.json",
        )
    return 0 if result.finished else 1


def _revert(args: argparse.Namespace) -> int:
    """把上一次写入的文件恢复到改动前。"""
    project_root = Path(args.root).resolve()
    baseline_path = project_root / ".agent" / "last_change.json"
    baseline = load_baseline(baseline_path)
    if baseline is None:
        print("没有可回滚的记录。", file=sys.stderr)
        return 2

    touched = revert(project_root, baseline)
    baseline_path.unlink()
    for path in touched:
        print(f"已恢复: {path}")
    print(f"共恢复 {len(touched)} 个文件。")
    return 0


def _policy_command(args: argparse.Namespace) -> int:
    """查看或调整授权策略。调整会持久化，下一轮自动沿用。"""
    project_root = Path(args.root).resolve()
    path = policy_path(project_root)

    if args.new_policy:
        save_policy(path, args.new_policy)
        print(f"授权策略已设为 {args.new_policy}：{describe_policy(args.new_policy)}")
        print(f"已保存到 {path}，下一轮起生效。")
        return 0

    current = load_policy(path)
    print(f"当前授权策略：{current}")
    print(f"  {describe_policy(current)}")
    print("可选：")
    for name in POLICIES:
        print(f"  {name}: {describe_policy(name)}")
    return 0


def _make_plan(args: argparse.Namespace) -> int:
    """把一个较大目标拆成可验收的步骤序列并落盘。"""
    project_root = Path(args.root).resolve()
    provider_config = ProviderConfig(
        project_root=project_root,
        model=args.model,
        base_url=args.base_url,
        proxy=args.proxy if args.proxy else system_proxy(),
        env=dict(os.environ),
    )
    try:
        gateway = load_gateway(args.provider, provider_config)
    except ProviderError as exc:
        print(f"无法装载供应商 {args.provider}: {exc}", file=sys.stderr)
        return 2

    plan = decompose(gateway, args.goal, limit=args.limit)
    if not plan.steps:
        print("没有拆出任何带验收标准的步骤。", file=sys.stderr)
        return 1

    save_plan(plan_path(project_root), plan)
    print(plan.render())
    print(f"\n计划已保存到 {plan_path(project_root)}")
    return 0


def _execute_step(
    args: argparse.Namespace,
    project_root: Path,
    gateway,
    plan,
    step,
    scope: Sequence[str],
    non_interactive: bool,
    approver=None,
    grants=None,
):
    """执行一个计划步骤。两条路径共用，避免策略在其中一条上悄悄失效。"""
    report_policy(resolve_policy(args, project_root), scope)
    window = resolve_window(gateway, args.window)
    pending = PendingChanges(project_root)
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
    loop = assemble_loop(
        project_root,
        gateway,
        window=window,
        max_steps=args.max_steps,
        subagent_steps=args.subagent_steps,
        memory=memory,
        pending=pending,
        approver=approver,
        grants=grants,
        lessons=build_lessons(memory) if memory is not None else None,
    )
    result = loop.run(render_step_prompt(plan, step))

    if memory is not None:
        settle_lessons(memory, result.lessons_pushed, result.finished)

    for line in result.trace:
        print(line)
    print(result.usage())
    return result, pending


def _record_step(
    project_root: Path,
    plan,
    step,
    result,
    pending,
    scope: Sequence[str],
    policy: str,
    non_interactive: bool,
) -> None:
    """记录步骤结果并按策略处理待落盘改动。"""
    note = (result.final or "").strip().splitlines()[0][:80] if result.final else ""
    plan.mark(step.index, DONE if result.finished else FAILED, note=note)
    save_plan(plan_path(project_root), plan)

    if len(pending):
        settle(
            pending,
            policy,
            scope,
            baseline_path=project_root / ".agent" / "last_change.json",
            non_interactive=non_interactive,
        )


def _advance_plan(args: argparse.Namespace, project_root: Path, gateway) -> int:
    """执行计划中下一个待办步骤，并记录结果。"""
    plan = load_plan(plan_path(project_root))
    if plan is None:
        print("还没有计划，请先用 `plan --goal` 生成。", file=sys.stderr)
        return 2

    step = plan.next_pending()
    if step is None:
        print(plan.render())
        print("\n计划已全部完成。")
        return 0

    blocked = plan.blocked_by()
    if blocked is not None:
        print(plan.render())
        print(
            f"\n计划被第 {blocked.index} 步阻塞：{blocked.goal}\n"
            f"原因：{blocked.note or '未记录'}\n"
            "先处理它，或者用 `plan --goal` 重新生成计划。",
            file=sys.stderr,
        )
        return 1

    print(f"执行第 {step.index}/{len(plan.steps)} 步：{step.goal}")
    # 打印的是**这一步实际生效**的范围，不是运行级默认值：
    # 安全边界的输出报错比不输出更糟——看到「**」会以为整个项目都放行了。
    scope = resolve_scope(args, step.scope, default=())
    approver, grants = build_approver(project_root)
    result, pending = _execute_step(
        args,
        project_root,
        gateway,
        plan,
        step,
        scope,
        non_interactive=False,
        approver=approver,
        grants=grants,
    )
    _record_step(
        project_root,
        plan,
        step,
        result,
        pending,
        scope,
        resolve_policy(args, project_root),
        non_interactive=False,
    )
    print(plan.render())
    return 0 if result.finished else 1


def _run_autonomous(args: argparse.Namespace, project_root: Path, gateway) -> int:
    """自己拆解目标并逐步做完。

    唯一不能交给模型的是**授权范围**：如果它既能提范围又能批范围，
    就等于自己给自己发许可证。所以范围必须由 --scope 给出，
    模型只能在里面收窄，不能扩大。
    """
    granted = tuple(part.strip() for part in args.scope.split(",") if part.strip())
    if not granted:
        print(
            "自主模式必须用 --scope 指定授权范围。\n"
            "授权必须来自你——让模型自己定范围，等于它自己给自己发许可证。\n"
            "例如：--scope scratch_lab/",
            file=sys.stderr,
        )
        return 2

    plan = decompose(gateway, args.goal, limit=args.limit)
    if not plan.steps:
        print("没能拆出任何带验收标准的步骤。", file=sys.stderr)
        return 1
    save_plan(plan_path(project_root), plan)
    print(f"授权范围：{'、'.join(granted)}")
    print(plan.render())

    policy = resolve_policy(args, project_root)
    completed = 0
    for step in plan.steps:
        blocked = plan.blocked_by()
        if blocked is not None:
            print(
                f"\n计划被第 {blocked.index} 步阻塞：{blocked.goal}"
                f"（{blocked.note or '未记录'}）",
                file=sys.stderr,
            )
            break

        effective, rejected = narrow_scope(granted, step.scope or granted)
        if rejected:
            print(
                f"第 {step.index} 步提出的范围超出授权，已收窄："
                + "、".join(rejected)
            )
        # 提议被全部驳回时回退到你授予的范围，而不是回退成「什么都不许」。
        # 授权来自你；步骤提议只是模型想进一步收窄的意愿，它不该有
        # 把整步变成只读的能力。
        if not effective:
            effective = granted

        print(f"\n执行第 {step.index}/{len(plan.steps)} 步：{step.goal}")
        result, pending = _execute_step(
            args,
            project_root,
            gateway,
            plan,
            step,
            effective,
            non_interactive=True,
            approver=None,
            grants=Grants(path=project_root / ".agent" / "grants.json"),
        )
        _record_step(
            project_root,
            plan,
            step,
            result,
            pending,
            effective,
            policy,
            non_interactive=True,
        )
        if not result.finished:
            print(plan.render())
            return 1
        completed += 1

    print(plan.render())
    print(f"\n自主运行结束：完成 {completed}/{len(plan.steps)} 步。")
    return 0 if completed == len(plan.steps) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents-dev")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="运行一次任务")
    run_parser.add_argument("--goal", default="")
    run_parser.add_argument(
        "--provider",
        "--engine",
        dest="provider",
        choices=provider_names(),
        default="fake",
        help="模型供应商；" + describe_providers(),
    )
    run_parser.add_argument("--script", default="", help="假模型脚本 JSON")
    run_parser.add_argument("--model", default="", help="留空则用供应商默认模型")
    run_parser.add_argument("--base-url", default="", help="llama.cpp 服务地址")
    run_parser.add_argument("--proxy", default="", help="留空则使用系统代理")
    run_parser.add_argument("--root", default=".")
    run_parser.add_argument(
        "--window", type=int, default=0, help="0 表示自动向供应商查询"
    )
    run_parser.add_argument("--max-steps", type=int, default=10)
    run_parser.add_argument(
        "--subagent-steps",
        type=int,
        default=20,
        help="子智能体的步数上限，默认比主循环大（一次性容器，重派成本低）",
    )
    run_parser.add_argument(
        "--no-memory", action="store_true", help="关闭记忆读写，用于对照实验"
    )
    run_parser.add_argument(
        "--delegate",
        action="store_true",
        help="先判断是否派发，派发时由实现者做、审查者独立验",
    )
    run_parser.add_argument(
        "--plan", action="store_true", help="执行计划中的下一个待办步骤"
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="接着上次未完成的检查点继续，而不是从零开始",
    )
    run_parser.add_argument(
        "--session",
        default="cli",
        help="会话名。不同时段/不同目的的活可以用不同会话，记忆分开记",
    )
    run_parser.add_argument(
        "--history", type=int, default=6, help="启动时显示最近几轮会话记录"
    )
    run_parser.add_argument(
        "--no-history", action="store_true", help="不显示会话记录"
    )
    run_parser.add_argument(
        "--autonomous",
        action="store_true",
        help="自己拆解目标并逐步做完；必须用 --scope 指定授权范围",
    )
    run_parser.add_argument(
        "--policy",
        choices=POLICIES,
        default="",
        help="本次运行的授权策略；留空则用已保存的设置",
    )
    run_parser.add_argument(
        "--scope",
        default="",
        help="auto 策略下允许自动落盘的路径，逗号分隔；留空则用计划声明的范围",
    )
    run_parser.add_argument("--limit", type=int, default=10, help="自主模式下最多拆几步")
    run_parser.set_defaults(func=_run)

    revert_parser = sub.add_parser("revert", help="回滚上一次写入的改动")
    revert_parser.add_argument("--root", default=".")
    revert_parser.set_defaults(func=_revert)

    plan_parser = sub.add_parser("plan", help="把目标拆成可验收的步骤序列")
    plan_parser.add_argument("--goal", required=True)
    plan_parser.add_argument("--provider", dest="provider", choices=provider_names(), default="gemini")
    plan_parser.add_argument("--model", default="")
    plan_parser.add_argument("--base-url", default="")
    plan_parser.add_argument("--proxy", default="")
    plan_parser.add_argument("--root", default=".")
    plan_parser.add_argument("--limit", type=int, default=10)
    plan_parser.set_defaults(func=_make_plan)

    policy_parser = sub.add_parser("policy", help="查看或调整授权策略")
    policy_parser.add_argument("--set", dest="new_policy", choices=POLICIES, default="")
    policy_parser.add_argument("--root", default=".")
    policy_parser.set_defaults(func=_policy_command)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

