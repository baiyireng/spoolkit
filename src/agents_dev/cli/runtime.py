"""运行期装配。

把「用哪个模型、带哪些工具、接不接索引与记忆」拼成一个可直接跑的循环。
命令行各条路径都从这里取，差别只在传什么参数。
"""

import atexit
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from agents_dev.llm.providers import ProviderConfig, ProviderError, load_gateway
from agents_dev.net import system_proxy
from agents_dev import settings
from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.index.indexer import index_project, iter_source_files
from agents_dev import limits
from agents_dev.index.rank import prefetch as prefetch_text
from agents_dev.index.rank import prefetch_contents
from agents_dev.index.rank import prefetch_scope
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
from agents_dev.tools.edit import PendingChanges, register_edit_tools
from agents_dev.tools.exec import run_command_spec
from agents_dev import diagnosis
from agents_dev.tools.diagnosis import (
    read_diagnosis_spec,
    request_diagnosis_spec,
)
from agents_dev.tools.fs import list_dir_spec, read_file_spec
from agents_dev.tools.grant import Grants
from agents_dev.tools.help import tool_help_spec
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.verify import make_verifier
from agents_dev.tools.search import search_code_spec
from agents_dev.tools.stats import dir_stats_spec
from agents_dev.tools.survey import survey_spec
from agents_dev.tools.calc import calc_spec
from agents_dev.tools.dispatch import dispatch_spec
from agents_dev.tools.sources import SourceLog, check_numbers_spec

# 符号表给人「有哪些东西」，内容给人「它是怎么写的」。两块都要：
# 只有符号表时，模型会一直查、始终不下手（实测本地 7B 的整条轨迹里
# 连一次 read_file 都没有）。
# 这两个数搬进了登记表（limits.KNOBS），这里只留名字给外部导入。
PREFETCH_BUDGET = int(limits.knob("prefetch_budget").default)
PREFETCH_CONTENT_BUDGET = int(limits.knob("prefetch_content_budget").default)


def counter_for(gateway: ModelGateway) -> object:
    """挑一个 token 计数器：供应商能给真实的就用真实的。

    预算是整套架构的支点，而它原先无条件建立在**估算**分词上。实测偏差够大：
    估算放行、服务端拒绝（提示词 9772 token vs 上限 8192），而且那条拒绝
    会直接打断整个运行。llama.cpp 有 /tokenize，没理由不用。
    """
    factory = getattr(gateway, "token_counter", None)
    if callable(factory):
        try:
            return factory()
        except Exception:
            pass  # 拿不到就退回估算：计数不准不该让 agent 起不来
    return OfflineTokenCounter()


@dataclass
class LoopWiring:
    """一次装配里挂上的可选协作者。

    收成一个对象而不是一串关键字参数，理由是：这些协作者各自控制一条能力
    （记忆、教训、写操作、命令授权），散在参数表里时，调用方看不出自己
    漏传了什么——而漏传的表现是**那条能力静默失效**，不是报错。

    早先这套参数有十一个，改一处要数一遍；现在默认值集中在这里，
    调用方只写自己真正要接的那几个。
    """

    memory: object | None = None
    distiller: object | None = None
    pending: object | None = None
    approver: object | None = None
    grants: object | None = None
    lessons: object | None = None
    on_event: object | None = None
    # 改完自动跑一遍项目测试并把结果顶回去。默认开——实测模型自己
    # 几乎从不去跑（8 条失败里 0 次 run_command），指望它养成习惯不现实。
    auto_verify: bool = True
    verify: object | None = None
    # 额外可读根：由用户显式授权（--allow-read）。读得到，写不到，
    # 工作区绑定不变——「看一个目录」不该等于「换个项目」。
    read_roots: tuple[Path, ...] = ()
    # 这一步自己声明的范围（计划里的 scope）。有它就按它锚定预取：
    # 关键词排序会被步骤提示词里的噪音带偏，实测能把两个正文名额
    # 全给测试文件，而真正要改的文件一个都进不来。
    prefetch_anchors: tuple[str, ...] = ()
    # 要不要挂用户配置里的 MCP 外挂工具。默认挂——配了就是要用；
    # `--no-mcp` 用来临时排除它们（排查"是不是外挂工具在捣乱"时用）。
    mcp: bool = True


