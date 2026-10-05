"""子进程运行器。

一次只跑一个运行。理由：并发要处理多个待确认队列怎么合并、
状态归属哪个运行，而这是本地单人用的壳——那些复杂度没有收益。

读取放在后台线程。主线程必须能随时响应 HTTP 请求，尤其是「确认」——
它不能等 stdout 读完才被处理，否则点了按钮界面会僵住。

`command()` 被设计成可覆盖的方法：测试要能换掉命令行而不改动其它逻辑，
否则「确认流程」这种时序敏感的行为只能靠真 agent 跑几十秒来验证。
"""

import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

from spoolkit import health as health_mod
from spoolkit.bridge.pairing import Pairings
from spoolkit.memory.transcript import ASSISTANT, recent_messages
from spoolkit.web.protocol import (
    AWAIT,
    CONFIRM,
    DIFF,
    ERROR,
    FINAL,
    NOTE,
    STEP,
    TOOL,
    USAGE,
    WAIT,
    Event,
    parse_line,
)

NOISE_LINES = 3

# 一轮的"思考轨迹"留多少行、留几轮。
#
# 为什么要有上限：这份轨迹随每一帧 `/state` 发给页面（页面每 3 秒轮一次），
# 而一轮里 `wait` 心跳与工具调用可以上百条。40 行 ≈ 40 步的量级，足够看清
# "它卡在哪一步、哪次调用失败了"，又不会把轮询变成搬数据。
#
# 为什么留 4 轮而不是 1 轮：用户的原话是"刷新页面、切走再切回，仍能回看那一轮
# 的思考"——一次坐下来说不定跑了好几轮，只有最近那一轮等于"上一轮的错再也查不到"。
# 4 轮 × 40 行 × 一行几百字节，最坏也就几十 KB，本地单机够用。
TRACE_LINES = 40
TRACE_TURNS = 4

# 事件留痕的条数上限。外部驱动者（MCP 那条路）不能订阅 SSE，只能按序号轮询，
# 所以事件必须留一段。留全量不行：长任务的事件是几千条，内存里攒着没意义。
EVENT_LOG = 400

# 聊天区域里回放多少条历史。面向页面的往来；模型拿的是另一份
# 截断后的最近几轮（`conversational_prompt`），不是这份完整回放。
HISTORY_LIMIT = 20

# 收子进程时等多久才升级成强杀。与 `cli/commands/hub.py::CHILD_WAIT` 同一个
# 量级（那边管的是桥子进程）：长驻进程收到 terminate 后要收尾（关通道、写状态），
# 给太短会把"自己能收干净"的那条路直接跳过去。
STOP_WAIT = 8.0


def health_snapshot(
    extra_args: Sequence[str] = (),
    extra_health=None,
    *,
    timeout: float = health_mod.PROBE_TIMEOUT_SECONDS,
) -> dict:
    """模型（以及 hub 可选附带的通道）通不通。展示用的，不进模型上下文。

    单独拎成模块级函数：机器级网页壳在**还没有当前目标**时也要能给出这一块
    （页面横幅读的就是它），而那时连一个 Runner 都还没有。探活失败不得把状态
    接口弄挂——探活是增强。

    这个函数**会发 HTTP**（等一口网络）。网页壳的请求路径不许直接调它：那边
    有后台探针（`web/probe.py`）把结果算好，`Runner.snapshot(health=…)` 收下。
    直接调它的地方是：桥 / MCP / 测试，以及没有后台探针的老用法。
    """
    try:
        model = health_mod.probe_from_extra(list(extra_args), timeout=timeout)
    except Exception as exc:
        model = {
            "ok": False,
            "name": "model",
            "target": "",
            "detail": f"探活失败：{exc}",
        }
    payload = {"model": model}
    try:
        payload["services"] = list(health_mod.probe_services())
    except Exception:
        payload["services"] = []
    if extra_health is None:
        return payload
    try:
        extra = extra_health() or {}
    except Exception:
        return payload
    if isinstance(extra, dict):
        payload.update(extra)
    return payload


