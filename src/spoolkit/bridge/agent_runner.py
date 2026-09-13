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

import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from spoolkit.web.runner import Runner

POLL_SECONDS = 0.5

# "应用 / 丢弃"怎么回答。中英文都给：用户在手机上打字，越短越顺手。
YES_WORDS = frozenset({"y", "yes", "是", "好", "可以", "应用", "1"})
NO_WORDS = frozenset({"n", "no", "不", "否", "丢弃", "不要", "0"})


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
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.project_root = project_root
        self.session = session
        self.extra_args = list(extra_args)
        self.timeout = timeout
        self._clock = clock
        self._sleep = sleep
        self._runner: Runner | None = None
        # 超时之后**结果还要送回来**：聊天通道里"等超时就丢掉结论"等于这个任务
        # 白跑了。notify 由桥注入（它知道回给哪个会话）。
        self.notify = notify

    # --- 给桥用的接口 ---

    def set_notify(self, notify: Callable[[str], None] | None) -> None:
        """桥用它把"补发一条消息"的能力挂上来（见 `_watch_later`）。"""
        self.notify = notify

    def __call__(self, message: str) -> str:
        reply = self._confirmation_answer(message)
        if reply is not None:
            return reply
        # **复用同一个 Runner**。原先每条消息都新建一个：`start()` 里那句
        # "已有运行在跑就拒绝"只认自己那一个实例，于是你连着发两条，第二个
        # agent 会在同一个工作区里同时跑起来（记忆、检查点、进度文件一起写）。
        runner = self._runner
        if runner is None:
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
                self._watch_later(runner)
                return (
                    f"这一轮超过 {int(self.timeout)} 秒还没结束，我先不等了——"
                    "它还在后台跑，跑完我会把结论发给你。"
                )
            self._sleep(POLL_SECONDS)

    def _watch_later(self, runner: Runner) -> None:
        """超时之后守着这一轮：它跑完（或被确认）就把结论补发出去。

        为什么要有：聊天通道里长任务是常态，而桥不可能一直等着。原先的做法是
        回一句"不等了"就**把结论丢掉**——从用户那边看，就是"指挥它干了活，
        然后没有然后了"。
        """
        if self.notify is None:
            return

        def watch() -> None:
            while True:
                snapshot = runner.snapshot()
                if snapshot.get("finished"):
                    text = self._final_text(snapshot)
                    try:
                        self.notify(f"（这一轮跑完了）\n{text}")
                    except Exception:  # noqa: BLE001 - 补发失败不该影响谁
                        pass
                    return
                if not snapshot.get("running"):
                    # 既没在跑也没给结局：子进程没了，别再等了。
                    return
                self._sleep(POLL_SECONDS)

        threading.Thread(target=watch, daemon=True).start()

    def _confirmation_answer(self, message: str) -> str | None:
        """正在等授权时，`y`/`n` 是**回答**，不是新任务。

        真踩过：提示语写着"回 y 应用、n 丢弃"，而 CLI 那条循环把每条消息都当
        新目标——你回一个 `y`，它就拿着目标 "y" 又跑一轮 agent，而真正在等确认
        的那一轮一直卡着。这类"看起来在工作、其实答非所问"最难查。
        """
        word = message.strip().lower()
        if word not in YES_WORDS and word not in NO_WORDS:
            return None
        runner = self._runner
        if runner is None:
            return None
        try:
            snapshot = runner.snapshot()
        except Exception:  # noqa: BLE001 - 快照拿不到就当没在等
            return None
        if not snapshot.get("awaiting"):
            return None
        return self.confirm(word in YES_WORDS)

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