def resolve_provider_args(args) -> dict[str, str]:
    """把命令行、环境变量、用户配置合成一次运行真正用的值。"""
    resolved = {}
    for key in ("provider", "model", "base_url", "proxy", "script"):
        flag = str(getattr(args, key, "") or "")
        # 已经解析过的值会被写回 args（见 cli/app.main）。第二次解析时它看起来
        # 像"命令行给的"，来源就串了位——"来自：命令行"而实际来自配置文件，
        # 这种错误比不显示来源更坏。所以认得出的旧来源就留着。
        known = str(getattr(args, f"{key}_source", "") or "")
        if flag and known and str(getattr(args, key, "")) == flag:
            resolved[key] = flag
            resolved[f"{key}_source"] = known
            continue
        value, source = settings.resolve(key, flag)
        resolved[key] = value
        resolved[f"{key}_source"] = source
    if not resolved["provider"]:
        # 什么都没配：用假模型（离线可跑），但下面会给出怎么固定下来。
        resolved["provider"] = "fake"
        resolved["provider_source"] = "内置默认"
    return resolved


def provider_gateway(args, project_root):
    """按命令行参数装载网关。失败时打印原因并返回 None。

    集中在一处：三条执行路径都要用它，各写一遍的结果是某一条上
    悄悄漏掉代理或密钥来源，而那种问题只会表现为「连不上」。
    """
    values = resolve_provider_args(args)
    provider = values["provider"]
    script: tuple[str, ...] = ()
    if provider == "fake":
        raw_path = values["script"]
        if not raw_path:
            # 第一次用最容易撞这里：默认供应商是假模型，而报错原先只说
            # "需要 --script"，一个字都没提"你大概想接本地模型"。
            print(
                "假模型需要 --script 指定脚本文件。\n"
                "如果你是想接本地模型，加这两个参数就行："
                " --provider llamacpp --base-url http://127.0.0.1:8080\n"
                "想把它固定成默认（以后不用每次打）："
                " agents-dev config --set provider=llamacpp "
                "--set base_url=http://127.0.0.1:8080",
                file=sys.stderr,
            )
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
        model=values["model"],
        base_url=values["base_url"],
        proxy=values["proxy"] or system_proxy(),
        script=script,
        env=dict(__import__("os").environ),
    )
    try:
        gateway = load_gateway(provider, config)
    except ProviderError as exc:
        print(f"无法装载供应商 {provider}: {exc}", file=sys.stderr)
        return None

    # 本地服务的失败要在**开工之前**说清楚：连不上时后面的每一步都会以
    # 各种奇怪的形式报错（分词 502、HTTP 超时、窗口探测失败），
    # 而真正的原因只有一个，且只有一句话。
    if provider == "llamacpp" and gateway.context_window() is None:
        address = values["base_url"] or "(供应商默认地址)"
        print(
            f"连不上 llama.cpp 服务：{address}\n"
            f"（地址来自：{values.get('base_url_source') or '未设置'}）\n"
            "先确认服务起着，例如：\n"
            "  llama-server.exe -m <模型.gguf> --host 127.0.0.1 --port 8080 "
            "-c 8192 -ngl 99 --reasoning off\n"
            "地址不对就用 --base-url 指定，或固定下来："
            " agents-dev config --set base_url=http://127.0.0.1:8080",
            file=sys.stderr,
        )
        return None
    return gateway


