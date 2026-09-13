"""子进程运行器。

一次只跑一个运行。这不是偷懒：并发要处理多个待确认队列怎么合并、
状态归属哪个运行，而这是本地单人用的壳——那些复杂度换不来任何东西。

读取放在后台线程。主线程必须能随时响应 HTTP 请求，尤其是「确认」——
它不能等 stdout 读完才被处理，否则点了按钮界面会僵住。

`command()` 被设计成可覆盖的方法：测试要能换掉命令行而不改动其它逻辑，
否则「确认流程」这种时序敏感的东西只能靠真 agent 跑几十秒来验证。
"""

import queue
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Sequence

from spoolkit.memory.transcript import recent_messages
from spoolkit.web.protocol import (
    AWAIT,
    CONFIRM,
    DIFF,
    FINAL,
    USAGE,
    Event,
    parse_line,
)

NOISE_LINES = 3

# 事件留痕的条数上限。外部驱动者（MCP 那条路）不能订阅 SSE，只能按序号轮询，
# 所以事件必须留一段。留全量不行：长任务的事件是几千条，内存里攒着没意义。
EVENT_LOG = 400

# 聊天区域里回放多少条历史。它**只是给人看的**：模型上下文不受它影响，
# 页面上看到的往来不等于模型看到的上下文（后者由状态与记忆按需取）。
HISTORY_LIMIT = 20


