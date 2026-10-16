"""提示词的机械核对：改提示词必做。

三件事，都是机器能判的：
1. **占位符一个不丢**：`{name}` 形态的 format 占位符集合与改造前一致（丢一个
   就是运行期 KeyError，或者更糟——把 `{goal}` 原样发给模型）。
2. **语义锚点一个不缺**：那些「丢了就是行为变了」的原句片段逐条 `assert`。
3. **system 区段放得下**：按 `cli/runtime.assemble_loop` 的真实路径装出**最全**
   工具集，量它的 system 区段——必须在 `effective()` 之内并留出 ≥5% 余量；
   另有一条「构造超支 → 必红」的用例确认这道闸门有牙，还有一条确认压缩之后
   每件已注册工具仍然可达、可发现（不许把工具悄悄藏起来）。

改造前的占位符清单在这里逐字冻结（从 2026-10-03.2 那一版抓的）。
"""

import re
from pathlib import Path

import pytest

from spoolkit.context import templates as T
from spoolkit.llm.tokenizer import OfflineTokenCounter

# 改造前（PROMPT_VERSION 2026-10-03.2）全部模板里的占位符集合，逐字冻结。
FROZEN_PLACEHOLDERS = {
    "SYSTEM": ["tools", "workflow"],
    "WORKFLOW_HEAD": ["lines"],
    "WORKFLOW_FIND": ["names"],
    "WORKFLOW_READ_ROOTS": ["roots"],
    "OUTPUT_TRUNCATED": ["limit"],
    "SUPERVISE": [
        # `tools`：督导的证据包里带注册表全量清单（它的建议必须出自这份清单），
        # 模板里因此有 `{tools}`。黄金表此前漏了它——**表落后于模板**，
        # 不是模板多写了占位符（渲染一直正常，缺的话每条督导用例都会 KeyError）。
        "asking", "goal", "step", "limit", "ceiling", "room", "state", "trace",
        "tools", "edits", "calls", "resets", "repeats", "no_edit", "tokens", "cycle",
        "oscillations", "same_query",
    ],
    "DECOMPOSE": ["goal", "context", "limit"],
    "DECOMPOSE_CONTINUE": ["planned", "start", "ask"],
    "REFLECT": ["goal", "results"],
    "DECOMPOSE_BATCH_LIMIT": ["limit"],
    "EXTEND": ["done", "reflection", "start", "ask"],
    "DECOMPOSE_FILL": ["missing", "ask"],
    "DISPATCH": ["goal", "context"],
    "JUDGE_REVIEW": ["acceptance", "review"],
    "REPAIR": ["goal", "acceptance", "reasons"],
    "DISTILL": ["body", "limit"],
}

# 「丢了就是行为变了」的原句片段。分两类：这一轮**新加**的（确认通道），
# 与**原来就在**的（护栏、便宜路径、验证分派）。
NEW_ANCHORS = {
    "WORKFLOW_REWRITE_DECLARE": [
        "rewrite_reason",
        "confirm_rewrite",
        "只发项 id",
        "不重发正文",
        "acked",
        "dropped",
        "项 id",
        "不会落盘",
        "acked_disappeared",
        "不得原样重提",
        "消失清单",
        "令牌",
        "没有令牌",
    ],
}
KEPT_ANCHORS = {
    "WORKFLOW_REWRITE_DECLARE": ["合并", "去重", "搬运", "rewrite_reason"],
    "WORKFLOW_EDIT_LINES": ["replace_lines", "输出预算"],
    "WORKFLOW_VERIFY": ["verify_file", ".html", ".js", ".py"],
    "WORKFLOW_RUN": ["run_command", "自动验证"],
    "WORKFLOW_ACT": ["重复两次以上"],
    "WORKFLOW_WRITE_SAFE": ["不用停下来等确认"],
    "EMPTY_TURN_FEEDBACK": ["done"],
}

# 用户第 3 条质疑（意图要含「想拿什么 + 接下来怎么做」）与第 5 条发现
# （replace_lines 的行号锚点）落在提示词里的那几处。丢了就是行为变了。
INTENT_ANCHORS = {
    "SYSTEM": [
        "想拿到什么",
        "拿到之后接下来怎么做",
        "不算意图",
    ],
    "WORKFLOW_EDIT_LINES": [
        "expect",
        "锚点对不上就会被拒绝",
    ],
}


