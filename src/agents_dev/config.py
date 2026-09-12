"""运行配置。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """一次运行的配置。

    两个步数预算刻意不一样，而且子智能体更大：
    主循环的每一步都很贵——它维护全局状态，一旦崩了整个任务要重来，
    所以应该克制；子智能体是一次性容器，崩了重派一次即可，所以应该给足。
    反过来设等于把昂贵的预算用在长任务上、把便宜的用在短任务上，正好相反。
    """

    project_root: Path
    context_window: int = 8192
    max_steps: int = 10
    subagent_steps: int = 20
    # 一次派发最多「实现 → 审查」几轮。审查不通过会打回给主循环，
    # 由它决定要不要再派一次修复；这个数字是那道循环的上限，
    # 不是留给模型自己收敛的空间——没有上限的自动重试等于没有退路。
    review_rounds: int = 2
    # 督导：预算用尽或检测到打转时，由一个独立上下文的会话判断该续期、
    # 改道还是收手。关掉就退回「撞上限即失败」——那需要用户自己 --resume，
    # 等于把判断成本推给用户。
    supervise: bool = True
    # 总步数上限（含督导给的续期）。0 表示按基础预算自动算——它是一条
    # 安全线，不是任务预算：真正的预算由督导按进展给。
    step_ceiling: int = 0
    state_dir_name: str = ".agent"

    @property
    def state_dir(self) -> Path:
        return self.project_root / self.state_dir_name

    def task_path(self, task_id: str) -> Path:
        return self.state_dir / "tasks" / f"{task_id}.json"