class Runner:
    """管理一次 agent 子进程的运行。"""

    def __init__(
        self,
        project_root: Path,
        session: str = "cli",
        extra_args: Sequence[str] = (),
        history_limit: int = HISTORY_LIMIT,
    ) -> None:
        self.project_root = project_root
        self.session = session
        self.extra_args = list(extra_args)
        self.history_limit = history_limit
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._listeners: list[queue.Queue] = []
        self._goal = ""
        self._awaiting = 0
        self._usage: dict = {}
        self._final: dict | None = None
        self._finished = False
        # 最近几条不是事件的输出。子进程崩了的时候，原因多半就写在这里
        # （异常回溯、参数错误提示都在 stderr 上，而 stderr 也接到了本管道）。
        # 丢掉它们的话，界面只会说「退出码 2」，谁都猜不出为什么。
        self._noise: list[str] = []
        # 待确认的 diff。断线重连的页面拿不到漏掉的事件，只能靠快照；
        # 快照里没有 diff 的话，按钮会回来、内容却是空的——
        # 那时候用户只能盲点「应用」。
        self._diffs: list[dict] = []
        # 事件留痕：给不能订阅 SSE 的驱动者轮询用（MCP）。带序号，
        # 因为"上次看到第几条"是驱动者唯一能说的话。
        self._log: list[dict] = []
        self._log_seq = 0

    # --- 命令 ---

    def command(self, goal: str) -> list[str]:
        """拼出要执行的命令行。子类可覆盖它来换掉被测进程。"""
        return [
            sys.executable,
            "-m",
            "spoolkit.cli.app",
            "run",
            "--events",
            "--session",
            self.session,
            "--root",
            str(self.project_root),
            *self.extra_args,
            "--goal",
            goal,
        ]

    # --- 生命周期 ---

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, goal: str) -> bool:
        """启动一次运行。已有运行在跑则拒绝。"""
        with self._lock:
            if self.running:
                return False
            self._goal = goal
            self._awaiting = 0
            self._usage = {}
            self._final = None
            self._finished = False
            self._noise = []
            self._diffs = []
            self._log = []
            self._log_seq = 0
            self._process = subprocess.Popen(
                self.command(goal),
                cwd=str(self.project_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            thread = threading.Thread(
                target=self._pump, args=(self._process,), daemon=True
            )
            thread.start()
            return True

    def confirm(self, apply: bool) -> bool:
        """把用户的决定写进子进程的 stdin。"""
        with self._lock:
            if self._awaiting <= 0 or self._process is None:
                return False
            if self._process.stdin is None:
                return False
            self._process.stdin.write("y\n" if apply else "n\n")
            self._process.stdin.flush()
            self._awaiting = 0
            return True

    # --- 事件分发 ---

    def subscribe(self) -> queue.Queue:
        listener: queue.Queue = queue.Queue()
        with self._lock:
            self._listeners.append(listener)
        return listener

    def unsubscribe(self, listener: queue.Queue) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def snapshot(self) -> dict:
        """当前状态。SSE 每次连接的首帧就是它。

        快照必须在锁内取完整的一份：分几次取的话，中间可能有事件进来，
        你会拿到一个自相矛盾的状态（比如「运行中」但已经给出了 final）。
        """
        with self._lock:
            return {
                "running": self.running,
                "finished": self._finished,
                "awaiting": self._awaiting,
                "goal": self._goal,
                "usage": dict(self._usage),
                "final": dict(self._final) if self._final else None,
                "diffs": [dict(item) for item in self._diffs],
                "session": self.session,
                "messages": self._transcript(),
            }

    def _transcript(self) -> list[dict]:
        """这个会话之前的往来——**给人看的**，不进入模型上下文。

        两者混为一谈会得出"那就把历史塞回上下文吧"的结论，而那会把这个项目
        的支点（短上下文）直接推翻。所以它只出现在页面快照里。

        读的是子进程在写的那个库：只读打开、失败就当没有——历史是增强，
        不该因为它把状态接口弄挂。
        """
        path = self.project_root / ".agent" / "memory.db"
        if not path.is_file():
            return []
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            return []
        try:
            conn.row_factory = sqlite3.Row
            rows = recent_messages(conn, self.session, self.history_limit)
            return [
                {
                    "role": row["role"],
                    "content": row["content"],
                    "meta": row["meta"] or "",
                }
                for row in rows
            ]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def _broadcast(self, event: Event) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            listener.put(event)

    def _absorb(self, event: Event) -> None:
        with self._lock:
            self._log_seq += 1
            self._log.append({"seq": self._log_seq, **event.data, "type": event.type})
            del self._log[:-EVENT_LOG]
            if event.type == AWAIT:
                self._awaiting = int(event.data.get("count", 1))
            elif event.type == DIFF:
                self._diffs.append(dict(event.data))
            elif event.type == CONFIRM:
                self._diffs = []
            elif event.type == USAGE:
                self._usage = dict(event.data)
            elif event.type == FINAL:
                self._final = dict(event.data)
                self._awaiting = 0
                self._diffs = []

    def events(self, since: int = 0) -> list[dict]:
        """序号大于 since 的事件。外部驱动者按 last_seq 往下拉。"""
        with self._lock:
            return [dict(item) for item in self._log if item["seq"] > since]

    def _pump(self, process: subprocess.Popen) -> None:
        """后台读取子进程输出，逐行解析并广播。"""
        if process.stdout is not None:
            for raw in process.stdout:
                event = parse_line(raw)
                if event is None:
                    self._note_noise(raw)
                    continue
                self._absorb(event)
                self._broadcast(event)
        process.wait()
        with self._lock:
            self._finished = True
            self._awaiting = 0
            code = process.returncode
            fallback = None
            if self._final is None:
                # 子进程没给结局就退出了（崩了、被杀了）。**必须补一条结局**，
                # 否则界面会永远停在「运行中」，而你会以为它还在干活。
                #
                # 补的这条要同时进快照，而且要和 finished 一起写：分两步写的话
                # 中间有个瞬间是「已完成、却没有结局」，那一刻恰好连上来的页面
                # 什么都显示不出来——既像跑完了又像没跑，比报错还难查。
                fallback = Event(FINAL, {"ok": False, "text": self._death_note(code)})
                self._final = dict(fallback.data)
        if fallback is not None:
            self._broadcast(fallback)

    def _note_noise(self, raw: str) -> None:
        text = raw.strip()
        if not text:
            return
        with self._lock:
            self._noise.append(text)
            del self._noise[:-NOISE_LINES]

    def _death_note(self, code: int | None) -> str:
        """给一句能查下去的失败原因。调用方必须已持有锁。"""
        reason = "；".join(self._noise[-NOISE_LINES:])
        head = f"进程退出，退出码 {code}"
        return f"{head}：{reason}" if reason else head
