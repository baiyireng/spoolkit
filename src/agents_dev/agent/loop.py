"""Agent 主循环。

设计要点：任务状态每轮注入，对话历史只保留最近若干轮，
因此上下文可以被安全地重置——状态不丢，历史可弃。

预算守卫依据 demand_tokens（裁剪前的需求）而非实际装入量来判断：
装配器最多只能装到有效预算，用实际装入量判断触发线永远不会触发。
"""

from dataclasses import dataclass, field

from agents_dev.agent.protocol import ParseFailure, parse_turn
from agents_dev.agent.state import TaskState, save_state
from agents_dev.config import Config
from agents_dev.context.assembler import Assembler
from agents_dev.context.budget import Budget
from agents_dev.context.sections import Section
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.llm.types import ChatRequest, Message
from agents_dev.tools.registry import ToolRegistry

SYSTEM_PROMPT = """你是本地运行的编程助手。每轮只做一件事。
必须输出一个 JSON 对象，字段为 thought、tool_calls、state、final。
不要输出 JSON 以外的任何内容。
可用工具：
{tools}"""

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


class AgentLoop:
    """单步编排的主循环。"""

    def __init__(
        self,
        gateway: ModelGateway,
        tokenizer: TokenCounter,
        registry: ToolRegistry,
        config: Config,
    ) -> None:
        self.gateway = gateway
        self.tokenizer = tokenizer
        self.registry = registry
        self.config = config
        self._budget = Budget(window=config.context_window)

    def _assemble(self, state: TaskState, history: list[Message], feedback: str | None):
        """按当前状态与历史装配本轮上下文。"""
        assembler = Assembler(tokenizer=self.tokenizer, budget=self._budget)
        system_text = SYSTEM_PROMPT.format(tools=self.registry.describe())
        sections = [
            Section(name="system", text=system_text, priority=10, mandatory=True),
            Section(name="task_state", text=state.render(), priority=30),
        ]
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

        while state.step < self.config.max_steps:
            assembled = self._assemble(state, history, feedback)

            # 预算守卫：软触发整理，硬触发重置。依据需求体积而非装入量。
            if assembled.demand_tokens >= self._budget.hard_limit():
                history = [Message(role="user", content=goal)]
                feedback = None
                resets += 1
                trace.append(f"step{state.step}: 上下文重置（第 {resets} 次）")
                assembled = self._assemble(state, history, feedback)
            elif assembled.demand_tokens >= self._budget.soft_limit():
                history = history[-(MAX_RECENT_TURNS // 2):]
                trace.append(f"step{state.step}: 上下文整理")

            response = self.gateway.chat(
                ChatRequest(
                    messages=assembled.messages,
                    max_tokens=self._budget.output_reserve(),
                )
            )
            turn = parse_turn(response.text)

            if isinstance(turn, ParseFailure):
                feedback = f"上一轮输出无法解析：{turn.reason}。请只输出规定的 JSON 对象。"
                history.append(Message(role="assistant", content=response.text))
                history.append(Message(role="user", content=feedback))
                state.step_forward()
                trace.append(f"step{state.step}: 解析失败 - {turn.reason}")
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
                    trace.append(f"step{state.step}: 工具 {call.name} -> {status}")
                history.append(Message(role="tool", content="\n".join(outputs)))

            state.step_forward()
            save_state(state, self.config.task_path(task_id))

            if turn.final is not None:
                trace.append(f"step{state.step}: 完成")
                return LoopResult(True, turn.final, state, state.step, resets, trace)

        return LoopResult(
            False, "已达步数上限，任务未完成", state, state.step, resets, trace
        )

