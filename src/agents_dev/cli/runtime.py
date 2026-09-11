"""运行期装配。

把「用哪个模型、带哪些工具、接不接索引与记忆」拼成一个可直接跑的循环。
命令行各条路径都从这里取，差别只在传什么参数。
"""

import json
import sys
from pathlib import Path

from agents_dev.llm.providers import ProviderConfig, ProviderError, load_gateway
from agents_dev.net import system_proxy
from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.index.indexer import index_project
from agents_dev.index.rank import prefetch as prefetch_text
from agents_dev.index.tools import file_symbols_spec, find_callers_spec, find_symbol_spec
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.memory.lessons import match_lessons, prune_lessons, record_outcome
from agents_dev.memory.session import MemorySession
from agents_dev.memory.store import check_binding, init_memory_schema, record_session
from agents_dev.memory.tools import recall_spec
from agents_dev.memory.transcript import recent_messages, render_transcript
from agents_dev.store.db import init_schema, open_db
from agents_dev.tools.edit import PendingChanges, replace_lines_spec, write_file_spec
from agents_dev.tools.exec import run_command_spec
from agents_dev.tools.fs import list_dir_spec, read_file_spec
from agents_dev.tools.grant import Grants
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.search import search_code_spec

PREFETCH_BUDGET = 400


def provider_gateway(args, project_root):
    """按命令行参数装载网关。失败时打印原因并返回 None。

    集中在一处：三条执行路径都要用它，各写一遍的结果是某一条上
    悄悄漏掉代理或密钥来源，而那种问题只会表现为「连不上」。
    """
    script: tuple[str, ...] = ()
    if args.provider == "fake":
        raw_path = getattr(args, "script", "")
        if not raw_path:
            print("假模型需要 --script 指定脚本文件。", file=sys.stderr)
            return None
        script_path = Path(raw_path)
        if not script_path.exists():
            print(f"脚本文件不存在: {script_path}", file=sys.stderr)
            return None
        raw = json.loads(script_path.read_text(encoding="utf-8"))
        script = tuple(
            item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            for item in raw
        )

    config = ProviderConfig(
        project_root=project_root,
        model=args.model,
        base_url=args.base_url,
        proxy=args.proxy if args.proxy else system_proxy(),
        script=script,
        env=dict(__import__("os").environ),
    )
    try:
        return load_gateway(args.provider, config)
    except ProviderError as exc:
        print(f"无法装载供应商 {args.provider}: {exc}", file=sys.stderr)
        return None


def attach_index(project_root: Path, registry: ToolRegistry, tokenizer):
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
        prefetch=attach_index(project_root, registry, tokenizer),
        memory=memory,
        lessons=lessons,
        distiller=distiller,
    )


def build_loop(project_root: Path, script: list[str], window: int = 4096) -> AgentLoop:
    """装配一个由脚本化假模型驱动的循环（离线可用）。"""
    tokenizer = OfflineTokenCounter()
    return assemble_loop(
        project_root,
        FakeModel(script=script, tokenizer=tokenizer),
        window=window,
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