#: `NOISE_LINES` 里那几行怎么归到"模型那一步坏了"的哪一类。
#:
#: 只用来**分类**（页面据此给出可读说明），不做任何业务判断——循环该不该继续、
#: 要不要重试，都由内核自己的代码决定，跟这里的词一个关系都没有。
MODEL_FAILURE_MARKS = (
    ("connect", ("无法连接 llama-server", "ConnectError", "ConnectTimeout",
                 "Connection refused")),
    ("slow", ("秒内没返回", "ReadTimeout", "Read timed out")),
    ("server", ("服务返回", "tokenize 返回")),
)


def model_failure(extra_args: Sequence[str], noise: Sequence[str]) -> dict | None:
    """认出"连模型那一步失败了"，给出**结构化**的一条线索；认不出来给 None。

    为什么值得单独一条并返回字典：子进程是带着一段回溯死的，`final.text` 里只有
    "进程退出，退出码 1：<几行回溯>"——页面照原样显示出来就是一行原始异常，
    而人需要的是「哪一步 / 哪个地址 / 可能为什么 / 下一步敲什么」。
    这里**只增**：`final` 的既有字段与文字一个字都没动，只是多了一个 `error` 键
    （`{stage, url, kind}`），展示层（`page.py` 的 `explainFailure`）据此给出可读说明。

    `url` 就是这次运行生效的 `--base-url`（没有就给空串，页面退回探活给的地址）。
    地址里可能带查询串，页面显示前还会过一遍遮蔽（`redact`）。
    """
    text = "\n".join(str(line) for line in noise)
    for kind, marks in MODEL_FAILURE_MARKS:
        if any(mark in text for mark in marks):
            return {
                "stage": "model",
                "kind": kind,
                "url": health_mod.flag_from_extra(list(extra_args), "base-url"),
            }
    return None


def trace_line(event: Event) -> dict | None:
    """把一条事件翻成"给人回看的思考轨迹"里的一行；不值得记的给 None。

    为什么要这样一份轨迹：子进程的思考过程（第几步 / 调了什么工具 / 为什么失败）
    原先只以**瞬时**的界面行存在——页面刷新、切走再切回，那一轮的排错线索就没了。
    轨迹由**已经有的事件**推出来（不新造协议、不新造存储），封存进 Runner，
    随 `/state` 只增地交出去，页面据此画一个默认收起的「思考过程」。

    `diff` 单独给 None：那份正文可能有几百行，而且它已经作为待确认卡片有了自己的
    位置（`diffs` 字段一个字没动），塞进轨迹只会把它淹没。`start` 也不是一步。
    """
    kind = event.type
    data = event.data
    if kind == STEP:
        return {"type": STEP, "text": f"第 {data.get('n', '?')} 步"}
    if kind == TOOL:
        name = str(data.get("name") or "工具")
        ok = bool(data.get("ok"))
        if data.get("no_checker"):
            # 第三档状态（问题⑤）：`ok=True` 只是「没有发现问题」，不是
            # 「验过了」。渲染成「成功」会让用户读到一个不成立的通过。
            line = {
                "type": TOOL,
                "ok": ok,
                "text": f"{name} → 没有校验器（不是失败）",
            }
        else:
            line = {"type": TOOL, "ok": ok,
                    "text": f"{name} → {'成功' if ok else '失败'}"}
        detail = str(data.get("detail") or "").strip()
        # 成功那行只报状态，失败那行**必须带原因**：排错要的就是这一句。
        if detail and not ok:
            line["detail"] = detail
        return line
    if kind == NOTE:
        return {
            "type": NOTE,
            "ok": bool(data.get("ok", True)),
            "text": str(data.get("text") or ""),
        }
    if kind == WAIT:
        text = f"模型生成中，已等待 {data.get('elapsed', 0)}s"
        if data.get("limit"):
            text += f" / 上限 {data['limit']}s"
        return {"type": WAIT, "text": text}
    if kind == AWAIT:
        command = str(data.get("command") or "").strip()
        if command:
            return {"type": AWAIT, "text": f"等你确认：要执行 {command}"}
        return {"type": AWAIT, "text": f"等你确认：{data.get('count', 1)} 处改动"}
    if kind == CONFIRM:
        applied = bool(data.get("applied"))
        where = "（自动）" if data.get("auto") else ""
        return {
            "type": CONFIRM,
            "ok": applied,
            "text": f"{'已应用' if applied else '已拒绝'} {data.get('count', 0)} 处改动{where}",
        }
    if kind == ERROR:
        return {"type": ERROR, "ok": False, "text": str(data.get("message") or "")}
    if kind == FINAL:
        ok = bool(data.get("ok"))
        line = {"type": FINAL, "ok": ok, "text": str(data.get("text") or "")}
        clue = data.get("error")
        if isinstance(clue, dict):
            # **只增**地带走服务端那条结构化线索（`{stage, url, kind}`），
            # 页面才能把"哪一步/哪个地址/下一步敲什么"重新说一遍。
            line["error"] = {
                key: clue.get(key) for key in ("stage", "kind", "url") if clue.get(key)
            }
        if not ok and not line["text"]:
            line["text"] = "这一轮没有产出"
        return line
    return None


