"""Agent 主循环。

设计要点：任务状态每轮注入，对话历史只保留最近若干轮，
因此上下文可以被安全地重置——状态不丢，历史可弃。

预算守卫依据 demand_tokens（裁剪前的需求）而非实际装入量来判断：
装配器最多只能装到有效预算，用实际装入量判断触发线永远不会触发。
"""

from dataclasses import dataclass, field
from typing import Any, Callable

from agents_dev.agent.protocol import ParseFailure, build_turn_schema, parse_turn
from agents_dev.agent.state import TaskState, save_state
from agents_dev.config import Config
from agents_dev.context.assembler import Assembler
from agents_dev.context.budget import Budget
from agents_dev.context.sections import Section
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.retry import chat_with_escalation
from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.llm.types import ChatRequest, Message
from agents_dev.tools.registry import ToolRegistry

SYSTEM_PROMPT = """你是本地运行的编程助手。每轮只做一件事。
必须只输出一个 JSON 对象，不要有任何其他文字。
格式：
{{"thought":"这一步的打算","tool_calls":[{{"name":"工具名","arguments":{{"参数名":值}}}}],"state":{{"current":"当前在做什么"}},"done":false,"final":null}}
还要工具就调用工具，此时 done 必须是 false。
已经有答案要交付时，tool_calls 设为 []，done 设为 true，final 设为给用户的完整答复。
可用工具：
{tools}

{workflow}"""

LOOKUP_TOOLS = ("find_symbol", "file_symbols", "find_callers")
EDIT_TOOLS = ("replace_lines", "write_file")


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
        lines.append(
            "- 查符号优先用 " + " / ".join(lookup) + "，不要整份读文件；索引已经建好。"
        )
    if registry.get("find_callers") is not None:
        lines.append("- 改代码前先用 find_callers 看波及面，避免改坏调用方。")

    edits = [name for name in EDIT_TOOLS if registry.get(name) is not None]
    if edits:
        if set(edits) == set(EDIT_TOOLS):
            lines.append(
                "- 改动优先用 replace_lines 精确替换；write_file 只用于新文件或整份重写。"
            )
        else:
            lines.append("- 改动使用 " + "、".join(edits) + " 精确替换。")
        lines.append("- 写操作只生成 diff 并需用户确认，不必回避提出改动。")

    if registry.get("recall") is not None:
        lines.append("- 要回忆过去的结论、决策或失败教训时，用 recall 查历史记忆。")

    lines.append("- 信息不足先查，不要猜；确实找不到就直说找不到。")
    return "\n".join(lines)

MAX_RECENT_TURNS = 6


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
        persona: str = "",
    ) -> None:
        self.gateway = gateway
        self.tokenizer = tokenizer
        self.registry = registry
        self.config = config
        self.prefetch = prefetch
        self.memory = memory
        self.distiller = distiller
        self.persona = persona
        self._budget = Budget(window=config.context_window)
        self._schema = build_turn_schema(registry)

    def _assemble(
        self,
        state: TaskState,
        history: list[Message],
        feedback: str | None,
        prefetched: str = "",
        hot: str = "",
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
        sections.append(Section(name="task_state", text=state.render(), priority=30))
        if prefetched:
            sections.append(Section(name="retrieval", text=prefetched, priority=35))
        if feedback:
            sections.append(Section(name="retrieval", text=feedback, priority=40))
        return assembler.assemble(sections, recent_turns=history[-MAX_RECENT_TURNS:])

    def run(self, goal: str, task_id: str = "task") -> LoopResult:
        """运行任务直到给出最终答复或达到步数上限。"""
        state = TaskState(task_id=task_id, goal=goal)
        history: list[Message] = [Message(role="user", content=goal)]
        feedback: str | None = None
        resets = 0
        trace: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        model_calls = 0
        prefetched = self.prefetch(goal) if self.prefetch is not None else ""
        hot = self.memory.hot_text() if self.memory is not None else ""

        while state.step < self.config.max_steps:
            assembled = self._assemble(state, history, feedback, prefetched, hot)

            # 预算守卫：软触发整理，硬触发重置。依据需求体积而非装入量。
            if assembled.demand_tokens >= self._budget.hard_limit():
                history = [Message(role="user", content=goal)]
                feedback = None
                resets += 1
                trace.append(f"step{state.step}: 上下文重置（第 {resets} 次）")
                assembled = self._assemble(state, history, feedback, prefetched, hot)
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
                snippet = response.text.strip().replace("\n", " ")[:160]
                trace.append(f"step{state.step}: 解析失败 - {turn.reason} | 原始: {snippet}")
                continue

            if turn.state_delta is not None:
                state.apply(turn.state_delta)
            history.append(Message(role="assistant", content=response.text))

            if turn.tool_calls:
                outputs = []
                for call in turn.tool_calls:
                    result = self.registry.invoke(call)
                    status = "成功" if result.ok else "失败"
                    outputs.append(f"[{call.name}] {status}: {result.content}")
                    detail = "" if result.ok else f": {result.content.splitlines()[0]}"
                    trace.append(f"step{state.step}: 工具 {call.name} -> {status}{detail}")
                history.append(Message(role="tool", content="\n".join(outputs)))

            state.step_forward()
            save_state(state, self.config.task_path(task_id))

            if turn.done:
                trace.append(f"step{state.step}: 完成")
                self._archive(state, "success", trace, turn.final or "")
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
        )

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

