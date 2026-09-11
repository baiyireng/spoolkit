"""子进程运行器。

一次只跑一个运行。这不是偷懒：并发要处理多个待确认队列怎么合并、
状态归属哪个运行，而这是本地单人用的壳——那些复杂度换不来任何东西。

读取放在后台线程。主线程必须能随时响应 HTTP 请求，尤其是「确认」——
它不能等 stdout 读完才被处理，否则点了按钮界面会僵住。

`command()` 被设计成可覆盖的方法：测试要能换掉命令行而不改动其它逻辑，
否则「确认流程」这种时序敏感的东西只能靠真 agent 跑几十秒来验证。
"""

import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Sequence

from agents_dev.web.protocol import (
    AWAIT,
    FINAL,
    USAGE,
    Event,
    parse_line,
)


class Runner:
    """管理一次 agent 子进程的运行。"""

    def __init__(
        self,
        project_root: Path,
        session: str = "cli",
        extra_args: Sequence[str] = (),
    ) -> None:
        self.project_root = project_root
        self.session = session
        self.extra_args = list(extra_args)
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._listeners: list[queue.Queue] = []
        self._goal = ""
        self._awaiting = 0
        self._usage: dict = {}
        self._final: dict | None = None
        self._finished = False

    # --- 命令 ---

    def command(self, goal: str) -> list[str]:
        """拼出要执行的命令行。子类可覆盖它来换掉被测进程。"""
        return [
            sys.executable,
            "-m",
            "agents_dev.cli.app",
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
                "session": self.session,
            }

    def _broadcast(self, event: Event) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            listener.put(event)

    def _absorb(self, event: Event) -> None:
        with self._lock:
            if event.type == AWAIT:
                self._awaiting = int(event.data.get("count", 1))
            elif event.type == USAGE:
                self._usage = dict(event.data)
            elif event.type == FINAL:
                self._final = dict(event.data)
                self._awaiting = 0

    def _pump(self, process: subprocess.Popen) -> None:
        """后台读取子进程输出，逐行解析并广播。"""
        if process.stdout is not None:
            for raw in process.stdout:
                event = parse_line(raw)
                if event is None:
                    continue
                self._absorb(event)
                self._broadcast(event)
        process.wait()
        with self._lock:
            self._finished = True
            self._awaiting = 0
            code = process.returncode
            had_final = self._final is not None
        if not had_final:
            # 子进程没给结局就退出了（崩了、被杀了）。**必须补一条结局**，
            # 否则界面会永远停在「运行中」，而你会以为它还在干活。
            self._broadcast(
                Event(FINAL, {"ok": False, "text": f"进程退出，退出码 {code}"})
            )