def attach_traces(messages: list[dict], turns: Sequence[dict]) -> None:
    """把每一轮的轨迹挂回**它那条回复**上（给每条消息**只增**一个 `trace` 键）。

    为什么按时间窗口认领而不是按正文比对：同一句话可能被跑两遍
    （"（没有产出）"这种正文更是每条都一样），按正文匹配会把轨迹挂错轮。
    一轮的窗口 `[started_at, ended_at]` 里落下的消息就是这一轮写的：子进程
    开工时写用户那条、收尾时写助手那条（见 `cli/commands/run.py`）。

    一轮**只挂一条**：有助手回复就挂它（回复卡片才是"排错要看的那张"），
    没有（进程被停掉 / 崩了，助手那条根本没写）就挂这一轮最后那条消息——
    宁可挂在用户那条上，也不能让这一轮的线索没有落脚点。

    挂不上的那些消息照旧有 `trace: []`：键一个不少，值就是"这一轮没有轨迹"
    （页面因此不必去猜"键缺了是因为没这个功能、还是因为没有值"）。
    """
    for item in messages:
        item["trace"] = []
    for turn in turns:
        start = float(turn.get("started_at") or 0.0)
        end = float(turn.get("ended_at") or 0.0)
        mine = []
        for item in messages:
            at = item.get("created_at")
            if at is None:
                continue
            if start <= float(at) <= end:
                mine.append(item)
        if not mine:
            continue
        holder = next(
            (item for item in reversed(mine) if item.get("role") == ASSISTANT),
            mine[-1],
        )
        holder["trace"] = [dict(line) for line in turn.get("lines") or []]


