"""任务状态与检查点。

任务状态是「我做到哪了」的结构化表达，每轮注入上下文，
从而让对话历史变成可以随时丢弃的东西。这是短上下文设计的支点。
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agents_dev.llm.tokenizer import TokenCounter


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

    def render(self) -> str:
        """渲染成紧凑文本供注入。刻意省略空字段以节省 token。"""
        lines = [f"目标: {self.goal}"]
        if self.done:
            lines.append("已完成: " + " | ".join(self.done))
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

