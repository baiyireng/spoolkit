"""把一条聊天消息交给本项目的 agent 去跑。

用的就是网页壳那套机制（`web.runner.Runner`：起一个 `run --events` 子进程、
读它的事件流），所以两条入口的判定完全一致——同一个授权策略、同一个工作区、
同一份待确认清单。**不是另写一套。**

几条刻意的选择：

- **超时不是"没反应"**：到点了回一句"还在跑 / 已超时"，而不是让用户面对
  空白自行推测。长任务本来就可能跑十几分钟，聊天通道更适合"先回执、后结果"。
- **待确认要能回答**：agent 若在等授权（越界改动），回复里带上"回 y 应用、
  n 丢弃"，`confirm()` 负责把这句话转达给子进程的 stdin。
- 子进程的每一步事件都留着（`last_events`），出问题时排查有据。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from spoolkit.kernel.target import Target
from spoolkit.web.runner import Runner

POLL_SECONDS = 0.5

# "应用 / 丢弃"怎么回答。中英文都给：用户在手机上打字，越短越好输入。
YES_WORDS = frozenset({"y", "yes", "是", "好", "可以", "应用", "1"})
NO_WORDS = frozenset({"n", "no", "不", "否", "丢弃", "不要", "0"})
# 命令授权比"是/否"多两档：始终允许（记进本工作区）、本轮全拒。
# 压成两态就等于把这两个选项从聊天里删掉了。
ALWAYS_WORDS = frozenset({"a", "always", "始终", "总是", "一直", "2"})
BLOCK_WORDS = frozenset({"b", "block", "全拒", "都拒绝", "别再问", "3"})
ANSWERS = {
    **{word: "y" for word in YES_WORDS},
    **{word: "n" for word in NO_WORDS},
    **{word: "a" for word in ALWAYS_WORDS},
    **{word: "b" for word in BLOCK_WORDS},
}


class AgentRunner:
    """`Callable[[str], str]`：给一条消息，回一段答复。

    一条通道服务整台机器时，"这次落到哪个工作区、哪条会话"是**按聊天**算的，
    所以第一个参数既可以是 `(client_key) -> Target` 的解析函数（新的形状），
    也可以直接给一个工作区路径（老的形状：这条通道只服务那一个工作区）。
    """

    def __init__(
        self,
        resolve: Callable[[str], Target] | Path | str,
        *,
        session: str = "",
        default_session: str = "bridge",
        extra_args: Sequence[str] = (),
        read_roots_for: Callable[[Path], Sequence[str]] | None = None,
        timeout: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        if callable(resolve):
            self._resolve: Callable[[str], Target] = resolve
        else:
            # 老形状：固定一个工作区（`--root` 给了的桥就走这条）。
            fixed_root = Path(resolve)
            fixed_session = str(session or default_session)
            self._resolve = lambda _key: Target(
                root=fixed_root,
                session=fixed_session,
                workspace_source="explicit",
                session_source="default",
            )
        self.default_session = str(default_session)
        self.session = str(session or default_session)
        self.project_root = None if callable(resolve) else Path(resolve)
        self.extra_args = list(extra_args)
        # 关联目录（`--allow-read`）是**工作区级**的，机器级桥启动时还不知道服务谁，
        # 所以它按每一轮的目标现算。给了这个回调就按目标补，不给就用 `extra_args` 里的。
        self.read_roots_for = read_roots_for
        self.timeout = timeout
        self._clock = clock
        self._sleep = sleep
        # 一个 (工作区, 会话) 一个 Runner：`start()` 里那句"已有运行在跑就拒绝"
        # 是按实例算的，共用一个实例会让两个聊天互相挡住。
        self._runners: dict[str, Runner] = {}
        self._current = ""
        # 兼容老用法（测试直接塞一个替身进来）：非空时优先用它。
        self._runner: Runner | None = None
        # 超时之后**结果还要送回来**：聊天通道里"等超时就丢掉结论"等于这个任务
        # 白跑了。notify 由桥注入（它知道回给哪个会话）。
        self.notify = notify

    # --- 给桥用的接口 ---

    def set_notify(self, notify: Callable[[str], None] | None) -> None:
        """桥用它把"补发一条消息"的能力挂上来（见 `_watch_later`）。"""
        self.notify = notify

    def for_client(self, client_key: str) -> Callable[[str], str]:
        """桥按"每条聊天"取一个可调用对象（`Bridge` 认得这个接口就用它）。"""
        return lambda text: self.run(text, client_key)

    def __call__(self, message: str) -> str:
        return self.run(message, "")

    def run(self, message: str, client_key: str = "") -> str:
        reply = self._confirmation_answer(message)
        if reply is not None:
            return reply
        reply = self._undo_answer(message)
        if reply is not None:
            return reply
        # **按目标复用同一个 Runner**。原先每条消息都新建一个：`start()` 里那句
        # "已有运行在跑就拒绝"只认自己那一个实例，于是你连着发两条，第二个
        # agent 会在同一个工作区里同时跑起来（记忆、检查点、进度文件一起写）。
        runner = self._runner_for(client_key)
        if not runner.start(message):
            return "上一轮仍在运行，等它结束后再发。"
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
                    f"这一轮已超过 {int(self.timeout)} 秒仍未结束，不再等待；"
                    "它仍在后台运行，结束后我会把结论发给你。"
                )
            self._sleep(POLL_SECONDS)

    def _runner_for(self, client_key: str) -> Runner:
        if self._runner is not None:  # 测试塞进来的替身
            return self._runner
        target = self._resolve(client_key)
        key = f"{target.root}\u0000{target.session}"
        runner = self._runners.get(key)
        if runner is None:
            extra = list(self.extra_args)
            if self.read_roots_for is not None:
                extra += list(self.read_roots_for(target.root) or ())
            runner = Runner(
                target.root, session=target.session, extra_args=extra
            )
            self._runners[key] = runner
        self._current = key
        return runner

    def _awaiting_runner(self) -> Runner | None:
        """正在等确认的那个 runner：先看最近用过的那条聊天，再扫全部。"""
        candidates: list[Runner] = []
        if self._runner is not None:
            candidates.append(self._runner)
        for key in [self._current, *self._runners]:
            runner = self._runners.get(key)
            if runner is not None and runner not in candidates:
                candidates.append(runner)
        for runner in candidates:
            try:
                if runner.snapshot().get("awaiting"):
                    return runner
            except Exception:  # noqa: BLE001 - 快照拿不到就当没在等
                continue
        return None

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
                        self.notify(f"（这一轮已结束）\n{text}")
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
        if word not in ANSWERS:
            return None
        runner = self._awaiting_runner()
        if runner is None:
            return None
        try:
            snapshot = runner.snapshot()
        except Exception:  # noqa: BLE001 - 快照拿不到就当没在等
            return None
        if not snapshot.get("awaiting"):
            return None
        detail = snapshot.get("awaiting_detail") or {}
        if detail.get("command"):
            # 命令授权：四档原样回给子进程。
            answer = ANSWERS[word]
            writer = getattr(runner, "answer", None)
            if callable(writer) and writer(answer):
                return {
                    "y": "已允许这一次。",
                    "a": "已始终允许（记在这个工作区）。",
                    "b": "本轮不再询问这类命令。",
                }.get(answer, "已拒绝。")
            return "现在没有待确认的授权请求。"
        return self._confirm(runner, word in YES_WORDS)

    def confirm(self, apply: bool) -> str:
        """回答 agent 的授权询问（它正在等）。"""
        runner = self._awaiting_runner()
        if runner is None:
            return "现在没有待确认的改动。"
        return self._confirm(runner, apply)

    def _undo_answer(self, message: str) -> str | None:
        """**没有待确认、而上一轮留了退路时，`y` 是「撤销这一轮」**（问题④）。

        为什么这条要在聊天里存在：收尾报告写着「撤销这一轮：spool revert --run
        …（桥里回 y）」，而聊天里的人不会去抄命令。所以那一轮回一个 `y` 就等于
        点了网页壳上那枚按钮——两边走**同一份实现**（`Runner.revert`）。

        两条边界都必须守住：
        ① **有改动在等确认时，`y` 永远是「应用」**（那条路排在前面，见 `run()`）；
        ② 上一轮没有退路（没有 `revert_run_id` / 没有回退命令）时，`y` 照旧是
           一句普通的话，不能被这条吃掉——`tests/bridge` 里那条用例钉的就是它。
        """
        word = message.strip().lower()
        if word not in YES_WORDS:
            return None
        runner = self._undo_runner()
        if runner is None:
            return None
        final = runner.snapshot().get("final") or {}
        code, said = runner.revert(str(final.get("revert_run_id") or ""))
        if code == 0:
            return said
        return f"{said}（要强行退，用报告里那条 spool revert 命令。）"

    def _undo_runner(self) -> Runner | None:
        """上一轮**已经结束、而且给了回退命令**的那个 runner（没有就 None）。"""
        candidates: list[Runner] = []
        if self._runner is not None:
            candidates.append(self._runner)
        for key in [self._current, *self._runners]:
            runner = self._runners.get(key)
            if runner is not None and runner not in candidates:
                candidates.append(runner)
        for runner in candidates:
            if not callable(getattr(runner, "revert", None)):
                continue
            try:
                snapshot = runner.snapshot()
            except Exception:  # noqa: BLE001 - 快照拿不到就当它没有退路
                continue
            if snapshot.get("awaiting") or snapshot.get("running"):
                continue
            final = snapshot.get("final") or {}
            if final.get("revert_run_id") or final.get("revert_command"):
                return runner
        return None

    def _confirm(self, runner: Runner, apply: bool) -> str:
        if not runner.confirm(apply):
            return "现在没有待确认的改动。"
        return "已应用。" if apply else "已丢弃。"

    # --- 结果渲染 ---

    def _final_text(self, snapshot: dict) -> str:
        final = snapshot.get("final") or {}
        text = str(final.get("text") or "").strip()
        if text:
            return text
        return "这一轮没有产出结论；请到工作区中确认。"

    def _pending_text(self, snapshot: dict) -> str:
        detail = snapshot.get("awaiting_detail") or {}
        if detail.get("escalation"):
            # 风险分级放行的请示（§3.2.3）：**一条消息给五件 + 撤销命令**，
            # 按四档词批复。被请示的是那个**动作**，不是成果——成果已经在
            # 工作区里，所以这里绝不能说「要不要写入」。
            lines = [
                f"⚠ 一个动作被拦下、等你批复（触发条件 {detail['escalation']}）",
                f"① 影响：{detail.get('impact') or '（未填）'}",
                f"② 结论：{detail.get('conclusion') or '（未填）'}",
                f"③ 建议：{detail.get('advice') or '（未填）'}",
                f"④ 选项：{detail.get('options') or '（未填）'}",
                f"⑤ 回退：{detail.get('rollback') or '（未填）'}",
                "这一轮的成果**已经落在工作区里**，这里批的只是那个动作：",
                "回 y 批准这个动作、p 只批准其中几处、n 撤销（不执行）、r 改方案。",
            ]
            return "\n".join(lines)
        if detail.get("command"):
            # 命令授权和"改动要落盘"是两件事，话得分开说：一个是在问"能不能跑"，
            # 一个是在问"要不要写入"。混成一句话，用户点 y 之前不知道在批什么。
            return (
                "它想执行一条白名单外的命令：\n"
                f"  {detail['command']}\n"
                f"原因：{detail.get('reason') or '未说明'}\n"
                "回 y 允许这一次\n"
                "a 始终允许（记在本工作区）\n"
                "n 拒绝\n"
                "b 本轮拒绝该类命令"
            )
        count = int(snapshot.get("awaiting") or 0)
        paths = [str(item.get("path") or "?") for item in snapshot.get("diffs") or []]
        # 一行一个路径：手机上把 `a、b、c` 挤成一行会折断，看不出改了几个文件
        listed = "\n".join(f"  {path}" for path in paths[:5])
        if len(paths) > 5:
            listed += f"\n  …等 {len(paths)} 个文件"
        return (
            f"有 {count} 处改动超出授权范围，等你确认：\n{listed}\n"
            "回 y 应用、n 丢弃。"
        )
