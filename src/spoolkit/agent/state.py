"""任务状态与检查点。

任务状态是「我做到哪了」的结构化表达，每轮注入上下文，
从而让对话历史变成可以随时丢弃的东西。这是短上下文设计的支点。
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from spoolkit import limits as _limits
from spoolkit.llm.tokenizer import TokenCounter


@dataclass
class StateDelta:
    """模型对任务状态提出的增量修改。未提供的字段保持不变。"""

    done_added: list[str] = field(default_factory=list)
    current: str | None = None
    verify: str | None = None
    excluded_added: list[str] = field(default_factory=list)
    hypothesis: str | None = None


@dataclass
class TaskState:
    """一次任务的状态。"""

    task_id: str
    goal: str
    done: list[str] = field(default_factory=list)
    current: str = ""
    verify: str = ""
    excluded: list[str] = field(default_factory=list)
    hypothesis: str = ""
    step: int = 0

    # 状态块里「已完成」只列最近几条：它**每轮都要注入一次**，每一条都是
    # 每个请求的税——和工具清单是同一笔账。完整记录留在状态里（也就落在
    # 检查点文件上），需要细节时去读 `.agent/progress.md`。
    #
    # 默认值来自登记表（limits.KNOBS 的 done_inline），这里留名字给外部导入；
    # 实际列几条由渲染方按本次运行的覆盖决定。
    DONE_INLINE = int(_limits.knob("done_inline").default)

    def render(self, done_inline: int | None = None) -> str:
        """渲染成紧凑文本供注入。刻意省略空字段以节省 token。

        done_inline 由调用方按本次运行的标定值给（默认用登记表的默认值）。
        """
        limit = self.DONE_INLINE if done_inline is None else max(1, done_inline)
        lines = [f"目标: {self.goal}"]
        if self.done:
            recent = self.done[-limit:]
            head = f"已完成 {len(self.done)} 项"
            if len(self.done) > len(recent):
                head += f"（只列最近 {len(recent)} 项）"
            lines.append(f"{head}: " + " | ".join(recent))
            if len(self.done) > len(recent):
                lines.append("完整清单: .agent/progress.md")
        if self.current:
            lines.append(f"当前: {self.current}")
        if self.verify:
            lines.append(f"待验证: {self.verify}")
        if self.excluded:
            lines.append("已排除: " + " | ".join(self.excluded))
        if self.hypothesis:
            lines.append(f"下一步假设: {self.hypothesis}")
        return "\n".join(lines)

    def token_cost(self, counter: TokenCounter) -> int:
        return counter.count(self.render())

    def apply(self, delta: StateDelta) -> None:
        """应用增量修改。"""
        self.done.extend(delta.done_added)
        self.excluded.extend(delta.excluded_added)
        if delta.current is not None:
            self.current = delta.current
        if delta.verify is not None:
            self.verify = delta.verify
        if delta.hypothesis is not None:
            self.hypothesis = delta.hypothesis

    def step_forward(self) -> None:
        self.step += 1


def save_state(state: TaskState, path: Path) -> None:
    """把状态写入检查点文件，父目录不存在则创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_state(path: Path) -> TaskState | None:
    """读取检查点，文件不存在则返回 None。"""
    if not path.exists():
        return None
    return TaskState(**json.loads(path.read_text(encoding="utf-8")))


def clear_state(path: Path) -> None:
    """任务成功结束后清掉检查点。

    留着它会让下一次运行误以为「有活没干完」。检查点是过程状态，不是结果——
    结果已经沉淀成事件和记忆了。
    """
    path.unlink(missing_ok=True)