def _placeholders(name: str) -> list[str]:
    text = getattr(T, name)
    return sorted(set(re.findall(r"\{([a-z_]+)\}", text)))


def test_占位符与改造前逐字一致() -> None:
    for name, expected in FROZEN_PLACEHOLDERS.items():
        assert _placeholders(name) == sorted(expected), name


def test_新锚点一条不缺() -> None:
    for name, anchors in NEW_ANCHORS.items():
        text = getattr(T, name)
        missing = [one for one in anchors if one not in text]
        assert not missing, f"{name} 缺: {missing}"


def test_旧锚点一条不缺() -> None:
    for name, anchors in KEPT_ANCHORS.items():
        text = getattr(T, name)
        missing = [one for one in anchors if one not in text]
        assert not missing, f"{name} 缺: {missing}"


def test_意图口径与行号锚点一条不缺() -> None:
    """改了提示词就必须有的第二组机械核对（占位符核对见上）。

    这一组是「意图要含想拿什么 + 接下来怎么做」与「replace_lines 必须带
    expect」这两条要求的落点：它们只写在提示词里，没有别的地方会提醒。
    """
    missing: list[str] = []
    for name, anchors in INTENT_ANCHORS.items():
        text = getattr(T, name)
        missing += [f"{name}:{one}" for one in anchors if one not in text]
    assert not missing, f"语义锚点缺: {missing}"


def test_版本号与改动记录同步() -> None:
    """升了版本就必须有那一行记录：不然「结果归因到某一版」是空话。"""
    source = Path(T.__file__).read_text(encoding="utf-8")
    assert f'PROMPT_VERSION = "{T.PROMPT_VERSION}"' in source
    assert T.PROMPT_VERSION in source.split("# 改动记录", 1)[1]


# 探针首轮（5 个形状 3 绿 2 红）抓出的两个提示层缺陷，各自对应的锚点。
# 它们是「丢了就是行为变了」的那几句，所以同样机械核对。
PROBE_ANCHORS = {
    # merge-html 红：工具集里没有删除文件的工具，模型只能让用户自己 rm。
    "WORKFLOW_DELETE": [
        "delete_file",
        "不删目录",
        "待确认",
        "revert",
        "不要建议用户自己去 rm",
        "假装删除",
    ],
    # rename-cross-file 红：只改了 .py，README / docs 没动却说「已统一」。
    "WORKFLOW_RENAME": [
        "README",
        "docs",
        "残留清单",
        "统一",
        "与事实不符",
    ],
}


def test_探针轮新增的锚点一条不缺() -> None:
    missing: list[str] = []
    for name, anchors in PROBE_ANCHORS.items():
        text = getattr(T, name)
        missing += [f"{name}:{one}" for one in anchors if one not in text]
    assert not missing, f"语义锚点缺: {missing}"


def test_探针轮新增的两段没有引入新的占位符() -> None:
    """新增的两段是纯文本：工具名与参数名写死在里面。

    一旦它们被写成 `{delete_tool}` 这种占位符，`SYSTEM.format(tools=..., workflow=...)`
    会在运行期 KeyError——而那条路只在真的注册了删除工具的工作区里才走到。
    """
    for name in PROBE_ANCHORS:
        assert _placeholders(name) == [], name


# --- 本轮（rename-cross-file 复跑）新增的两句：各自对应的锚点 -----------------
#
# 这两句是「丢了就是行为变了」的那几句，所以同样机械核对。判据与话术的主体
# 在代码层（`verify.py` 的中间态说明、`agent/audit.py` 的三类发现），
# 提示词只做最小承载——所以这里只冻两句，不再往上加。
RENAME_REGRESSION_ANCHORS = {
    # 迎合测试：测试失败时**不许动业务逻辑**。原来只说「不得修改测试」，
    # 而实测它为了过测试去删了 render() 的表头行——那是删业务逻辑，不是改测试。
    "WORKFLOW_TESTS_ARE_SPEC": [
        "不得为了让测试通过而修改它",
        "不得为了让测试通过而删掉或削弱业务逻辑",
    ],
    # 中间态：验证对象是累计改动，半新半旧不是代码失败。
    "WORKFLOW_TEST_SCOPE": [
        "本次运行累计的改动",
        "半新半旧不是代码失败",
    ],
}


