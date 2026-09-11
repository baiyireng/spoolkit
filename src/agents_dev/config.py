"""运行配置。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """一次运行的配置。context_window 为模型上下文窗口总长。"""

    project_root: Path
    context_window: int = 8192
    max_steps: int = 10
    state_dir_name: str = ".agent"

    @property
    def state_dir(self) -> Path:
        return self.project_root / self.state_dir_name

    def task_path(self, task_id: str) -> Path:
        return self.state_dir / "tasks" / f"{task_id}.json"

