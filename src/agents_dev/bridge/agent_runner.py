"""把一条聊天消息交给本项目的 agent 去跑。

用的就是网页壳那套机制（`web.runner.Runner`：起一个 `run --events` 子进程、
读它的事件流），所以两条入口的判定完全一致——同一个授权策略、同一个工作区、
同一份待确认清单。**不是另写一套。**

几条刻意的选择：

- **超时不是"没反应"**：到点了回一句"还在跑 / 已超时"，而不是让用户对着
  空白猜。长任务本来就可能跑十几分钟，聊天通道更适合"先回执、后结果"。
- **待确认要能回答**：agent 若在等授权（越界改动），回复里带上"回 y 应用、
  n 丢弃"，`confirm()` 负责把这句话转达给子进程的 stdin。
- 子进程的每一步事件都留着（`last_events`），出问题时排查不用靠猜。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Sequence

from agents_dev.web.runner import Runner

POLL_SECONDS = 0.5


class AgentRunner:
    """`Callable[[str], str]`：给一条消息，回一段答复。"""

    def __init__(
        self,
        project_root: Path,
        session: str = "bridge",
        extra_args: Sequence[str] = (),
        timeout: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.project_root = project_root
        self.session = session
        self.extra_args = list(extra_args)
        self.timeout = timeout
        self._clock = clock
        self._sleep = sleep
        self._runner: Runner | None = None

    # --- 给桥用的接口 ---

    def __call__(self, message: str) -> str:
        runner = Runner(
            self.project_root, session=self.session, extra_args=self.extra_args
        )
        self._runner = runner
        if not runner.start(message):
            return "上一轮还在跑，等它结束再发。"
        deadline = self._clock() + self.timeout
        while True:
            snapshot = runner.snapshot()
            if snapshot.get("awaiting"):
                return self._pending_text(snapshot)
            if snapshot.get("finished"):
                return self._final_text(snapshot)
            if self._clock() >= deadline:
                return (
                    f"这一轮超过 {int(self.timeout)} 秒还没结束，我先不等了——"
                    "它还在后台跑，等会儿问它进度（或者直接去看工作区）。"
                )
            self._sleep(POLL_SECONDS)

    def confirm(self, apply: bool) -> str:
        """回答 agent 的授权询问（它正在等）。"""
        runner = self._runner
        if runner is None or not runner.confirm(apply):
            return "现在没有待确认的改动。"
        return "已应用。" if apply else "已丢弃。"

    # --- 结果渲染 ---

    def _final_text(self, snapshot: dict) -> str:
        final = snapshot.get("final") or {}
        text = str(final.get("text") or "").strip()
        if text:
            return text
        return "这一轮没有产出结论，去工作区里看一眼吧。"

    def _pending_text(self, snapshot: dict) -> str:
        count = int(snapshot.get("awaiting") or 0)
        paths = [str(item.get("path") or "?") for item in snapshot.get("diffs") or []]
        listing = "、".join(paths[:5]) + ("…" if len(paths) > 5 else "")
        return (
            f"有 {count} 处改动超出授权范围，等你确认：{listing}\n"
            "回 y 应用、n 丢弃。"
        )