def test_改名复跑新增的两句一条不缺() -> None:
    missing: list[str] = []
    for name, anchors in RENAME_REGRESSION_ANCHORS.items():
        text = getattr(T, name)
        missing += [f"{name}:{one}" for one in anchors if one not in text]
    assert not missing, f"语义锚点缺: {missing}"


# system 区段是**每个请求都要付一次**的税。它的真实上界不是
# `budget.FIXED_QUOTAS["system"] = 900`——那个数管的是 `Budget.allocation()`
# 那张表；`Assembler.assemble` 对必留区段**不裁剪**，真正卡住运行的是另一条：
# 必留区段不得超过 `effective()`（4096 窗口下 3482）。一旦越过，`spool run`
# 当场抛「必留区段 system 超出有效预算」，一次运行都起不来。
#
# **上一版这条用例量错了对象**：它自己列了 9 件关键工具（只够让 workflow 的
# 各个分支出现），于是量的是「system 区段不含工具索引的那部分」，还另立了一个
# 2900 的手工上界。真实运行装的是**全量工具**（清单见 `最全装配工具清单`，
# 当年 26 件、删掉编排层之后 25 件）——改动前实测 26 件下 system 3548 /
# effective 3482，**早就超了**，而这条用例一直是绿的。
# 它守的不是真实装配，所以真实装配的超支只能等到发布才暴露（本轮就是这件事）。
#
# 现在改成量**真实最全装配**（走 `cli/runtime.assemble_loop`，不另抄一份注册
# 清单），并直接对着 `effective()` 判，不再另立手工上界——手工上界正是它掩盖
# 超支的地方。余量要求 ≥5%：留出的不是装饰，而是「再加一两件工具」的空间。
SYSTEM_RESERVE_RATIO = 0.05

#: 最全装配必须盖住的工具。**它写的是「装配了什么」**：少了任何一件，
#: 就说明这次量到的不是真实装配，数字不算数（用例会先在这一条上报出来）。
#:
#: 改动前（离线计数，4096 窗口）：26 件 → system 3548 / effective 3482，超 66。
#: 压缩之后：26 件 → system 3195 / effective 3482，余 287（8.2%）。
#: 删掉编排层之后：25 件 → system 3177 / effective 3482，余 305（8.8%）。
#: （那件工具的参数 schema 有 675 字，但它不在这条读数里：它进的是每轮的
#: `response_schema`，也就是解码约束，不进 system 区段。）
最全装配工具清单 = (
    "read_file",
    "list_dir",
    "search_code",
    "dir_stats",
    "calc",
    "web_read",
    "tool_help",
    "check_numbers",
    "request_diagnosis",
    "read_diagnosis",
    "run_command",
    "write_file",
    "confirm_rewrite",
    "delete_file",
    "replace_lines",
    "verify_file",
    "recall",
    "remember",
    "search_history",
    "dispatch",
    "research",
    "survey",
    "find_symbol",
    "file_symbols",
    "find_callers",
)


def _记忆会话(root: Path):
    """挂记忆工具要的那套东西（与 `tests/tools/test_capabilities.py` 同一条路）。"""
    from spoolkit.memory.session import MemorySession
    from spoolkit.memory.store import init_memory_schema
    from spoolkit.store.db import open_db

    conn = open_db(root / ".agent" / "memory.db")
    init_memory_schema(conn)
    return conn, MemorySession(
        conn=conn,
        hot_path=root / ".agent" / "memory.md",
        counter=OfflineTokenCounter(),
        context_window=4096,
        session_id="full",
    )


