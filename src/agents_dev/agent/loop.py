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

SYSTEM_PROMPT = T.SYSTEM

LOOKUP_TOOLS = ("find_symbol", "file_symbols", "find_callers")
EDIT_TOOLS = ("replace_lines", "write_file")

# 同一个调用连续重复到第几次时加提醒（仍然执行），到第几次时不再执行。
#
# 阈值来自实测，不是拍的：本地 Qwen2.5-Coder-7B 跑回归集时，连续 10 次
# 调用同一个 find_callers（参数一字不差），把 12 步预算全烧光。
# 第二次先给提醒、不阻断——调用有时确实有意义（比如文件刚被改过）；
# 第三次中间没有任何别的动作，结果不可能变，再执行只是在消耗步数。
REPEAT_WARN_AT = 2
REPEAT_BLOCK_AT = 3


def call_signature(call: ToolCall) -> str:
    """工具调用的指纹：名字 + 规范化后的参数。

    参数按 key 排序后再序列化。不排序的话 {"a":1,"b":2} 与 {"b":2,"a":1}
    会算成两次不同的调用——模型只要换个字段顺序就绕过了检测，
    而它换顺序几乎不花任何代价。
    """
    return call.name + " " + json.dumps(
        call.arguments, sort_keys=True, ensure_ascii=False
    )


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


def build_workflow(registry: ToolRegistry) -> str:
    """按实际注册的工具生成工作方式说明。

    只写「什么时候用哪个」，不写工具内部怎么实现——模型不需要知道索引是树
    还是图，那是纯浪费。

    关键在于每一句都从注册表推导。提到一个没注册的工具，就是给模型一条它
    做不到的指令，比不写更糟：它会浪费步数去尝试。审查者没有写权限，
    所以它看到的说明里就不该出现写工具。
    """
    lines = ["工作方式："]

    lookup = [name for name in LOOKUP_TOOLS if registry.get(name) is not None]
    if lookup:
        lines.append(T.WORKFLOW_FIND.format(names=" / ".join(lookup)))
    if registry.get("find_callers") is not None:
        lines.append(T.WORKFLOW_IMPACT)

    edits = [name for name in EDIT_TOOLS if registry.get(name) is not None]
    if edits:
        if set(edits) == set(EDIT_TOOLS):
            lines.append(T.WORKFLOW_EDIT_FULL)
        else:
            lines.append(T.WORKFLOW_EDIT_PARTIAL.format(names="、".join(edits)))
        lines.append(T.WORKFLOW_WRITE_SAFE)

    if registry.get("recall") is not None:
        lines.append(T.WORKFLOW_RECALL)

    if registry.get("run_command") is not None:
        lines.append(T.WORKFLOW_RUN)
        lines.append(T.WORKFLOW_TEST_SCOPE)
        lines.append(T.WORKFLOW_PERMISSION)
        lines.append(T.WORKFLOW_DEPENDENCY)

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
        self.persona = persona
        self.on_event = on_event
        self._budget = Budget(window=config.context_window)
        self._schema = build_turn_schema(registry)

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
            tools=self.registry.describe(), workflow=build_workflow(self.registry)
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
        resets = 0
        trace: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        model_calls = 0
        prefetched = self.prefetch(goal) if self.prefetch is not None else ""
        hot = self.memory.hot_text() if self.memory is not None else ""
        # 「原地打转」检测：连续相同的调用计数。跨步骤累计，
        # 中间只要出现一个不同的调用就清零。
        last_signature = ""
        repeats = 0

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

        while state.step < limit:
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

            response = chat_with_escalation(
                self.gateway,
                ChatRequest(
                    messages=assembled.messages,
                    max_tokens=self._budget.output_reserve(),
                    response_schema=self._schema,
                ),
            )
            model_calls += 1
            prompt_tokens += response.prompt_tokens
            completion_tokens += response.completion_tokens
            turn = parse_turn(response.text)

            if isinstance(turn, ParseFailure):
                feedback = f"上一轮输出无法解析：{turn.reason}。请只输出规定的 JSON 对象。"
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
                for call in turn.tool_calls:
                    signature = call_signature(call)
                    repeats = repeats + 1 if signature == last_signature else 1
                    last_signature = signature
                    result = self._invoke_guarded(call, repeats)
                    self._note_progress(state, call, result)
                    if repeats >= REPEAT_WARN_AT:
                        trace.append(
                            f"step{state.step}: 重复调用第 {repeats} 次：{call.name}"
                        )
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
                )

        self._archive(state, "fail", trace, "")
        return LoopResult(
            False,
            "已达步数上限，任务未完成",
            state,
            state.step,
            resets,
            trace,
            prompt_tokens,
            completion_tokens,
            model_calls,
            tuple(item[0] for item in pushed),
        )

    def _invoke_guarded(self, call: ToolCall, repeats: int) -> ToolResult:
        """执行一次工具调用，并对「原地打转」作出反应。

        小模型在短上下文里失去方向时会反复做同一个动作，而且不会自己停：
        实测本地 7B 在回归集里连续 10 次调用同一个 find_callers、参数一字
        不差。它不是在试探，是卡住了——这时候只能由循环把它顶回去，
        等模型自己醒悟是不现实的。
        """
        if repeats >= REPEAT_BLOCK_AT:
            return ToolResult(
                ok=False,
                content=(
                    f"这一步与前面 {repeats - 1} 次完全相同，结果不会改变，"
                    "因此没有执行。请换一种做法：换一个工具、换一组参数，"
                    "或者先去看别的地方；如果任务其实已经完成，直接给出结论即可。"
                ),
            )
        result = self.registry.invoke(call)
        if repeats >= REPEAT_WARN_AT:
            return ToolResult(
                ok=result.ok,
                content=(
                    "注意：这一步和上一步完全相同，你已经做过一次，结果也一样。"
                    "如果它没有帮你推进，就换一种做法。\n" + result.content
                ),
            )
        return result

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

