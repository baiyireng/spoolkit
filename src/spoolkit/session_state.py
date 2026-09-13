"""会话级状态放在哪。

**为什么要有这一层**：`plan.json`、`progress.md`、`tasks/*.json` 原先都是
**工作区级**的——同一个工作区里换个会话，读到的还是上一个会话的计划与进度。
真实后果见过两次：

- QQ 那轮让 agent 找小说，它读到了别的任务（`scratch_lab` 的 percent 函数）的
  残留计划，最后一条答复答非所问（回了一句 pytest 死循环）；
- 「我明明在做 A，它说的却是 B」这类困惑，根子都在这里。

会话本来就是"不同时段/目的的活分开记"（`--session` 的原话），那计划与进度就
该跟着会话走。所以统一挪到 `.agent/sessions/<会话>/`：

| 文件 | 是什么 |
|---|---|
| `plan.json` | 这个会话的计划与步骤状态 |
| `progress.md` | 这个会话"做过什么"的全量清单 |
| `tasks/<id>.json` | 主循环与子智能体的检查点 |

**工作区级的东西不动**：记忆库（`memory.db`，会话是里面的一列）、热记忆
（`memory.md`）、索引（`index.db`）、策略与标定值、配对记录——它们本来就该
跨会话共享。

旧位置（`.agent/plan.json` 等）**不再读**：那正是串味的来源，读它等于把刚拆掉
的问题装回去。想在旧工作区里找回旧计划，手工看一眼再重排一次即可。
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_SESSION = "cli"
SESSIONS_DIR = "sessions"


def safe(name: str) -> str:
    """把会话名变成安全的目录名。中文、字母数字、`-`、`_`、`.` 都留着。"""
    cleaned = re.sub(r"[^\w.-]+", "_", str(name).strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("._")
    return cleaned or DEFAULT_SESSION


def dir_for(root: Path | str, session: str = DEFAULT_SESSION) -> Path:
    return Path(root) / ".agent" / SESSIONS_DIR / safe(session)


def plan_path(root: Path | str, session: str = DEFAULT_SESSION) -> Path:
    return dir_for(root, session) / "plan.json"


def progress_path(root: Path | str, session: str = DEFAULT_SESSION) -> Path:
    return dir_for(root, session) / "progress.md"


def task_path(root: Path | str, session: str, task_id: str) -> Path:
    return dir_for(root, session) / "tasks" / f"{safe(task_id)}.json"


def hint(root: Path | str, session: str = DEFAULT_SESSION) -> str:
    """给模型看的相对路径（状态块里那句"完整清单在…"）。"""
    return progress_path(root, session).relative_to(Path(root)).as_posix()