def _最全装配(tmp_path: Path):
    """按 `cli/runtime.assemble_loop` 的**真实装配路径**装出最全的一种工具集。

    装配的是什么（这正是上一版那条冻结线用例漏掉的部分）：

    - 读/查/量/算五件（read_file / list_dir / search_code / dir_stats / calc）；
    - web：`web_read` + `research`（研究子会话自带 web_fetch / web_search）；
    - 索引三件（find_symbol / file_symbols / find_callers）——项目里放一个
      **400 行的大文件**是有意的：小项目只注册 write_file，放个大文件才会走到
      `register_edit_tools` 的非小项目那一支，把 replace_lines 也装进来；
    - 编辑四件（write_file / confirm_rewrite / delete_file / replace_lines）；
    - 记忆三件（recall / remember / search_history）；
    - 派发、诊断两件、核对、tool_help、survey、verify_file。

    外挂（MCP）**不在这里**：它由用户配置决定、件数没有上界，所以它由
    `test_加一批外挂工具就超支即红` 那条守着。
    """
    from spoolkit.cli.runtime import LoopWiring, assemble_loop
    from spoolkit.config import Config
    from spoolkit.fetch.client import Fetched, WebPolicy
    from spoolkit.llm.fake import FakeModel
    from spoolkit.tools.edit import PendingChanges
    from spoolkit.tools.webfetch import WebAccess

    root = tmp_path / "proj"
    root.mkdir()
    (root / "big.py").write_text("y = 1\n" * 400, encoding="utf-8")
    counter = OfflineTokenCounter()
    conn, session = _记忆会话(root)

    def fake_fetch(url: str, policy: WebPolicy) -> Fetched:
        return Fetched(url=url, status=404, content_type="text/html", body=b"")

    loop = assemble_loop(
        root,
        FakeModel(script=[], tokenizer=counter),
        config=Config(project_root=root, context_window=4096, supervise=False),
        wiring=LoopWiring(
            mcp=False,
            pending=PendingChanges(root),
            memory=session,
            web=WebAccess(root=root, policy=WebPolicy(chunk_chars=400), fetch=fake_fetch),
        ),
    )
    return loop, conn


@pytest.fixture
def full_loop(tmp_path: Path):
    loop, conn = _最全装配(tmp_path)
    try:
        yield loop
    finally:
        conn.close()


def _首轮system区段(loop) -> str:
    """把**主循环真正装配出来的** system 段取回来。

    刻意不自己拼 `T.SYSTEM.format(tools=..., workflow=...)`：装配路径上还有
    persona / read_roots / from_channel 这些分支，自己拼一份就是第二个实现，
    量到的东西可能与真实运行无关——上一版那条用例的毛病正是「量的不是真实
    装配」。这里让装配器自己说话：它不抛错，就说明这一段真的放得下。
    """
    from spoolkit.agent.state import TaskState
    from spoolkit.llm.types import Message

    state = TaskState(task_id="full", goal="把 big.py 里的重复常量抽成一个模块")
    result = loop._assemble(
        state,
        [Message(role="user", content=state.goal)],
        None,
        prefetched="",
        hot="",
    )
    return result.messages[0].content


def test_最全装配下system区段不超预算(full_loop) -> None:
    """最全装配：system 必须在 `effective()` 之内，且留出 ≥5% 的余量。

    判据直接是运行期那一条（必留区段 ≤ `effective()`），不再另立手工上界。
    """
    names = full_loop.registry.names()
    missing = [name for name in 最全装配工具清单 if name not in names]
    assert not missing, (
        f"这次没有装全，量出来的数不算数：缺 {missing}。"
        "装配路径或能力开关变了就要同步 `最全装配工具清单`。"
    )

    system_text = _首轮system区段(full_loop)
    system = OfflineTokenCounter().count(system_text)
    effective = full_loop._budget.effective()
    reserve = effective - system
    assert system <= effective, (
        f"system 区段 {system} token 超过有效预算 {effective}"
        f"（{len(names)} 件工具）：`spool run` 会抛"
        "「必留区段 system 超出有效预算」，一次运行都起不来。"
    )
    assert reserve >= effective * SYSTEM_RESERVE_RATIO, (
        f"system 区段 {system} / 有效预算 {effective}，"
        f"余量只剩 {reserve}（{reserve / effective:.1%}，"
        f"要求 ≥{SYSTEM_RESERVE_RATIO:.0%}，{len(names)} 件工具）。"
        "这一段是每个请求都要付一次的税：要加就先压掉等量的旧话，"
        "或者把低频能力做成可发现的按需装载（但不得让模型调不到它）。"
    )