def attach_index(
    project_root: Path,
    registry: ToolRegistry,
    tokenizer,
    pending=None,
    overrides=None,
    anchors: tuple[str, ...] = (),
):
    """建立（或复用）代码索引，注册索引工具并返回预取函数。

    索引是可选增强：建索引失败不应让整个 agent 起不来，
    所以这里只做最保守的处理，失败时退化为无索引模式。

    退化时必须**说出来**：模型看不到「本该有的符号工具」这件事，
    只会以为自己手上就这些。残的索引比没有更糟——查不到会被当成不存在，
    所以到量停下时同样按「没有索引」处理。
    """
    try:
        # 先数一遍再决定做不做：数一遍只走目录、不读文件，很便宜；
        # 而建索引是按文件数花钱的。项目明显太大时直接不做，
        # 别先花十几秒再把它丢掉。
        #
        # 顺序很重要：**必须在开库之前判断**。开库会顺手建出 .agent 目录，
        # 于是「跳过了索引」反而在用户的项目里留下一个空壳。
        # 上限是能力/机器标定值：本机 27B 上建索引的收益与代价在 2000 文件
        # 附近平衡，换个模型或换台机器就不是这个数了。
        max_files = int(limits.resolve("max_index_files", overrides)[0])
        index_seconds = limits.resolve("index_seconds", overrides)[0]
        candidates = sum(1 for _ in iter_source_files(project_root))
        if candidates > max_files:
            return _no_index(
                f"项目里有 {candidates} 个 Python 文件，超过上限 {max_files}，索引已跳过"
            )
        conn = open_db(project_root / ".agent" / "index.db")
        init_schema(conn)
        stats = index_project(
            project_root, conn, max_files=max_files, seconds=index_seconds
        )
    except Exception as exc:
        return _no_index(f"建索引失败（{type(exc).__name__}）")
    if stats.stopped:
        return _no_index(f"项目太大，索引已跳过：{stats.stopped}")

    registry.register(find_symbol_spec(project_root, conn, pending))
    registry.register(file_symbols_spec(conn, pending))
    registry.register(find_callers_spec(conn, pending))

    def prefetch_for(goal: str) -> str:
        # 预取预算走登记表：每次运行都要付，且与模型强弱强相关。
        content_budget = int(
            limits.resolve("prefetch_content_budget", overrides)[0]
        )
        # 锚定部分先占额度：范围是「这一步要动什么」最可靠的信号，
        # 而关键词排序是在整段提示词上做的，容易被噪音带偏。
        anchored, spent, covered = "", 0, set()
        if anchors:
            anchored, spent, covered = prefetch_scope(
                project_root, anchors, tokenizer, max_tokens=content_budget
            )
        # 有锚定时**不再给符号表**：锚定给的是"这一步要改的那个文件 + 它旁边
        # 有什么"，而符号表是按关键词在整段提示词上猜出来的——在 50 题那轮里
        # 它猜出来的是两个测试文件，真正要改的 main.py 排第三。
        # 两者重叠时留着符号表，等于每轮多付一份猜错的名单。
        # 需要"还有谁调用它"时，那是 find_callers 的活（模型可以自己问）。
        symbols = ""
        if not anchored:
            symbols = prefetch_text(
                conn,
                goal,
                tokenizer,
                int(limits.resolve("prefetch_budget", overrides)[0]),
            )
        contents = prefetch_contents(
            conn,
            project_root,
            goal,
            tokenizer,
            max_tokens=max(0, content_budget - spent),
            skip=covered,
        )
        return "\n\n".join(part for part in (anchored, symbols, contents) if part)

    return prefetch_for


def _no_index(reason: str):
    """没有索引时的预取函数：只负责把原因说清楚。

    空着不说是最坏的选择——模型会以为项目里就这些东西，然后把
    「索引里没有」当成「代码里没有」。
    """

    def explain(_goal: str) -> str:
        return f"（本项目的代码索引不可用：{reason}。符号类工具因此没有提供，改用 read_file / search_code / list_dir。）"

    return explain


# 外挂的 MCP 服务在进程里只起一次：自主编排每一步都会重新装配一次注册表，
# 每步各起一遍的话，50 步就是 50 个外部进程。
_MCP_CLIENTS: list = []


