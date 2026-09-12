"""Agent 主循环。

设计要点：任务状态每轮注入，对话历史只保留最近若干轮，
因此上下文可以被安全地重置——状态不丢，历史可弃。

预算守卫依据 demand_tokens（裁剪前的需求）而非实际装入量来判断：
装配器最多只能装到有效预算，用实际装入量判断触发线永远不会触发。
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from agents_dev.agent.protocol import ParseFailure, build_turn_schema, parse_turn
from agents_dev.agent.state import TaskState, clear_state, load_state, save_state
from agents_dev.agents.supervisor import EXTEND, STOP, Evidence, Verdict, supervise
from agents_dev.config import Config
from agents_dev.context.assembler import Assembler
from agents_dev.context.budget import Budget
from agents_dev.context.sections import Section
from agents_dev.context import templates as T
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.retry import chat_with_escalation
from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.llm.types import ChatRequest, Message
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.types import ToolCall, ToolResult
from agents_dev.tools.verify import ENVIRONMENT_MARKER
from agents_dev.errors import ContextOverflowError

SYSTEM_PROMPT = T.SYSTEM

LOOKUP_TOOLS = ("find_symbol", "file_symbols", "find_callers")
EDIT_TOOLS = ("replace_lines", "replace_text", "write_file")

# 同一个调用连续重复到第几次时加提醒（仍然执行），到第几次时不再执行。
#
# 阈值来自实测，不是拍的：本地 Qwen2.5-Coder-7B 跑回归集时，连续 10 次
# 调用同一个 find_callers（参数一字不差），把 12 步预算全烧光。
# 第二次先给提醒、不阻断——调用有时确实有意义（比如文件刚被改过）；
# 第三次中间没有任何别的动作，结果不可能变，再执行只是在消耗步数。
REPEAT_WARN_AT = 2
REPEAT_BLOCK_AT = 3

# 重复到这个次数还停不下来，就升级给督导。
#
# 前面两档是**机械**的（提醒、不执行），它们只挡「参数一模一样」的重复。
# 挡不住的剩下两种情况：一是它换着花样绕（每次都不同，机械计数器永远归零），
# 二是它明知道在重复也停不下来。这两种都得看它到底在干什么才知道怎么办——
# 交给督导判断，而不是继续加规则。
REPEAT_INTERVENE_AT = 4

# 督导给的续期加起来最多到这里（基础预算的倍数）。
# 这不是任务预算，是安全线：没有它，一个卡住的任务能把 GPU 烧一整夜。
STEP_CEILING_FACTOR = 8

# 只看「连续相同」会漏掉交替打转：A、B、A、B…每一步都和上一步不同，
# 计数每次都被重置。实测审查者用 git status / git diff 交替复读了 11 步，
# 一次都没被拦住。所以再加一条频率判据：同一个调用在最近几次里出现够多，
# 同样是原地打转，不管中间夹了什么。
RECENT_WINDOW = 6

# 连续多少步没有提出任何改动，就收窄输出通道。
# 重复调用检测只盖得住「参数完全相同」的打转，盖不住「每次都换一个查询」
# 的漫游——实测那条轨迹 12 步里换了 8 种不同的调用，一次都没被拦住。
NO_EDIT_LIMIT = 4

# 漫游到这个步数还没产出，就升级给督导。
#
# NO_EDIT_LIMIT 那一步做的是**收窄输出通道**（只能写），它盖得住「它还想查」
# 这一种。盖不住的是「收窄之后它去翻别的文件」——每一步换一个查询，
# 机械计数器永远归零。到这一步该有人看看它到底在干什么，而不是继续加规则。
NO_EDIT_INTERVENE_AT = 8

# 空回合连续出现到这个次数就收尾。
#
# 模型想表达「改动提完了、在等你确认」时，只能发出一个空回合；协议里
# 没有这个表达方式，于是它判为无效、回灌、再发一遍。实测连发 11 次，
# 把预算全烧在复读上。它不是要再想一会儿，是卡住了——这时候把已经提出
# 的改动交给用户判断，比继续复读诚实，也便宜。
#
# 阈值取 4 而不是 2：实测阈值太小会误伤——有些题只抖动一两轮就自己走出来了，
# 把它们一起掐掉净亏两道题。真正卡死的那种会一直复读（实测 11 次），
# 放宽到 4 一样能兜住。
EMPTY_TURN_LIMIT = 4


def call_signature(call: ToolCall) -> str:
    """工具调用的指纹：名字 + 规范化后的参数。

    参数按 key 排序后再序列化。不排序的话 {"a":1,"b":2} 与 {"b":2,"a":1}
    会算成两次不同的调用——模型只要换个字段顺序就绕过了检测，
    而它换顺序几乎不花任何代价。
    """
    return call.name + " " + json.dumps(
        call.arguments, sort_keys=True, ensure_ascii=False
    )


def _parse_feedback(failure: ParseFailure) -> str:
    """把解析失败翻译成「下一步该做什么」。

    空回合要单独对待：模型不是在写坏格式，而是在表达一件协议里没有的事
    （最常见的是「改动提完了，等用户确认」）。对它说「请只输出规定的 JSON」
    没有任何用——它下一轮会原样再来一次，实测连发十几次。
    """
    if failure.kind == "empty_turn":
        return T.EMPTY_TURN_FEEDBACK
    return f"上一轮输出无法解析：{failure.reason}。请只输出规定的 JSON 对象。"


def _render_lessons(pushed: list[tuple[int, str]]) -> str:
    """把推送的教训渲染成一段。空的返回空串，不留一个空标题。

    标题里点明来源，是为了让模型知道这是别人踩过的坑、不是当前项目的要求；
    否则它可能把教训当成任务约束照搬。
    """
    if not pushed:
        return ""
    lines = ["以下是过去类似任务里踩过的坑，做之前先看一眼："]
    lines.extend(f"- {text}" for _, text in pushed)
    return "\n".join(lines)


def build_workflow(registry: ToolRegistry, read_roots: tuple = ()) -> str:
    """按实际注册的工具生成工作方式说明。

    只写「什么时候用哪个」，不写工具内部怎么实现——模型不需要知道索引是树
    还是图，那是纯浪费。

    关键在于每一句都从注册表推导。提到一个没注册的工具，就是给模型一条它
    做不到的指令，比不写更糟：它会浪费步数去尝试。审查者没有写权限，
    所以它看到的说明里就不该出现写工具。
    """
    lines = ["工作方式："]

    lookup = [name for name in LOOKUP_TOOLS if registry.get(name) is not None]
    edits = [name for name in EDIT_TOOLS if registry.get(name) is not None]
    if lookup:
        lines.append(T.WORKFLOW_FIND.format(names=" / ".join(lookup)))
    if registry.get("find_callers") is not None:
        lines.append(T.WORKFLOW_IMPACT)
    if lookup and edits:
        # 必须紧跟在查询规则后面。实测模型会把「怎么查」那一段当成全部
        # 工作方式，从头查到尾、一次改动都不提；这条是它的对面。
        # 只读角色（没有写工具）不得看到这句——那是它做不到的事。
        lines.append(T.WORKFLOW_ACT)

    if edits:
        if "write_file" in edits and "replace_text" in edits:
            lines.append(T.WORKFLOW_EDIT_FULL)
        elif "write_file" in edits:
            # 小项目只给整份重写：这时候再说「精确替换」就是在提一个
            # 不存在的工具，模型会浪费步数去找它。
            lines.append(T.WORKFLOW_EDIT_WHOLE)
        else:
            lines.append(T.WORKFLOW_EDIT_PRECISE)
        if "replace_lines" in edits:
            lines.append(T.WORKFLOW_EDIT_LINES)
        lines.append(T.WORKFLOW_WRITE_SAFE)

    if registry.get("recall") is not None:
        lines.append(T.WORKFLOW_RECALL)

    if registry.get("run_command") is not None:
        lines.append(T.WORKFLOW_RUN)
        lines.append(T.WORKFLOW_TESTS_ARE_SPEC)
        lines.append(T.WORKFLOW_TEST_SCOPE)

    if registry.get("request_diagnosis") is not None:
        # 紧跟在「怎么验证」后面：它处理的正是验证本身出问题的那种情况。
        lines.append(T.WORKFLOW_DIAGNOSIS)

    if read_roots:
        lines.append(
            T.WORKFLOW_READ_ROOTS.format(
                roots="、".join(str(item) for item in read_roots)
            )
        )

    if registry.get("dir_stats") is not None:
        lines.append(T.WORKFLOW_STATS)

    if registry.get("calc") is not None:
        lines.append(T.WORKFLOW_CALC)
        lines.append(T.WORKFLOW_PERMISSION)
        lines.append(T.WORKFLOW_DEPENDENCY)

    if registry.get("dispatch") is not None:
        # 放在最后几条之前：它讲的是「怎么把活干完」的选择，
        # 而不是某类工具的用法，跟前面的查询/改动规则并列。
        lines.append(T.WORKFLOW_DISPATCH)

    lines.append(T.WORKFLOW_NO_GUESS)
    return "\n".join(lines)

MAX_RECENT_TURNS = 6

# 只有这几类工具的结果算「进度」。读文件和搜索不算：它们是手段，
# 不是产出，记进去只会把状态撑满噪声。
PROGRESS_TOOLS = ("write_file", "replace_lines", "run_command")
MAX_DONE_NOTES = 8


@dataclass
class LoopResult:
    """一次运行的结果。"""

    finished: bool
    final: str
    state: TaskState
    steps: int
    resets: int
    trace: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0
    lessons_pushed: tuple[int, ...] = ()
    # 这次运行里确认过「失败是环境造成的」。任务没做成时，它是决定
    # 要不要替 Agent 登记诊断请求的依据之一。
    environment_blocked: bool = False

    def usage(self) -> str:
        """一行用量摘要，用于比较不同配置的实际成本。"""
        return (
            f"步数 {self.steps}，模型调用 {self.model_calls}，"
            f"输入 {self.prompt_tokens} token，输出 {self.completion_tokens} token"
        )


class AgentLoop:
    """单步编排的主循环。"""

    def __init__(
        self,
        gateway: ModelGateway,
        tokenizer: TokenCounter,
        registry: ToolRegistry,
        config: Config,
        prefetch: Callable[[str], str] | None = None,
        memory: Any | None = None,
        distiller: Callable[[TaskState, str], list[tuple[str, str]]] | None = None,
        lessons: Callable[[str], list[tuple[int, str]]] | None = None,
        verify: Callable[[], ToolResult] | None = None,
        incoming: Callable[[], str] | None = None,
        read_roots: tuple = (),
        sources: Any | None = None,
        persona: str = "",
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.tokenizer = tokenizer
        self.registry = registry
        self.config = config
        self.prefetch = prefetch
        self.memory = memory
        self.distiller = distiller
        self.lessons = lessons
        self.verify = verify
        self.incoming = incoming
        self.read_roots = tuple(read_roots)
        # 工具输出台账：数字核对要拿它当「出处」。由循环追加，
        # 模型改不了它——模型自己写进去的出处不算出处。
        self.sources = sources
        self.environment_blocked = False
        self.persona = persona
        self.on_event = on_event
        self._budget = Budget(window=config.context_window)
        self._schema = build_turn_schema(registry)
        # 强制收敛用的收窄 schema：只留写工具。没有写工具（只读角色）
        # 就没有这回事，None 表示这条路不适用。
        edits = [name for name in EDIT_TOOLS if registry.get(name) is not None]
        self._commit_schema = build_turn_schema(registry, only=edits) if edits else None

    def _assemble(
        self,
        state: TaskState,
        history: list[Message],
        feedback: str | None,
        prefetched: str = "",
        hot: str = "",
        lesson_text: str = "",
    ):
        """按当前状态与历史装配本轮上下文。

        预取内容与解析反馈共用 retrieval 预算：它们语义相同，都是外部检索来的内容。
        """
        assembler = Assembler(tokenizer=self.tokenizer, budget=self._budget)
        system_text = SYSTEM_PROMPT.format(
            tools=self.registry.describe(),
            workflow=build_workflow(self.registry, self.read_roots),
        )
        if self.persona:
            system_text = f"{self.persona}\n\n{system_text}"
        sections = [
            Section(name="system", text=system_text, priority=10, mandatory=True),
        ]
        if hot:
            sections.append(Section(name="hot_memory", text=hot, priority=20))
        if lesson_text:
            sections.append(Section(name="lessons", text=lesson_text, priority=25))
        sections.append(Section(name="task_state", text=state.render(), priority=30))
        if prefetched:
            sections.append(Section(name="retrieval", text=prefetched, priority=35))
        if feedback:
            sections.append(Section(name="retrieval", text=feedback, priority=40))
        return assembler.assemble(sections, recent_turns=history[-MAX_RECENT_TURNS:])

    def run(
        self, goal: str, task_id: str = "task", resume: bool = False
    ) -> LoopResult:
        """运行任务直到给出最终答复或达到步数上限。"""
        checkpoint = self.config.task_path(task_id)

        # 恢复未完成的检查点。之前这里只写不读，等于「崩溃可恢复」这句
        # 承诺从未兑现——跑到一半中断，下次只能从零开始。
        state = load_state(checkpoint) if resume else None
        resumed = state is not None
        if state is None:
            state = TaskState(task_id=task_id, goal=goal)
        goal = state.goal

        # 续跑给的是**新增**预算，不是沿用已经耗尽的那份。
        # 否则一个撞过上限的任务永远续不动——检查点里的步数已经等于上限了。
        limit = state.step + self.config.max_steps if resumed else self.config.max_steps
        # 总步数上限：督导的续期从这里扣。基础预算只是**起点**，
        # 一次跑多久由督导按「有没有进展」决定，这个数是它的天花板。
        ceiling = self.config.step_ceiling or self.config.max_steps * STEP_CEILING_FACTOR
        ceiling = max(ceiling, limit)

        history: list[Message] = [
            Message(
                role="user",
                content=(
                    f"继续之前未完成的任务（已进行 {state.step} 步）。"
                    if resumed
                    else goal
                ),
            )
        ]
        feedback: str | None = None
        # 督导让主循环改道时说的话。单独放一个变量，是因为 feedback 会被
        # 强制收敛那几处覆盖掉——而改道的话比通用提醒具体，不能被覆盖。
        redirect: str | None = None
        resets = 0
        trace: list[str] = []
        # 上一轮诊断回来的报告：一开局就摆到面前，模型不用记得去查。
        if self.incoming is not None:
            report = self.incoming()
            if report:
                feedback = report
                trace.append("注入诊断报告")
        prompt_tokens = 0
        completion_tokens = 0
        model_calls = 0
        prefetched = self.prefetch(goal) if self.prefetch is not None else ""
        hot = self.memory.hot_text() if self.memory is not None else ""
        # 「原地打转」检测：连续相同的调用计数。跨步骤累计，
        # 中间只要出现一个不同的调用就清零。
        last_signature = ""
        repeats = 0
        recent: list[str] = []
        no_edit_steps = 0
        empty_turns = 0
        overflow_retried = False
        # 督导连续问几次就该退避：同一个打转状态每步问一遍，问出来的话是一样的，
        # 只是把成本翻倍。所以每次介入之后把门槛翻倍（4 → 8 → 16），
        # 而不是设一个「隔几步问一次」的定时器。
        intervene_at = REPEAT_INTERVENE_AT
        roam_intervene_at = NO_EDIT_INTERVENE_AT
        edits_made = 0
        tool_calls_made = 0
        stopped_by = ""

        # 主动推送：进入任务时就把它相关的历史教训放到模型面前，
        # 而不是等它自己去检索——它不会去检索，因为它不知道自己缺什么。
        pushed: list[tuple[int, str]] = (
            list(self.lessons(goal)) if self.lessons is not None else []
        )
        lesson_text = _render_lessons(pushed)
        if pushed:
            # 让推送可见：教训推了没推、推了哪几条，是调这块时唯一能看到的信号。
            trace.append(f"推送 {len(pushed)} 条历史教训：" + "；".join(
                text[:30] for _, text in pushed
            ))

        while True:
            if state.step >= limit and not self.config.supervise:
                # 关掉督导就退回老行为：撞上上限即停。这条路仍然要留着——
                # 它是一个可用的对照，也是督导自己出问题时的最终退路。
                break
            stuck_on_repeat = self.config.supervise and repeats >= intervene_at
            # 换着花样绕：每步都换了查询，重复计数永远归零，只有「一直没产出」
            # 这个信号还在涨。
            stuck_on_roam = (
                self.config.supervise and no_edit_steps >= roam_intervene_at
            )
            if state.step >= limit or stuck_on_repeat or stuck_on_roam:
                # 两个触发点，同一件事：机械层已经说了有事，而模型自己没纠正。
                # 这是**要判断**的点，不是失败点——原先的处理是停下来提示用户
                # `--resume`，那等于把判断成本推给用户，还把一件事拆成两次对话。
                exhausted = state.step >= limit
                if exhausted:
                    asking = "这一轮的步数预算用完了，任务还没结束"
                elif stuck_on_roam:
                    asking = f"它已经连续 {no_edit_steps} 步只看不写，没有任何产出"
                else:
                    asking = (
                        f"它已经连续 {repeats} 次调用同一个工具（参数也一样），还在重复"
                    )
                # 把「为什么把它叫起来」记下来：调这块时唯一能看的东西就是它。
                trace.append(f"step{state.step}: 叫督导——{asking}")
                verdict = self._ask_supervisor(
                    state,
                    trace,
                    ceiling=ceiling,
                    limit=limit,
                    edits=edits_made,
                    calls=tool_calls_made,
                    resets=resets,
                    repeats=repeats,
                    no_edit=no_edit_steps,
                    tokens=prompt_tokens + completion_tokens,
                    asking=asking,
                )
                if verdict is not None:
                    prompt_tokens += verdict.prompt_tokens
                    completion_tokens += verdict.completion_tokens
                    model_calls += 1
                if verdict is None:
                    # 拿不到结论时按**不敢续期**处理：含糊的续期比不续期危险——
                    # 不续期只是停下来，错续期会把预算继续投进一个卡住的任务。
                    # 打转那条不打断任务，只退避：它还没到花光预算的地步。
                    intervene_at = max(repeats * 2, intervene_at * 2)
                    roam_intervene_at = max(
                        no_edit_steps * 2, roam_intervene_at * 2
                    )
                    if exhausted:
                        stopped_by = "督导没能给出结论（调用失败或结论不合格式）"
                        trace.append(
                            f"step{state.step}: 督导没给结论，按预算用尽收尾"
                        )
                        break
                elif verdict.action == STOP:
                    # 督导说停就直接停——继续烧到上限并不比它诚实。
                    stopped_by = verdict.reason or "督导判断这个任务做不下去"
                    trace.append(f"step{state.step}: 督导建议收手——{stopped_by}")
                    break
                else:
                    if verdict.grants:
                        limit = state.step + verdict.grants
                    if verdict.message:
                        redirect = verdict.message
                    what = (
                        f"续 {verdict.grants} 步"
                        if verdict.action == EXTEND
                        else f"改道（续 {verdict.grants} 步）"
                    )
                    trace.append(
                        f"step{state.step}: 督导{what}（预算 {limit}）——{verdict.reason}"
                    )
                    if verdict.message:
                        trace.append(f"step{state.step}: 发给它的话：{verdict.message}")
                    if not exhausted:
                        # 同一个打转状态每步问一遍，问出来的话是一样的，
                        # 只是把成本翻倍。门槛翻倍，别设定时器。
                        intervene_at = max(repeats * 2, intervene_at * 2)
                        roam_intervene_at = max(
                            no_edit_steps * 2, roam_intervene_at * 2
                        )
            # 强制收敛：连续若干步只查看不修改时，把「继续查」这个选项从
            # 语法里拿掉。实测这个模型对文字提醒完全免疫（重复提醒、拦截
            # 说明都照发不误），能推得动它的只有「这一轮物理上只能选什么」。
            forcing = (
                self._commit_schema is not None
                and no_edit_steps >= NO_EDIT_LIMIT
            )
            # 只读角色没有写工具，收窄无从谈起——对它来说唯一的「推进」
            # 就是给结论。没有这股压力，它会一路翻文件翻到步数上限。
            concluding = (
                self._commit_schema is None and no_edit_steps >= NO_EDIT_LIMIT
            )
            if forcing:
                feedback = T.FORCE_COMMIT
            elif concluding:
                feedback = T.FORCE_CONCLUDE
            # 督导的话排在最后：它比通用提醒具体，不该被顶掉。只发一次——
            # 一直挂着会让每一轮的装配都多一段重复内容。
            if redirect is not None:
                feedback = redirect
                redirect = None
            assembled = self._assemble(
                state, history, feedback, prefetched, hot, lesson_text
            )
            self._emit("step", {"n": state.step})

            # 预算守卫：软触发整理，硬触发重置。依据需求体积而非装入量。
            if assembled.demand_tokens >= self._budget.hard_limit():
                history = [Message(role="user", content=goal)]
                feedback = None
                resets += 1
                trace.append(f"step{state.step}: 上下文重置（第 {resets} 次）")
                assembled = self._assemble(
                    state, history, feedback, prefetched, hot, lesson_text
                )
            elif assembled.demand_tokens >= self._budget.soft_limit():
                history = history[-(MAX_RECENT_TURNS // 2):]
                trace.append(f"step{state.step}: 上下文整理")

            try:
                response = chat_with_escalation(
                    self.gateway,
                    ChatRequest(
                        messages=assembled.messages,
                        max_tokens=self._budget.output_reserve(),
                        response_schema=(
                            self._commit_schema if forcing else self._schema
                        ),
                    ),
                )
            except ContextOverflowError:
                # 预算用的是**估算**分词器，服务端用的是真实分词；两者偏差大时
                # （中文/代码混排尤其明显）本地判定会放行、服务端拒绝。
                # 这不能让它把整个运行打断——丢掉历史重发一次，只重试一次。
                if overflow_retried:
                    raise
                overflow_retried = True
                history = [Message(role="user", content=goal)]
                feedback = None
                resets += 1
                trace.append(
                    f"step{state.step}: 服务端说提示词超长，丢掉历史重发（第 {resets} 次）"
                )
                continue
            model_calls += 1
            prompt_tokens += response.prompt_tokens
            completion_tokens += response.completion_tokens
            turn = parse_turn(response.text)

            if isinstance(turn, ParseFailure):
                if turn.kind == "empty_turn":
                    empty_turns += 1
                    if empty_turns >= EMPTY_TURN_LIMIT:
                        # 它不是在想，是卡住了。继续复读只会把预算烧光，
                        # 而已提出的改动是真实产出——交给用户判断。
                        trace.append(
                            f"step{state.step}: 连续 {empty_turns} 轮空回合，"
                            "模型卡死，提前收尾"
                        )
                        self._archive(state, "fail", trace, "")
                        return LoopResult(
                            False,
                            T.EMPTY_TURN_FINAL,
                            state,
                            state.step,
                            resets,
                            trace,
                            prompt_tokens,
                            completion_tokens,
                            model_calls,
                            tuple(item[0] for item in pushed),
                        )
                else:
                    empty_turns = 0
                if turn.kind == "truncated":
                    # 截断说明「这个任务的输出形态就是偏大」。与其每一轮都撞一次
                    # 再翻倍重试，不如把这个事实记在这次任务里（封顶见 Budget）。
                    before = self._budget.output_reserve()
                    after = self._budget.boost_output()
                    if after > before:
                        trace.append(
                            f"step{state.step}: 输出被截断，本次任务的输出预算 "
                            f"{before} → {after} token"
                        )
                    # 光抬高预算不够：它得知道「这一轮要少说点」，
                    # 否则重来的还是同样大的一份。
                    feedback = T.OUTPUT_TRUNCATED.format(limit=after)
                else:
                    feedback = _parse_feedback(turn)
                history.append(Message(role="assistant", content=response.text))
                history.append(Message(role="user", content=feedback))
                state.step_forward()
                # 解析失败也要落盘：步数确实推进了，不写的话这类失败
                # 会连一个可续跑的检查点都不留下。
                save_state(state, checkpoint)
                snippet = response.text.strip().replace("\n", " ")[:160]
                trace.append(f"step{state.step}: 解析失败 - {turn.reason} | 原始: {snippet}")
                continue

            if turn.state_delta is not None:
                state.apply(turn.state_delta)
            history.append(Message(role="assistant", content=response.text))

            if turn.tool_calls:
                outputs = []
                edited = False
                for call in turn.tool_calls:
                    signature = call_signature(call)
                    repeats = repeats + 1 if signature == last_signature else 1
                    last_signature = signature
                    recent.append(signature)
                    del recent[:-RECENT_WINDOW]
                    # 两条判据各算各的：连续相同按连续次数算，交替打转按出现频率算。
                    # 分开是因为文案要说实话——「和上一步完全相同」在交替打转时
                    # 是假的，上一步明明是别的调用。
                    seen = recent.count(signature)
                    level = max(repeats, seen)
                    result = self._invoke_guarded(call, repeats, seen)
                    tool_calls_made += 1
                    if result.usage is not None:
                        # 工具自己花的模型开销（派发那一趟）也要记账：
                        # 不记的话用量表报的是「主循环自己花了多少」，
                        # 而人会把它读成「这次任务花了多少」。
                        prompt_tokens += result.usage.prompt_tokens
                        completion_tokens += result.usage.completion_tokens
                        model_calls += result.usage.calls
                    if self.sources is not None:
                        self.sources.add(result.content, result.facts)
                    self._note_progress(state, call, result)
                    if result.ok and call.name in EDIT_TOOLS:
                        edited = True
                        edits_made += 1
                    if level >= REPEAT_WARN_AT:
                        trace.append(
                            f"step{state.step}: 重复调用第 {level} 次：{call.name}"
                        )
                    repeats = level
                    self._emit(
                        "tool",
                        {
                            "name": call.name,
                            "ok": result.ok,
                            "detail": " ".join(result.content.split())[:200],
                        },
                    )
                    status = "成功" if result.ok else "失败"
                    outputs.append(f"[{call.name}] {status}: {result.content}")
                    # 失败时把输出压成一行摘要。取第一行不行：那里是命令本身，
                    # 而不是错误原因——调试时会被误导。
                    # 失败信息给足长度：160 字刚好够看到命令本身，
                    # 而真正的原因在后面——调试时会被自己的截断挡住。
                    flat = " ".join(result.content.split())
                    detail = "" if result.ok else f": {flat[:400]}"
                    trace.append(f"step{state.step}: 工具 {call.name} -> {status}{detail}")
                history.append(Message(role="tool", content="\n".join(outputs)))

                # 改完就替它验一次。模型不会主动去跑测试——实测 8 条失败里
                # 一条 run_command 都没有——所以这件事由循环来做，把结果
                # 当场顶回去，它才有机会发现自己改错了。
                if edited and self.verify is not None:
                    report, passed = self._run_verification()
                    trace.append(
                        f"step{state.step}: 自动验证 -> {'通过' if passed else '失败'}"
                    )
                    self._emit(
                        "tool",
                        {
                            "name": "自动验证",
                            "ok": passed,
                            "detail": " ".join(report.split())[:200],
                        },
                    )
                    # 必须紧跟在那次改动后面，不能塞进 system 区段。
                    # 区段排在整段历史之前，模型会先读到「测试失败」、
                    # 再读到自己刚才做的事——因果顺序反了，它接不上。
                    history.append(Message(role="user", content=report))

            if any(call.name in EDIT_TOOLS for call in turn.tool_calls):
                no_edit_steps = 0
                # 产出过一次，漫游就算结束了。门槛跟着复位，否则下一段漫游
                # 要等到翻倍后的那个数字才会被看见。
                roam_intervene_at = NO_EDIT_INTERVENE_AT
            elif turn.tool_calls:
                no_edit_steps += 1
                # 只读角色没有写工具，收窄无从谈起——日志说「收窄为只能写」
                # 而实际什么都没发生，那是最难查的一类假日志。
                if no_edit_steps == NO_EDIT_LIMIT and self._commit_schema is not None:
                    trace.append(
                        f"step{state.step}: 连续 {NO_EDIT_LIMIT} 步没有提出改动，"
                        "下一轮收窄为只能写"
                    )

            state.step_forward()
            save_state(state, checkpoint)

            if turn.done:
                trace.append(f"step{state.step}: 完成")
                self._archive(state, "success", trace, turn.final or "")
                clear_state(checkpoint)
                return LoopResult(
                    True,
                    turn.final or "",
                    state,
                    state.step,
                    resets,
                    trace,
                    prompt_tokens,
                    completion_tokens,
                    model_calls,
                    tuple(item[0] for item in pushed),
                    environment_blocked=self.environment_blocked,
                )

        self._archive(state, "fail", trace, "")
        final = "已达步数上限，任务未完成"
        if stopped_by:
            # 收尾文案必须说清**为什么**停：是钱花完了，还是督导判断做不下去。
            # 只说「未完成」的话，用户能做的只有原样再来一次。
            final = f"任务没做完，收手的原因：{stopped_by}"
        return LoopResult(
            False,
            final,
            state,
            state.step,
            resets,
            trace,
            prompt_tokens,
            completion_tokens,
            model_calls,
            tuple(item[0] for item in pushed),
            environment_blocked=self.environment_blocked,
        )

    def _ask_supervisor(
        self,
        state: TaskState,
        trace: list[str],
        *,
        ceiling: int,
        limit: int,
        edits: int,
        calls: int,
        resets: int,
        repeats: int,
        no_edit: int,
        tokens: int,
        asking: str,
    ) -> Verdict | None:
        """问一次督导。关着、或者它没能给出结论，都返回 None。

        证据里刻意不含对话历史：要判断的是「这些动作有没有进展」，
        读到主循环的推理只会让它附和那个推理——和审查者必须独立同一个理由。
        它看到的是事实（记录下来的状态、发生过什么、机械统计）。

        结论以事件发出去。网页里看得到「督导说了什么」这件事很重要：
        否则一次续期在界面上和「它自己一直在跑」完全分不清。
        """
        if not self.config.supervise:
            return None
        evidence = Evidence(
            goal=state.goal,
            step=state.step,
            limit=limit,
            ceiling=ceiling,
            state=state.render(),
            trace=tuple(trace),
            edits=edits,
            calls=calls,
            resets=resets,
            repeats=repeats,
            no_edit=no_edit,
            tokens=tokens,
            asking=asking,
        )
        verdict = supervise(self.gateway, evidence)
        if verdict is None:
            self._emit("note", {"text": "督导：没能给出结论", "ok": False})
            return None
        what = {
            "extend": f"续 {verdict.grants} 步",
            "redirect": f"改道（续 {verdict.grants} 步）",
            "stop": "建议收手",
        }.get(verdict.action, verdict.action)
        self._emit("note", {"text": f"督导：{what}——{verdict.reason}", "ok": True})
        return verdict

    def _invoke_guarded(
        self, call: ToolCall, repeats: int, seen: int = 1
    ) -> ToolResult:
        """执行一次工具调用，并对「原地打转」作出反应。

        小模型在短上下文里失去方向时会反复做同一个动作，而且不会自己停：
        实测本地 7B 在回归集里连续 10 次调用同一个 find_callers、参数一字
        不差。它不是在试探，是卡住了——这时候只能由循环把它顶回去，
        等模型自己醒悟是不现实的。

        repeats 是「连续相同的次数」，seen 是「最近几次窗口里出现的次数」。
        后者管交替打转（A、B、A、B…），前者管原地复读。
        """
        level = max(repeats, seen)
        if level >= REPEAT_BLOCK_AT:
            return ToolResult(
                ok=False,
                content=(
                    f"这个调用在最近几步里已经做过 {level - 1} 次，结果不会改变，"
                    "因此没有执行。请换一种做法：换一个工具、换一组参数，"
                    "或者先去看别的地方；如果任务其实已经完成，直接给出结论即可。"
                ),
            )
        result = self.registry.invoke(call)
        if level >= REPEAT_WARN_AT:
            if repeats >= REPEAT_WARN_AT:
                note = "注意：这一步和上一步完全相同，你已经做过一次，结果也一样。"
            else:
                note = "注意：这个调用你在最近几步里已经做过了，结果不会变。"
            return ToolResult(
                ok=result.ok,
                content=(note + "如果它没有帮你推进，就换一种做法。\n" + result.content),
            )
        return result

    def _run_verification(self) -> tuple[str, bool]:
        """替模型跑一遍项目测试，把结果整理成它看得懂的一段话。

        验证本身出问题（命令跑不起来、超时）不算任务失败，只作为一条
        诚实的反馈告诉模型——把工具故障说成「你的代码错了」，
        会把它引到完全错误的方向上去。
        """
        try:
            result = self.verify()
        except Exception as exc:
            return (f"自动验证没能跑起来（{type(exc).__name__}）：{exc}", False)
        body = " ".join(result.content.split())[:400]
        if result.content.startswith(ENVIRONMENT_MARKER):
            # 记下来：任务没做成时，这决定要不要替它登记诊断请求。
            self.environment_blocked = True
        if result.ok:
            return (
                f"系统自动跑了一遍项目里的测试：**通过**（{body}）。"
                "如果没有别的要改，直接给出结论收尾。",
                True,
            )
        # 失败到底是什么性质，由验证器说——它才知道退出码的含义。
        # 循环这边再加一句「测试失败」会盖掉那个区别。
        return (f"自动验证的结果：{body}", False)

    @staticmethod
    def _note_progress(state: TaskState, call, result) -> None:
        """把「确实做了的事」记进状态，而不是等模型自己填。

        之前 done 一直空着——模型只填 current，从不记进度。指望它每次都
        不忘是不现实的，而**实际发生了什么，系统自己看得见**。
        所以由循环记录事实，模型只管判断（current / hypothesis / 已排除）。

        只记有产出的动作：读文件和搜索是手段而非结果，记进去只会把
        每轮都要注入的状态撑满噪声。
        """
        if call.name not in PROGRESS_TOOLS or not result.ok:
            return
        target = call.arguments.get("path") or " ".join(
            call.arguments.get("command", [])[:2]
        )
        entry = f"{call.name} {target}".strip()
        if entry not in state.done:
            state.done.append(entry)
        # 状态每轮都要注入，必须封顶；留最近的，早的先让位。
        del state.done[:-MAX_DONE_NOTES]

    def _emit(self, kind: str, data: dict) -> None:
        """向外部观察者发一条事件。

        回调异常一律吞掉：它是壳挂上来的，壳的问题不该毁掉内核的工作。
        """
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception:
            pass

    def _archive(
        self, state: TaskState, outcome: str, trace: list[str], final: str
    ) -> None:
        """任务收尾时归档。

        归纳失败不能让用户拿不到结果——任务产出是主要价值，记忆是次要收益，
        因此这里刻意吞掉归纳异常，只保留确定性的归档动作。
        """
        if self.memory is None:
            return
        promote: list[tuple[str, str]] = []
        if self.distiller is not None:
            try:
                produced = self.distiller(state, final)
                if hasattr(produced, "entries"):
                    promote = list(produced.entries)
                    trace.append(
                        f"归纳: {produced.rounds} 轮，分治={produced.split}，"
                        f"截断={produced.truncated}，原始输出={produced.raw[:120]!r}"
                    )
                else:
                    promote = list(produced)
            except Exception as exc:
                promote = []
                trace.append(f"归纳失败（已忽略）: {type(exc).__name__}: {exc}")
        result = self.memory.archive(state, outcome=outcome, promote=promote)
        trace.append(
            f"归档: 事件#{result.episode_id}，提升 {len(result.promoted)} 条，"
            f"下沉 {len(result.demoted)} 条"
        )

