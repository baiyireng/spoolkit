"""按 (工作区, 会话) 复用 Runner 的注册表。

为什么要这一层：网页壳服务**整台机器**，页面上的选择栏能让"当前目标"换来换去。
每次切换都新建一个 Runner 的话，切回去看到的会是**另一条互不相干的历史**——
事件留痕（`Runner._log`）、待确认的 diff、以及正在跑的那个子进程都在旧对象里，
而页面上只表现为"刚才那些怎么没了"。所以同一个 (工作区, 会话) 必须拿到
**同一个对象**。

形状照 `mcp/server.py` 里那条路（也是"一个 (工作区, 会话) 一个 Runner"），
两处不要各写一套。

回收：注册表必须有上限，否则切来切去会攒下一堆 Runner（每个都留着它的事件留痕）。
上限只管**空闲**的那些——**在跑的一律不淘汰**：切走不许打断任务（设计 §3.4），
"我刚发起的任务因为我看了一眼别的工作区就被杀了"是比"表里多几个对象"坏得多的事。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from spoolkit.web.runner import Runner

# 默认上限的标定依据（这个仓库不写死阈值）：
#
# - 一个**空闲** Runner 常驻的就是它的事件留痕（`runner.EVENT_LOG` = 400 条
#   事件 + 少量状态字段），所以 8 ≈ 3200 条事件的量级——本地单人用的壳，
#   这个内存量级可以忽略；
# - 8 也够覆盖"一个人一次坐下来真正会来回切的 (工作区, 会话) 组合数"：再多，
#   页面上的下拉也选不过来，而超出上限淘汰的是**最久没看的那个空闲目标**，
#   最坏后果是切回去时历史从头开始——不丢正在跑的任务，也不丢磁盘上的记忆。
#
# 定成 2 以下没有意义：那样切换就退化成"每次都新建"，等于没有这张表。
DEFAULT_LIMIT = 8

# 键里的分隔符：工作区名里可能有冒号（Windows 盘符），会话名里可能有空格，
# 只有 NUL 不会出现在路径与参数里。
KEY_SEP = "\u0000"


def _key(root: Path, session: str) -> str:
    return f"{root}{KEY_SEP}{session}"


class RunnerRegistry:
    """一张 (工作区, 会话) → Runner 的表，带上限与回收。

    `build(root, session)` 由调用方给：注册表不认识 Runner 的构造参数
    （转发给子进程的那组、探活回调等），它只负责"同一个键给同一个对象"。
    """

    def __init__(
        self,
        build: Callable[[Path, str], Runner],
        limit: int = DEFAULT_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if int(limit) < 1:
            raise ValueError("注册表上限至少是 1；为 0 时它连一个目标都留不住。")
        self._build = build
        self._limit = int(limit)
        self._clock = clock
        self._lock = threading.Lock()
        self._runners: dict[str, Runner] = {}
        # 最近一次被取用的时刻。淘汰挑它最小的那个（"最久未使用"）。
        self._used: dict[str, float] = {}

    # --- 取用 ---

    def get(self, root: Path | str, session: str) -> Runner:
        """这个 (工作区, 会话) 的 Runner。同键永远拿到同一个对象。"""
        target = Path(root).resolve()
        name = str(session)
        key = _key(target, name)
        with self._lock:
            runner = self._runners.get(key)
            if runner is not None:
                self._used[key] = self._clock()
                return runner
            # 构造放在锁内：`build` 只是 new 一个 Runner（不含子进程，`start()`
            # 才起进程），很便宜；放在锁外则要处理"两个线程同时发现没有、
            # 各建一个"的竞态，而那个竞态的后果正是这张表最想避免的
            # "同一个目标有两条互不相干的历史"。
            runner = self._build(target, name)
            self._runners[key] = runner
            self._used[key] = self._clock()
            self._evict_locked(keep=key)
            return runner

    def _evict_locked(self, keep: str) -> None:
        """超出上限就丢最久未使用的**空闲**那个。调用方必须已持有锁。

        `keep` 是刚取用的那个键，**永远不当淘汰对象**：不排除它的话，
        上限为 1 时会出现"刚造出来就被自己挤掉"——调用方拿到一个已经不在表里的
        Runner，下次再要又新建一个，等于这张表时灵时不灵。

        淘汰只是把它从表里摘掉：挑出来的都是没在跑的（进程已经结束或从没起过），
        没有子进程要收，摘掉之后它自然被回收。
        """
        while len(self._runners) > self._limit:
            victim = self._victim_locked(keep)
            if victim is None:
                # 在跑的一个都不许杀，于是超了也只能先留着——上限不是硬保证，
                # 杀任务比多留几个对象严重得多。
                return
            self._runners.pop(victim, None)
            self._used.pop(victim, None)

    def _victim_locked(self, keep: str) -> str | None:
        idle = [
            (self._used.get(key, 0.0), key)
            for key, runner in self._runners.items()
            if not runner.running and key != keep
        ]
        if not idle:
            return None
        return min(idle)[1]

    # --- 给页面看的 ---

    def running_targets(self) -> list[dict]:
        """谁在跑。给 `/state` 用：页面必须**说得出**"别处还有一条在跑"。

        刻意只取 `Runner.brief()` 而不是完整快照：快照会去探活模型与配方
        （走 HTTP），页面每几秒轮一次 `/state`，给每个在跑的 Runner 各探一次
        会把状态接口变成一串等待，而这里要的只是"在不在跑、在做什么"。
        """
        with self._lock:
            items = list(self._runners.items())
        found: list[dict] = []
        for key, runner in items:
            if not runner.running:
                continue
            root, _, session = key.partition(KEY_SEP)
            line = runner.brief()
            found.append(
                {
                    "root": root,
                    "name": Path(root).name,
                    "session": session,
                    "goal": line.get("goal", ""),
                    "awaiting": line.get("awaiting", 0),
                }
            )
        return found

    # --- 生命周期 ---

    def close(self) -> None:
        """收掉自己拉起的子进程（照 `cli/commands/hub.py::stop_children` 的做法）。

        只收**自己这张表里的**：别人的进程不该被这里碰到。
        """
        with self._lock:
            runners = list(self._runners.values())
            self._runners.clear()
            self._used.clear()
        for runner in runners:
            try:
                runner.stop()
            except OSError:
                # 进程已经没了（terminate 与 poll 之间它自己退了）。收尾不该因此中断。
                continue