def attach_mcp(registry: ToolRegistry, announce=None) -> list:
    """把用户配置里的 MCP 服务挂成工具。没配就是空操作。

    连不上的服务**跳过并说出来**：悄悄降级的结果是模型找不到工具、
    开始自己造轮子，而人以为配好了。
    """
    from agents_dev import settings
    from agents_dev.mcp.client import McpClient, McpError, register_mcp_tools

    global _MCP_CLIENTS
    if not settings.load_mcp_servers():
        return []
    if not _MCP_CLIENTS:
        for config in settings.load_mcp_servers():
            client = McpClient(config)
            try:
                client.start()
            except McpError as exc:
                if announce is not None:
                    announce(f"外部工具 {config.name} 连不上：{exc}（已跳过）")
                continue
            _MCP_CLIENTS.append(client)
        atexit.register(_close_mcp_clients)
    if _MCP_CLIENTS:
        register_mcp_tools(registry, _MCP_CLIENTS, announce=announce)
    return list(_MCP_CLIENTS)


def _close_mcp_clients() -> None:
    for client in _MCP_CLIENTS:
        client.close()
    _MCP_CLIENTS.clear()


def assemble_loop(
    project_root: Path,
    gateway: ModelGateway,
    config: Config | None = None,
    wiring: LoopWiring | None = None,
) -> AgentLoop:
    """用给定网关装配完整循环：注册全部工具、建索引、接上预取。

    预算（窗口、步数）走 Config，可选的协作者走 LoopWiring。
    两者分开是因为性质不同：前者是「这次跑多大」，后者是「挂上哪些能力」。
    """
    settings = config or Config(project_root=project_root)
    parts = wiring or LoopWiring()
    registry = ToolRegistry()
    sources = SourceLog()
    # 读工具都接上 pending：待确认的改动优先于磁盘。不接的话，模型刚写完
    # 一个文件，read_file 却给它旧内容——而 run_command 在试跑副本里看到的
    # 是新内容，同一个模型活在两套矛盾的世界里。
    registry.register(read_file_spec(project_root, parts.pending, parts.read_roots))
    registry.register(list_dir_spec(project_root, parts.pending, parts.read_roots))
    registry.register(search_code_spec(project_root, parts.pending, parts.read_roots))
    registry.register(dir_stats_spec(project_root, parts.read_roots))
    registry.register(calc_spec())
    # 索引里只写「名字 + 参数名 + 一句干什么」，完整说明按需从这里取。
    # 注册在最后：它绑定的是这个注册表本身，而注册表是逐个长起来的。
    registry.register(tool_help_spec(registry))
    # 台账与核对工具共用同一个 SourceLog：模型只能读，循环负责写。
    registry.register(check_numbers_spec(sources))
    # 怀疑是环境或工具本身有问题时的申请通道。只登记与读回，
    # 报告由具备真实环境权限的一侧出具——它自己写不了。
    registry.register(request_diagnosis_spec(project_root))
    registry.register(read_diagnosis_spec(project_root))
    registry.register(
        run_command_spec(
            project_root,
            parts.pending,
            parts.approver,
            parts.grants,
            max_output=int(limits.resolve("max_output_chars", settings.overrides)[0]),
            default_timeout=int(
                limits.resolve("command_timeout", settings.overrides)[0]
            ),
            max_timeout=int(
                limits.resolve("max_command_timeout", settings.overrides)[0]
            ),
        )
    )
    if parts.pending is not None:
        register_edit_tools(registry, project_root, parts.pending)
    if parts.memory is not None:
        # 主循环用的注册表在这里构造，所以 recall 也必须在这里注册，
        # 否则提示词会提到一个只有派发路径才有的工具。
        registry.register(recall_spec(parts.memory))

    # 供应商能给出真实分词时就用真实的：预算建立在估算上，中文/代码混排时
    # 偏差足以让本地判定放行、服务端拒绝（实测提示词 9772 > 上限 8192）。
    tokenizer = counter_for(gateway)
    verifier = parts.verify
    if verifier is None and parts.auto_verify and parts.pending is not None:
        verifier = make_verifier(
            project_root, parts.pending, overrides=settings.overrides
        )

    # 会话内的派发入口。注册在最后：它绑定的就是这个注册表里的工具集，
    # 而子智能体拿的是按角色裁剪后的那一份（里面没有 dispatch，不会递归）。
    registry.register(
        dispatch_spec(gateway, registry, settings, tokenizer, verify=verifier)
    )
    # 外挂的 MCP 工具（如果用户配过）。放在这里而不是更早：它要往同一个
    # 注册表里加东西，而 tool_help 绑的是这个注册表，晚加一样能查到。
    mcp_clients = attach_mcp(registry, announce=print) if parts.mcp else []
    # 「收齐这一处该看的」是一个**动作**，不是一个必经阶段：模型自己决定
    # 何时用、看哪一处。它省的是步数——原先要「列目录 → 猜文件名 → 读 →
    # 猜错了再换」，实测模型猜错过文件名，那一步就白花了。
    registry.register(
        survey_spec(
            project_root,
            tokenizer,
            parts.pending,
            parts.read_roots,
            default_budget=int(limits.resolve("survey_budget", settings.overrides)[0]),
        )
    )

    def incoming_reports() -> str:
        """上一轮回来的诊断报告：开局就摆到模型面前，不用它记得去查。"""
        return diagnosis.render_incoming(project_root)

    return AgentLoop(
        gateway=gateway,
        tokenizer=tokenizer,
        registry=registry,
        config=settings,
        prefetch=attach_index(
            project_root,
            registry,
            tokenizer,
            parts.pending,
            settings.overrides,
            anchors=tuple(parts.prefetch_anchors),
        ),
        memory=parts.memory,
        lessons=parts.lessons,
        distiller=parts.distiller,
        on_event=parts.on_event,
        verify=verifier,
        incoming=incoming_reports,
        read_roots=parts.read_roots,
        sources=sources,
    )