class Runner:
    """管理一次 agent 子进程的运行。"""

    def __init__(
        self,
        project_root: Path,
        session: str = "cli",
        extra_args: Sequence[str] = (),
        history_limit: int = HISTORY_LIMIT,
        extra_health=None,
    ) -> None:
        self.project_root = project_root
        self.session = session
        self.extra_args = list(extra_args)
        self.history_limit = history_limit
        self._extra_health = extra_health
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._listeners: list[queue.Queue] = []
        self._goal = ""
        self._awaiting = 0
        # 在等什么：等确认的 diff，还是等一条命令的授权。桥要据此把话说明白。
        self._await_detail: dict = {}
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
        # 这一轮的思考轨迹（`trace_line` 推出来的行），以及封存下来的前几轮。
        # 页面刷新 / 切走再切回时，排错要看的正是这两样（见 `attach_traces`）。
        self._trace: list[dict] = []
        self._turns: list[dict] = []
        self._turn_started = 0.0
        self._turn_open = False
        # 这一轮是不是被用户手动停掉的（`stop()` 里置位，`start()` 清位）。
        # `/state` 只增地带出去：页面据此说"已停止"，而不是把一次主动停止
        # 画成一次神秘失败。
        self._stopped = False

    # --- 命令 ---

    def command(self, goal: str) -> list[str]:
        """拼出要执行的命令行。子类可覆盖它来换掉被测进程。"""
        return [
            sys.executable,
            "-m",
            "spoolkit.cli.app",
            "run",
            "--events",
            # 网页壳有人可问（页面上的"应用/拒绝"按钮），所以允许在 stdin 上问授权。
            # 不带这个参数时 events 模式一律拒绝——那正是"它想跑一条白名单外的
            # 命令、被拒、反复重试、最后整轮收尾"的由来。
            "--ask-on-stdin",
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
            self._await_detail = {}
            self._usage = {}
            self._final = None
            self._finished = False
            self._noise = []
            self._diffs = []
            self._log = []
            self._log_seq = 0
            # 新的一轮：轨迹重新开始攒，上一轮已经由 `_seal_turn_locked` 封存
            # （刷新 / 切回时还能回看它），停止标记也要清掉——那是上一轮的事。
            self._trace = []
            self._turn_started = time.time()
            self._turn_open = True
            self._stopped = False
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

    def stop(self, timeout: float = STOP_WAIT) -> None:
        """收掉**自己拉起的**那个子进程。没在跑时是空操作。

        照 `cli/commands/hub.py::stop_children` 的做法：先 terminate（让它自己
        收尾），到点还不退就 kill。别人的进程这里一概不碰。

        语义是**温和停止**：给它机会把手上这一步收尾（`terminate` 是"请求退出"，
        不是 kill -9），已经写进库里的往来、已经发出来的事件**一条都不删**——
        停止只让这一轮到此为止，不做任何回滚。

        `_stopped` 只在"真的在跑"时置位：空闲时调它必须仍是空操作，否则
        `/state` 会替上一轮背一个它没干过的锅（`/run/stop` 那条路在调用前
        已经用 `running` 判过，这里再判一次是给嵌入代码留的护栏）。
        """
        with self._lock:
            process = self._process
            alive = process is not None and process.poll() is None
            if alive:
                self._stopped = True
        if process is None or not alive:
            return
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)

    def confirm(self, apply: bool) -> bool:
        """把用户的决定写进子进程的 stdin。"""
        return self.answer("y" if apply else "n")

    def answer(self, word: str) -> bool:
        """把用户的一句话**原样**写回子进程的 stdin。

        为什么需要它：命令授权有四档（本次允许 / 始终允许 / 拒绝 / 本轮全拒），
        而 `confirm()` 只有是/否两态。压成两态的结果是"始终允许"这个选项在
        聊天通道上根本用不了——用户想长期放开一条命令，只能去改文件。
        """
        with self._lock:
            if self._awaiting <= 0 or self._process is None:
                return False
            if self._process.stdin is None:
                return False
            self._process.stdin.write(f"{word}\n")
            self._process.stdin.flush()
            self._awaiting = 0
            return True

    def revert(self, run_id: str = "") -> tuple[int, str]:
        """把某一轮的成果退回去（网页壳上那个可点的「撤销这一轮」）。

        走的是与 `spool revert --run <id>` **同一份实现**（`runlog.revert_run`）：
        壳里再写一套回退逻辑，两条路的差异只会在最需要它的时候（用户反悔时）
        暴露出来。返回（退出码, 一句人话），与那条命令的退出码口径一致
        （0 = 全退回去了，非 0 = 没全退成）。

        `run_id` 留空时退**这一轮**（`final` 事件上那个字段）；两者都没有就
        如实说没有可退的，不猜一个 id 去撞。
        """
        from spoolkit import runlog

        rid = str(run_id or "").strip()
        if not rid:
            rid = str((self._final or {}).get("revert_run_id") or "").strip()
        if not rid:
            return 2, "没有可撤销的一轮：这一轮没有给出过回退命令。"
        result = runlog.revert_run(self.project_root, rid)
        if result is None:
            known = runlog.run_ids(self.project_root)
            hint = "现有的是：" + "、".join(known[:5]) if known else "还没有任何运行记录。"
            return 2, f"没有这条运行记录：{rid}。{hint}"
        done = result.count(runlog.DONE)
        conflict = result.count(runlog.CONFLICT)
        failed = result.count(runlog.FAILED)
        skipped = result.count(runlog.SKIPPED)
        message = (
            f"已撤销这一轮（{rid}）：共 {len(result.steps)} 处，"
            f"恢复/删除 {done}、跳过 {skipped}、冲突 {conflict}"
            + (f"、失败 {failed}" if failed else "")
            + "。"
        )
        if conflict:
            # **冲突不静默**（与终端那条路同一条规矩）：没被覆盖的那几处要
            # 逐条说出来，并给出人工怎么做。
            listed = "；".join(f"{step.path}（{step.detail}）" for step in result.conflicts)
            message += (
                "这几处**没有被覆盖**（它们在那一轮之后又被改过）："
                + listed
                + "。人工处理：先把现在的版本备份出去，再决定保留哪一版。"
            )
        return (3 if conflict else 0), message

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

    def snapshot(self, *, health: dict | None = None) -> dict:
        """当前状态。SSE 每次连接的首帧就是它。

        快照必须在锁内取完整的一份：分几次取的话，中间可能有事件进来，
        你会拿到一个自相矛盾的状态（比如「运行中」但已经给出了 final）。

        配对请求读磁盘、不进模型上下文：锁外读，别让文件 I/O 堵住确认按钮。
        探活走 HTTP，同样放锁外，失败也不得把状态接口弄挂。

        `health` 给定就**直接用**（网页壳的后台探针已经把结果算好了：那条路上
        一次请求不该等一口网络，见 `web/probe.py`）；不给就照老样子同步探一次
        ——`Runner` 单独用时（桥、MCP、测试）没有那个探针，那里也没有"点一下
        切换卡三秒"这回事。
        """
        with self._lock:
            final = dict(self._final) if self._final else None
            messages = self._transcript()
            # **只增**：每条消息多一个 `trace`（这一轮的思考轨迹，默认空表）。
            # 刷新 / 切走再切回时，页面靠它把「思考过程」重新画出来——那一轮停在
            # 哪一步、哪次调用为什么失败，是排错唯一需要的内容。
            attach_traces(messages, self._turns)
            data = {
                "running": self.running,
                "finished": self._finished,
                "awaiting": self._awaiting,
                "awaiting_detail": dict(self._await_detail),
                "goal": self._goal,
                "usage": dict(self._usage),
                "final": final,
                "diffs": [dict(item) for item in self._diffs],
                "session": self.session,
                "messages": messages,
                # 顶层别名：外部驱动者（MCP 客户端等）不读源码时靠直觉就能判断
                # 「是否结束」「是否成功」。finished/final 仍然保留，不破坏老接口。
                "done": self._finished,
                "ok": final.get("ok") if final else None,
                # **只增**：这一轮（正在跑或刚跑完）的轨迹 + 是不是被手动停掉的。
                # `open` 为真 = 还在跑：页面刷新之后据此把实时卡片（连同已经攒下的
                # 轨迹行）接着画出来，而不是等它跑完才看得见。
                "trace": {
                    "open": self.running,
                    "goal": self._goal,
                    "lines": [dict(item) for item in self._trace],
                },
                "stopped": self._stopped,
            }
        data["pairings"] = self._pending_pairings()
        data["health"] = self._health_snapshot(health)
        return data

    def brief(self) -> dict:
        """一行状态：给"别处还有哪些目标在跑"用（页面上的那条提示）。

        刻意**不是** `snapshot()`：这一行要的只是"是否在跑、在做什么"，而快照还要
        读历史库与配对文件——`Runner.snapshot()` 默认还会**发一次 HTTP** 探活
        （网页壳那边已经把探活挪到后台、给快照喂缓存，但直接用它时没有那个探针）。
        页面每几秒轮一次 `/state`，给每个在跑的 Runner 各整一份没有收益。
        """
        with self._lock:
            return {
                "running": self.running,
                "finished": self._finished,
                "awaiting": self._awaiting,
                "goal": self._goal,
            }

    def _health_snapshot(self, health: dict | None = None) -> dict:
        """模型（以及 hub 可选附带的通道）通不通。展示用的。

        给了 `health` 就原样用它（网页壳的后台探针给的那一份），否则同步探一次。
        """
        if health is not None:
            return health
        return health_snapshot(self.extra_args, self._extra_health)

    def _pending_pairings(self) -> list[dict]:
        """待批准的配对码。展示用的，不进模型上下文。"""
        pending = Pairings(
            self.project_root / ".agent" / "bridge-pairings.json"
        ).pending
        return [{"code": code, "user": user} for code, user in pending]

    def _transcript(self) -> list[dict]:
        """这个会话之前的往来——**展示用的**，不进入模型上下文。

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
                    # **只增**：写这条消息的墙钟。它给 `attach_traces` 用来认领
                    # "这条消息属于哪一轮"（按时间窗口，不按正文比对——同一句话
                    # 可能被跑两遍），页面上不显示它。
                    "created_at": float(row["created_at"]),
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
            line = trace_line(event)
            if line is not None:
                # 轨迹与事件留痕是两件事：留痕**有上限地留一段**给轮询的驱动者，
                # 轨迹是给人回看的（只留 `TRACE_LINES` 行，但会随轮次封存）。
                self._trace.append(line)
                del self._trace[:-TRACE_LINES]
            if event.type == AWAIT:
                self._awaiting = int(event.data.get("count", 1))
                self._await_detail = dict(event.data)
            elif event.type == DIFF:
                self._diffs.append(dict(event.data))
            elif event.type == CONFIRM:
                self._diffs = []
                self._await_detail = {}
            elif event.type == USAGE:
                self._usage = dict(event.data)
            elif event.type == FINAL:
                self._final = dict(event.data)
                self._awaiting = 0
                self._await_detail = {}
                self._diffs = []
                self._seal_turn_locked()

    def _seal_turn_locked(self) -> None:
        """把这一轮的轨迹封存下来（刷新 / 切走再切回时还要能回看）。持锁调用。

        幂等：`final` 事件之后子进程还会退出，`_pump` 也要收尾一次——不封两次，
        否则同一个窗口里会出现两条一模一样的轮次，`attach_traces` 认领时就分不清。
        """
        if not self._turn_open:
            return
        self._turn_open = False
        self._turns.append(
            {
                "goal": self._goal,
                "final": (self._final or {}).get("text", ""),
                "ok": (self._final or {}).get("ok"),
                "started_at": self._turn_started,
                "ended_at": time.time(),
                "lines": [dict(item) for item in self._trace],
            }
        )
        del self._turns[:-TRACE_TURNS]

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
            self._await_detail = {}
            code = process.returncode
            fallback = None
            if self._final is None:
                # 子进程没给结局就退出了（崩了、被杀了）。**必须补一条结局**，
                # 否则界面会永远停在「运行中」，而你会以为它还在干活。
                #
                # 补的这条要同时进快照，而且要和 finished 一起写：分两步写的话
                # 中间有个瞬间是「已完成、却没有结局」，那一刻恰好连上来的页面
                # 什么都显示不出来——既像跑完了又像没跑，比报错还难查。
                data = {"ok": False, "text": self._death_note(code)}
                clue = model_failure(self.extra_args, self._noise)
                if clue is not None:
                    # 只**增**一个键：既有字段与文字都没动。
                    data["error"] = clue
                fallback = Event(FINAL, data)
                self._final = dict(fallback.data)
            # 子进程收场就是这一轮的终点：即使它没发 `final`（崩了 / 被停掉），
            # 轨迹也要就地封存——**失败的那一轮正是最需要事后回看的那一轮**。
            # 时间窗口的右端取"进程真的走掉"这一刻：助手那条消息是在这之前写的。
            self._seal_turn_locked()
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
        """给一句能查下去的失败原因。调用方必须已持有锁。

        被用户手动停掉时**要说得出是停的**：这条补的结局会进快照、也会推到页面上，
        写成"进程退出，退出码 1"会让一次主动停止看起来像一次神秘崩溃（然后人去翻
        根本不存在的日志）。退出码照旧给出来，只是把原因说在最前面。
        """
        reason = "；".join(self._noise[-NOISE_LINES:])
        if self._stopped:
            head = f"你手动停止了这一轮（进程退出码 {code}）；已产出的内容保留"
            return f"{head}：{reason}" if reason else head
        head = f"进程退出，退出码 {code}"
        return f"{head}：{reason}" if reason else head
