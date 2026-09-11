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
    state_dir_name: str = ".agent"

    @property
    def state_dir(self) -> Path:
        return self.project_root / self.state_dir_name

    def task_path(self, task_id: str) -> Path:
        return self.state_dir / "tasks" / f"{task_id}.json"