def test_加一批外挂工具就超支即红(full_loop) -> None:
    """构造一次**必然超支**的装配，确认这条闸门真的有牙。

    外挂（MCP）工具走的就是这条路：用户在配置里多挂一个服务，它带的每件工具
    都进 system 区段的索引，而件数没有上界。所以这里模拟一个带 40 件工具的外挂
    服务，要求两件事同时成立：

    ① 同一口径算出来的余量已经跌到 5% 以下——上限那条用例会红；
    ② 真装配时 `Assembler.assemble` 抛「必留区段 system 超出有效预算」
       ——也就是**真的跑不起来**，而不是只有一条用例难看。

    没有这条，上限那条可能被写成永远成立的形式，而真实超支要等到发布才暴露。
    """
    from spoolkit.tools.types import ToolResult, ToolSpec

    for index in range(40):
        full_loop.registry.register(
            ToolSpec(
                name=f"mcp__svc__op{index}",
                description="外挂服务提供的一件工具。",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=lambda args: ToolResult(ok=True, content=""),
                brief="外挂服务提供的一件工具",
                group="外挂",
            )
        )

    from spoolkit.agent.loop import build_workflow
    from spoolkit.agent.state import TaskState
    from spoolkit.llm.types import Message

    system = OfflineTokenCounter().count(
        T.SYSTEM.format(
            tools=full_loop.registry.describe(),
            workflow=build_workflow(full_loop.registry, full_loop.read_roots),
        )
    )
    effective = full_loop._budget.effective()
    assert system > effective * (1 - SYSTEM_RESERVE_RATIO), (
        f"加了 40 件外挂工具之后 system 才 {system}（有效预算 {effective}）——"
        "这条用例没有真的构造出超支，它会假绿"
    )

    state = TaskState(task_id="full", goal="随便改一处")
    with pytest.raises(ValueError, match="必留区段 system 超出有效预算"):
        full_loop._assemble(
            state,
            [Message(role="user", content=state.goal)],
            None,
            prefetched="",
            hot="",
        )


def test_最全装配下每件工具都可达可发现(full_loop) -> None:
    """压缩提示词**不许把任何一件工具藏起来**：最全装配下每件都要可达。

    四种「可达」都要成立，各自盖住一种不同的丢能力方式：

    ① **采样层**：名字在 `build_turn_schema` 的工具 enum 里。不在的话，约束
       解码永远采不出这个名字——提示词写得再全也调不到（最隐蔽的一种）；
    ② **提示层**：名字在每轮常驻的 `{tools}` 索引里（参数名也一并看得到）；
    ③ **按需层**：`tool_help("名字")` 能展开完整说明与参数含义——索引省掉的
       那部分就是从这儿取的；
    ④ **派发层**：`registry.invoke` 那条路上它不是「未知工具」，且绑着实现；
    ⑤ **清点层**：`tool_help("*")` 列得出**全部**工具——「不用先知道名字也能
       发现有哪几件能力」的那条路，列漏一件，那件就变成要靠猜。

    换句话说：索引可以压短、描述可以搬去按需取，但**能力本身一件都不许消失**。
    """
    from spoolkit.tools.types import ToolCall

    registry = full_loop.registry
    index = registry.describe()
    enum = full_loop._schema["properties"]["tool_calls"]["items"]["properties"]["name"][
        "enum"
    ]
    assert len(enum) == len(registry.names()), "采样约束里的工具名单与注册表不一致"

    for name in registry.names():
        spec = registry.get(name)
        assert spec is not None and callable(spec.handler), f"{name} 没绑上实现"
        assert name in enum, f"{name} 不在采样约束里，模型永远叫不到它"
        assert name in index, f"{name} 不在每轮常驻的工具索引里，模型看不见它"
        help_result = registry.invoke(
            ToolCall(name="tool_help", arguments={"name": name})
        )
        assert help_result.ok and name in help_result.content, (
            f"tool_help(\"{name}\") 展不开它的完整说明——索引里省掉的那部分没地方取"
        )
        problem = registry.validate(name, {})
        assert problem is None or "未知工具" not in problem, (
            f"{name} 走 invoke 那条路会被判成未知工具"
        )

    everything = registry.invoke(ToolCall(name="tool_help", arguments={"name": "*"}))
    assert everything.ok, f"tool_help(\"*\") 没给出清单：{everything.content[:200]}"
    absent = [name for name in registry.names() if name not in everything.content]
    assert not absent, f'tool_help("*") 没有列出：{absent}'