def build_loop(project_root: Path, script: list[str], window: int = 4096) -> AgentLoop:
    """装配一个由脚本化假模型驱动的循环（离线可用）。"""
    tokenizer = OfflineTokenCounter()
    return assemble_loop(
        project_root,
        FakeModel(script=script, tokenizer=tokenizer),
        # 假模型没有判断力：它的「续期」只是把脚本里下一条当成结论。
        # 这条路是离线演示与测试用的，督导在这里只会有害。
        config=Config(project_root=project_root, context_window=window, supervise=False),
    )


def build_memory(
    project_root: Path, window: int, session_id: str = "cli", overrides=None
):
    """在 .agent 下建立记忆库与热记忆文件。"""
    state_dir = project_root / ".agent"
    conn = open_db(state_dir / "memory.db")
    init_memory_schema(conn)
    return MemorySession(
        overrides=overrides,
        conn=conn,
        hot_path=state_dir / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=window,
        session_id=session_id,
    )


def open_memory(
    project_root: Path,
    window: int,
    session_id: str,
    model: str = "",
    overrides=None,
) -> MemorySession:
    """建立记忆会话，并在登记前检查工作区绑定。

    检查必须在登记之前：登记会写入当前工作区，先登记就把
    「上次绑定在哪」这个信息当场覆盖掉了，检查也就永远查不出问题。
    """
    memory = build_memory(project_root, window, session_id, overrides=overrides)
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
        # 四档。第三、四档刻意分开：只拒这一次，和「本轮别再问了」是两种
        # 意思，混成一个的结果要么是骚扰，要么是死路。
        answer = input(
            "[o]本次允许 / [s]始终允许（本工作区，落盘） / "
            "[n]拒绝 / [b]本轮全部拒绝 → "
        ).strip().lower()
        if answer in ("o", "once", "y"):
            return "once"
        if answer in ("s", "session", "always", "a"):
            return "always"
        if answer in ("b", "block"):
            return "block"
        return "deny"

    return approver, grants
