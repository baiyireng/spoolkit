"""最小命令行入口。

当前阶段用脚本化假模型驱动，因此整个闭环在无 GPU 环境下即可运行。
接入 llama.cpp 后，只需把 build_loop 中的 FakeModel 换成真实网关。
"""

import argparse
import json
import os
import sys
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.agents.dispatcher import plan_dispatch, run_delegated
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
from agents_dev.memory.store import init_memory_schema
from agents_dev.memory.tools import recall_spec
from agents_dev.net import system_proxy
from agents_dev.store.db import init_schema, open_db
from agents_dev.tools.edit import PendingChanges, replace_lines_spec, write_file_spec
from agents_dev.tools.exec import run_command_spec
from agents_dev.tools.edit import load_baseline, revert
from agents_dev.tools.fs import list_dir_spec, read_file_spec
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.search import search_code_spec
from agents_dev.cli.approval import review_and_apply

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
) -> AgentLoop:
    """用给定网关装配完整循环：注册全部工具、建索引、接上预取。"""
    registry = ToolRegistry()
    registry.register(read_file_spec(project_root))
    registry.register(list_dir_spec(project_root))
    registry.register(search_code_spec(project_root))
    registry.register(run_command_spec(project_root, pending))
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

    memory = None
    distiller = None
    pending = PendingChanges(project_root)
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
            review_and_apply(
                pending, baseline_path=project_root / ".agent" / "last_change.json"
            )
        return 0
    if not args.no_memory:
        memory = build_memory(project_root, window)
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
    )
    result = loop.run(args.goal)

    for line in result.trace:
        print(line)
    print("---")
    print(result.usage())
    print("---")
    print(result.final)

    if len(pending):
        print("---")
        review_and_apply(pending, baseline_path=project_root / ".agent" / "last_change.json")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents-dev")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="运行一次任务")
    run_parser.add_argument("--goal", required=True)
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
    run_parser.set_defaults(func=_run)

    revert_parser = sub.add_parser("revert", help="回滚上一次写入的改动")
    revert_parser.add_argument("--root", default=".")
    revert_parser.set_defaults(func=_revert)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

