"""HTTP 服务与 SSE 广播。

必须用 ThreadingHTTPServer：默认的单线程服务会让一个 SSE 长连接
把整个服务堵死——连首页都打不开，而你会以为服务崩了。

SSE 每写完一条都要 flush：不 flush 的话事件攒在缓冲里，
界面看起来像卡住。

机器级（不给 `--root`）时这个进程服务**整台机器**：它持有"当前目标"这个状态
（`WebState`）和一张按 (工作区, 会话) 复用的 Runner 表（`web/registry.py`）。
于是路由分成两类：打在**当前目标**上的（`/run`、`/run/stop`、`/confirm`），和改这个状态的
（`POST /target`、`POST /session/delete`、`POST /session/restore`、
`POST /session/new`）。切换**不杀任务**
——在跑的那条留在它自己的 Runner 里继续跑（设计 §3.4），切回去还能看到它的事件与历史。

`POST /run/stop` 是**温和停止**：复用 `Runner.stop()`（先 terminate 请它自己收尾、
到点才 kill），已经写进记忆库的往来与已经发出来的事件一条都不删；`/state` 里
`stopped` 与 `running`（只增的键）合起来说得出"这一轮是被停掉的"。空闲时停 → 409 说人话；
它和 `/run` 一样进 `WRITE_PATHS`，非回环绑定一律 403。

**探活不在请求路径上**（`web/probe.py`）：模型地址连不上时，一次探测要等到超时
（实测 `/state` 3419ms、`/target` 3398ms），而那跟"切个工作区"一点关系都没有。
所以探测由一条后台线程按固定节拍做，`/state` 只读**最近一次结果**，并把它
"是什么时候探的"（`probed_at` / `age_s`）一起交出去。线程挂在
`WebServer.server_close()` 上收（谁起的谁收，见 `WebServer` 的自述）。

会话的"移除"是**归档**（可恢复，`WebState::remove_session` 里有自述），不是真删：
`.agent/sessions/` 与 `memory.db` 里的记忆不该被网页上的一个按钮不可逆地销毁。
"新建"走**同一个** `kernel_sessions.create_session`（`WebState::create_session`），
不另写一套建法。

`/events` 是长连接而目标可能在中途被换掉：服务端发现目标变了要**退订旧的、
订阅新的、补一帧 state**，否则页面会一直停在旧工作区的事件流上。这是这块最
容易写错的地方。

页面上选一次还会**写进网页壳指针**（`clients.json` 的 `web` 那条）：不写的话，
刷新页面停得住、重启服务就回到内核顺位了（见 `WebState::_remember`）。

**非回环地址上只读**（设计 §3.7）：判据是**实际绑定的 host**（不是命令行那一层），
所以绑定地址一变，`/run` 与 `/confirm` 一律 403。判据放在这里而不是 CLI 里，是因为
CLI 不是唯一的入口——测试与嵌入代码直接 `serve(host=…)` / `build_server(…, host, …)`，
安全边界不能靠"大家都会走命令行"来成立。
"""

import ipaddress
import json
import queue
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

from spoolkit.health import (
    PROBE_TIMEOUT_SECONDS,
    describe_change,
    describe_service_change,
)
from spoolkit.kernel import pointers as kernel_pointers
from spoolkit.kernel import sessions as kernel_sessions
from spoolkit.kernel import workspaces as kernel_workspaces
from spoolkit.kernel.sessions import SessionError
from spoolkit.kernel.target import Target, resolve_target
from spoolkit.kernel.workspaces import WorkspaceError
from spoolkit.web.page import HTML
from spoolkit.web.probe import (
    HEALTH_INTERVAL_SECONDS,
    STATE_UNKNOWN,
    HealthProbe,
)
from spoolkit.web.registry import RunnerRegistry
from spoolkit.web.runner import Runner, health_snapshot

KEEPALIVE_SECONDS = 15
MAX_BODY = 64 * 1024
HEALTH_POLL_SECONDS = 5.0

# 事件流多久检查一次"目标换了没有"。
#
# 为什么不是拿 15 秒的心跳当检查周期：切换是人在页面上点一下，反馈要立刻；
# 用心跳当周期，页面会在**旧工作区的事件流**上待十几秒——那正是这个功能最
# 容易写错的地方（看起来切了，事件却还是别人的）。1 秒是"点完按钮基本感觉
# 不到"与"别把 CPU 空转"之间的折中：这一圈只读一个整数和一个引用。
TARGET_POLL_SECONDS = 1.0

#: 终端那一头**还没拿到第一份探活结果**时的等待节拍。
#:
#: 为什么不是直接按 `HEALTH_POLL_SECONDS`（5 秒）等：后台探针通常几百毫秒就有
#: 第一份结果（本机连不上时是它自己的超时，一秒），起手干等 5 秒会让"终端第一行"
#: 看起来像没反应。拿到结果之后立刻回到 5 秒的节拍。
FIRST_RESULT_POLL_SECONDS = 1.0

# 机器级网页壳还没有当前目标时，`target.note` 要能告诉人下一步做什么。
# 这句话会出现在 `/state` 的空态与启动提示里。**它必须与页面一致**：页面顶上那条
# 选择栏（工作区 / 会话两条下拉）就是"选一个工作区"的地方。改回"在页面上选"是
# 有言在先的——Task 3 当时只能写 `POST /target`，是因为选择栏还没接上。
# 另外要**给出路**：登记表一个工作区都没有时，下拉是空的，光说"选一个"是死路
# （评审逮到的），所以把"怎么造出候选"也写进去。
NO_TARGET_HINT = (
    "还没有选工作区：在页面顶上的选择栏里选一个"
    "（下拉是空的就先在工作区目录里敲 `spool init`），"
    "或用 `--root` 起服务把它钉住。"
)

#: 会**驱动**这台机器的端点。非回环绑定一律 403（设计 §3.7）。
#:
#: 新端点必须加进这张表：判据按路径写死在这里一次，漏一个就等于把安全边界
#: 漏在门外——而"远程只能看"这条边界是这一批唯一的硬承诺。
WRITE_PATHS = (
    "/run",
    "/run/stop",
    "/confirm",
    "/session/delete",
    "/session/restore",
    "/session/new",
)

#: 网页壳在 `clients.json` 里的键。与 `resolve_target(client=…)` 用的是同一套键，
#: 所以"页面上选的那个"就是内核顺位里"上次选的那个"（设计 §3.3）。
POINTER_CLIENT = "web"

# `_stream` 里那个"还没订阅过"的哨兵：没有它，第一圈会以为"目标没变"而不订阅。
_UNSET = object()

#: 非回环绑定时给人看的那句话（终端与 403 响应体共用同一句）。
#:
#: 为什么要说"要驱动它就在本机来"：这条壳只读是**故意的**（设计 §3.7：远程可写要
#: 单独一个开关，先想清楚鉴权与传输），所以不能只说"403"，得给出路——否则人只会
#: 以为壳坏了，然后去关掉整个安全检查。
REMOTE_READONLY_NOTE = (
    "只能看，不能驱动：这条壳绑在非回环地址上，本批不实现远程可写。"
    "要发任务 / 确认改动，就在本机开一个网页壳，或者把 --host 绑回 127.0.0.1。"
)


def is_loopback_host(host: str) -> bool:
    """这个绑定地址是不是**只有本机够得着**。

    为什么是一个单独的函数并单测它：这条判据是安全边界（非回环 = 只读），而它的
    输入是命令行 / 嵌入调用给的一个字符串，写法多得是——`127.0.0.1`、`::1`、
    `localhost`、`[::1]`、`LOCALHOST` 都得认；`0.0.0.0`、`::` 反而不算（它们是
    "所有网卡"，正好是最该收紧的那一种）。

    认不出来的名字（DNS 名、拼错的地址）**按远程办**：这里宁可错杀。反过来的
    代价是"我以为只是本机、其实全网可达"，那是这两种错里唯一不能接受的一种。
    所以这条函数不看 DNS、不发请求——解析结果还可能变，判据得是确定的。
    """
    text = str(host or "").strip().lower()
    if text.startswith("[") and text.endswith("]"):
        # `--host [::1]` 是常见写法（照 URL 里的写法来）。
        text = text[1:-1]
    text = text.rstrip(".")
    if not text:
        # 空串在 `HTTPServer` 里的意思是 INADDR_ANY（所有网卡），不是"本机"。
        return False
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _empty_snapshot(health: dict) -> dict:
    """还没有当前目标时的快照：字段一个不少，值全是空态。

    为什么要空态而不是少几个键：页面（以及外部驱动者）不该去猜"键缺了是因为
    没这个功能、还是因为现在没有值"。`health` 仍要给——那是"这台机器连不连得上
    模型"，与选了哪个工作区无关。它由调用方算好（网页壳上是**后台探针**的最近
    一次结果，见 `web/probe.py`），所以这个函数自己不探。
    """
    return {
        "running": False,
        "finished": False,
        "awaiting": 0,
        "awaiting_detail": {},
        "goal": "",
        "usage": {},
        "final": None,
        "diffs": [],
        "session": "",
        "messages": [],
        "done": False,
        "ok": None,
        "pairings": [],
        "health": health,
        # 空态也**字段一个不少**（`Runner.snapshot()` 那份里新加的只增字段）：
        # 页面不该去猜"键缺了是因为没这个功能、还是因为现在没有值"。
        "trace": {"open": False, "goal": "", "lines": []},
        "stopped": False,
    }


class WebState:
    """这一个进程的"当前目标" + 一张 Runner 表。

    为什么单独一个对象持有它：handler 是**每个连接一个实例**（BaseHTTPRequestHandler
    的约定），而"现在在看哪个工作区、哪条会话"是整个进程的状态，必须共享。

    `version` 是给 SSE 用的：它一变就说明目标换了，那条长连接该退订旧的、订阅新的。

    `_readonly`（非回环 = 只读）也挂在这上面：handler 是每个连接一个实例，而
    "这条壳绑在哪"同样是整个进程的属性。`build_server` 绑好端口后调
    `set_bound_host` 把它填上——**填的是实际绑定的地址**，不是命令行里那个字符串。
    """

    def __init__(
        self,
        registry: RunnerRegistry,
        target: Target | None = None,
        *,
        pinned: bool = False,
        note: str = "",
        default_session: str = "cli",
        fixed_runner: Runner | None = None,
        extra_args: Sequence[str] = (),
        extra_health: Callable[[], dict] | None = None,
    ) -> None:
        self._registry = registry
        self._target = target
        self._pinned = bool(pinned)
        self._note = str(note or "")
        self._default_session = str(default_session or "cli")
        # 老用法（`build_server(runner, …)`）只给一个 Runner：那时它就是这个
        # 进程唯一的目标，切换无意义。
        self._fixed = fixed_runner
        self._extra_args = tuple(extra_args)
        self._extra_health = extra_health
        self._lock = threading.Lock()
        self._version = 0
        # 后台探针（`build_server` 起，`WebServer.server_close` 收）。
        # 没有它时（`build_state(...).snapshot()` 这种直接调用）退回**同步**探一次：
        # 那种壳没有 HTTP 请求路径，也就没有"一次请求卡三秒"这回事，而
        # `Runner.snapshot()` 的既有口径（探活就在里面）一个字都不用动。
        self._probe: HealthProbe | None = None
        # 还没绑端口时（`build_state` 单独用、测试直接 `snapshot()`）当作 loopback：
        # 那时它还没有"够得着它的人"这件事，而 `build_server` 一定会紧接着把它填上。
        self._host = "127.0.0.1"
        self._readonly = False

    # --- 绑定地址与只读（设计 §3.7）---

    def set_bound_host(self, host: str) -> bool:
        """记下**实际绑定的**地址，返回这条壳是不是只读。

        传进来的必须是 `httpd.server_address[0]`（真正绑上去的那个），不是请求的
        那个字符串：`localhost` 到底落到 `127.0.0.1` 还是 `::1`、`0.0.0.0` 到底是不是
        真的全网可达，只有绑完才知道。
        """
        self._host = str(host or "")
        self._readonly = not is_loopback_host(self._host)
        return self._readonly

    def bound_host(self) -> str:
        return self._host

    def readonly(self) -> bool:
        """非回环绑定时为真：`/run` 与 `/confirm` 403，切换不落盘。"""
        return self._readonly

    # --- 当前目标 ---

    def current(self) -> tuple[int, Runner | None]:
        """（版本号, 当前目标的 Runner）。没有目标时 Runner 是 None。"""
        with self._lock:
            return self._version, self._runner_locked()

    def target(self) -> Target | None:
        with self._lock:
            return self._target

    def _runner_locked(self) -> Runner | None:
        if self._fixed is not None:
            return self._fixed
        if self._target is None:
            return None
        return self._registry.get(self._target.root, self._target.session)

    # --- 探活（后台，请求路径永不等它）---

    def start_probe(
        self,
        *,
        interval: float = HEALTH_INTERVAL_SECONDS,
        timeout: float = PROBE_TIMEOUT_SECONDS,
    ) -> HealthProbe:
        """起后台探针（幂等），返回它。

        为什么由 `WebState` 持有而**由服务收尾**：老用法（`build_server(runner, …)`）
        里那个 `WebState` 是 `build_server` 内部造的，调用方拿不到它，也就没人会
        去 `close()`——线程会一直陪到进程退出。服务的生命周期才是这条线程该有的
        边界，所以 `build_server` 拿到返回值之后把它挂到 `WebServer` 上
        （`server_close()` 里停）。
        """
        with self._lock:
            probe = self._probe
            if probe is None:
                probe = HealthProbe(
                    self._extra_args,
                    self._extra_health,
                    interval=interval,
                    timeout=timeout,
                )
                self._probe = probe
        probe.start()  # 幂等：同一个 state 起两个服务（测试里就有）也只探一次
        return probe

    def health(self) -> dict:
        """最近一次探活结果。**永不等探测**。

        没有后台探针时（测试 / 嵌入直接调 `snapshot()`）退回同步探一次：
        那里没有请求路径要保护，而"快照里带着模型红绿"这条既有口径更重要。
        """
        probe = self._probe
        if probe is not None:
            return probe.snapshot()
        return health_snapshot(self._extra_args, self._extra_health)

    # --- 读 ---

    def snapshot(self) -> dict:
        """给页面的完整状态：当前目标的快照 + 目标本身 + 有哪些可选 + 谁在跑。

        探活那一块由**后台探针**给（`health()`）：这里一次网络都不等，
        所以切工作区 / 会话的响应时间是几十毫秒，与模型通不通无关。
        """
        _version, runner = self.current()
        target = self.target()
        health = self.health()
        data = (
            runner.snapshot(health=health)
            if runner is not None
            else _empty_snapshot(health)
        )
        data["target"] = self._target_payload(target)
        data["workspaces"] = _workspace_rows(target)
        data["sessions"] = _session_rows(target)
        # 已移除（归档）的那几条**另开一个字段**：`sessions` 的口径一个字没动
        # （它仍然只列没归档的那些），而页面要能把删掉的放回来——"可恢复"如果
        # 在界面上够不着，那就不叫可恢复。
        data["sessions_removed"] = _removed_rows(target)
        # 只读要**说在状态里**，不能只体现在 403 上：页面得在点按钮之前就知道
        # "这里只能看"（点一下被拒是最后一秒才告诉人）。`host` 一起给，页面那句
        # 提示才说得出"绑在哪个地址上"。
        data["readonly"] = self._readonly
        data["host"] = self._host
        # 名字是 `running_targets` 而不是 `running`：`Runner.snapshot()` 里的
        # `running` 是**当前目标那一个**的布尔值，页面上的按钮直接读它
        # （`setRunning(state.running)`）。把它换成一张表＝改已有语义，
        # 而这里的规矩是**只增不改**。
        data["running_targets"] = self._running_rows(target)
        return data

    def _target_payload(self, target: Target | None) -> dict:
        """当前目标。没有目标时也给一份**形状相同**的空态。"""
        if target is None:
            return {
                "root": "",
                "session": "",
                "pinned": self._pinned,
                "workspace_source": "none",
                "session_source": "none",
                "note": self._note or f"还没有当前目标：{NO_TARGET_HINT}",
            }
        return {
            "root": str(target.root),
            "session": target.session,
            "pinned": self._pinned,
            "workspace_source": target.workspace_source,
            "session_source": target.session_source,
            "note": target.note,
        }

    def _running_rows(self, target: Target | None) -> list[dict]:
        """正在跑的 (工作区, 会话)。页面据此说"别处还有一条在跑"。

        老用法（只有一个钉住的 Runner）时这张表是空的——那种壳只有一个目标，
        `running` 那个布尔值已经把话说完了，`running_targets` 是给**机器级**
        准备的（它才可能同时有好几条）。
        """
        rows = self._registry.running_targets()
        for row in rows:
            row["current"] = bool(
                target is not None
                and row["root"] == str(target.root)
                and row["session"] == target.session
            )
        return rows

    # --- 写 ---

    def switch(self, workspace: str = "", session: str = "") -> tuple[int, dict]:
        """切当前目标，返回（HTTP 状态码, 响应体）。

        **不杀任务、不阻塞**（设计 §3.4）：在跑的那条留在它自己的
        (工作区, 会话) 里继续，这里只是改"页面在看谁"。
        """
        with self._lock:
            pinned = self._pinned
            prev = self._target
        if pinned:
            where = f"（钉在 {prev.root}）" if prev is not None else ""
            return 409, {
                "error": f"这条网页壳是用 --root 起的{where}，只有一个目标，切不了。"
                "去掉 --root 重启就是机器级。"
            }

        name = str(workspace or "").strip()
        if name:
            try:
                root = kernel_workspaces.resolve_workspace(name)
            except WorkspaceError as exc:
                # 名字对不上就**说人话**：抛回溯等于让页面显示一坨栈，
                # 而人需要的是"登记表里现在有这些"。
                return 400, {"error": str(exc)}
            workspace_source = "explicit"
            # 显式挑过的排到登记表最前面——与 `resolve_target` 对显式值的处理一致
            # （下次不带 `--root` 起服务时先看到的还是它）。写不进去不挡切换。
            #
            # **非回环时不写**：登记表和网页壳指针一样，是这台机器上**持久**的状态，
            # 而远程来的那一下只是"想看看那边"（见 `_remember` 里那条口径）。
            if not self._readonly:
                kernel_workspaces.register(root)
        else:
            if prev is None:
                return 400, {
                    "error": "还没有当前目标：body 里给一个 workspace"
                    "（名字 / 序号 / 路径）。有哪些看 GET /state 的 workspaces。"
                }
            root = prev.root
            workspace_source = prev.workspace_source

        wanted = str(session or "").strip()
        if wanted:
            try:
                picked = kernel_sessions.resolve_session(root, wanted)
            except SessionError as exc:
                return 400, {"error": str(exc)}
            session_source = "explicit"
        elif prev is not None:
            # 没点名会话就沿用当前那条的名字。会话是**按工作区**存的，同一个名字
            # 在新工作区里是另一份记忆——那是可预期的；替人挑一条"最近的"才是
            # 静默的猜测，而猜错的症状最难查（见 kernel/target.py 的自述）。
            picked = prev.session
            session_source = prev.session_source if prev.root == root else "default"
        else:
            picked = self._default_session
            session_source = "default"

        changed = Target(
            root=root,
            session=picked,
            workspace_source=workspace_source,
            session_source=session_source,
        )
        with self._lock:
            self._target = changed
            # 版本号一动，所有开着的事件流都会在下一圈发现自己订阅的是旧目标。
            self._version += 1
        # 只有**显式点名**才算"人选了它"。空 body 的一次 `POST /target` 什么都不改，
        # 也什么都不该记（`spool ws use` 同一个口径）。写指针不影响这次切换的结果。
        self._remember(changed, explicit=bool(name or wanted))
        return 200, self.snapshot()

    def create_session(self, name: str | None = None, *, title: str = "") -> tuple[int, dict]:
        """新建一条会话（`POST /session/new`），返回（状态码, 响应体）。

        走的是**既有那一条**建法（`kernel_sessions.create_session`）：存储位置
        （`.agent/sessions/<safe(id)>/meta.json` + 记忆库一行）、默认 id 形状
        （时间戳 + 两位随机）与 `spool session new` 完全一样。这里只决定
        "谁的名字"：`name` 给了就是**人点名**（要过校验、撞名要报错），
        不给就照既有约定自动生成。

        `name` 与"没给"分得开是刻意的：`{"session": ""}` 是**给了一个空名字**
        （400 说人话），而不是"你替我起一个"。

        **不写指针**：建一条会话不等于"选了它"——口径与 `POST /target` 的空 body
        一致（只有显式选择才落盘，见 `_remember`）。页面接着发的那次
        `POST /target {"session": <新名字>}` 才是那次"选了它"。

        钉住（`--root`）的壳在这里**照样能建**：它钉的是"看哪一条"，不是"什么
        都不能改"（与 `/session/delete` 对非当前会话的态度一致）；建完切不过去
        那件事由 `/target` 去说。
        """
        with self._lock:
            target = self._target
        if target is None:
            return 409, {"error": "还没有选工作区：先在左边选一个，再新建会话。"}
        wanted = None
        if name is not None:
            try:
                wanted = kernel_sessions.validate_session_name(name)
            except SessionError as exc:
                # 名字非法就说人话（与 `/target` 同一条口径），绝不悄悄改成别的名字：
                # 改掉之后页面上让人看到的名字与他敲的那个不一样，而目录已经建了。
                return 400, {"error": str(exc)}
        try:
            made = kernel_sessions.create_session(
                target.root, title=str(title or ""), session_id=wanted or ""
            )
        except kernel_sessions.SessionExists as exc:
            return 409, {"error": str(exc)}
        except SessionError as exc:
            return 400, {"error": str(exc)}
        except OSError as exc:
            # 磁盘满 / 状态目录没权限：不冒栈，给人话。
            return 500, {"error": f"会话没建起来（{target.root} 写不进去）：{exc}"}
        return 200, {"ok": True, "session": made, "title": str(title or "")}

    def remove_session(self, session: str, *, restore: bool = False) -> tuple[int, dict]:
        """把一条会话从列表里摘掉（归档）；`restore=True` 就是把它放回来。

        为什么是**归档**而不是真删：仓库里既有的就是 `archive_session`——标签写进
        `.agent/sessions/<id>/meta.json`，计划 / 进度 / 检查点 / 记忆一个字节都不动，
        随时能放回来（页面上的"已移除"那一段就是出口）。真删要动
        `.agent/sessions/` 与 `memory.db` 里的记忆，那是这台机器的底账，不该由
        网页上的一个按钮不可逆地销毁；`spool session` 这条既有命令也只到归档为止。

        正在跑的那一条**不许**移除：任务留在它自己的 Runner 里继续（切走不杀任务，
        设计 §3.4），把它的记录从列表里摘掉只会让人以为任务没了。

        移掉的正好是当前会话时：把目标挪到还在的下一条上（没有就退回启动时的默认
        会话名）。不挪的话指针会指着一条列表里没有的会话——"我在一条已移除的会话里"
        正是"列表与指针不一致"的样子。
        """
        with self._lock:
            target = self._target
            pinned = self._pinned
        if target is None:
            return 409, {"error": "还没有选工作区：先在左边选一个，再来管会话。"}
        raw = str(session or "").strip()
        if not raw:
            return 400, {"error": "缺少 session：要移除哪一条？"}
        try:
            picked = kernel_sessions.resolve_session(target.root, raw)
        except SessionError as exc:
            # 名字对不上就说人话（与 `POST /target` 同一条口径），绝不静默新建。
            return 400, {"error": str(exc)}
        if restore:
            kernel_sessions.archive_session(target.root, picked, archived=False)
            return 200, self.snapshot()
        running = any(
            row["root"] == str(target.root) and row["session"] == picked
            for row in self._registry.running_targets()
        )
        if running:
            return 409, {
                "error": f"会话 {picked} 正在跑，等它结束再移除"
                "（这个壳没有停止运行的接口）。"
            }
        if pinned and picked == target.session:
            return 409, {
                "error": "这条壳是用 --root 起的，钉的就是这一个目标："
                "移除当前会话就没有可看的会话了。"
                "先去终端换一条（`spool session use <id>`），或去掉 --root 重启。"
            }
        kernel_sessions.archive_session(target.root, picked)
        if picked == target.session:
            rest = kernel_sessions.list_sessions(target.root)
            self._retarget_session(
                target.root, self._fallback_session(rest, picked)
            )
        return 200, self.snapshot()

    def _fallback_session(self, rest: list, removed: str) -> str:
        """移掉**当前**会话之后退到哪一条。

        顺序：① 还在的下一条（最近活跃在前）；② 这条壳的默认会话名；③ `cli`
        （各入口一直以来的默认名）。一路跳过**刚被移除的那一个**——留在一个已移除
        的名字上就是"列表与指针不一致"。

        三条路都是它自己时（只有"把默认名本身移掉"才会出现）只能原地不动：那种
        名字仍然可用，下次真跑起来它会重新落进会话表，页面上它也一直在「已移除」
        那一段里，点「恢复」就回来。
        """
        for name in [item.id for item in rest] + [self._default_session, "cli"]:
            if name and name != removed:
                return name
        return removed

    def _retarget_session(self, root: Path, name: str) -> None:
        """只换会话名、不动工作区（移除当前会话之后的退路）。

        不复用 `switch()`：那条路会把名字交给 `resolve_session` 解析，而退路里的
        默认名（如 `cli`）**可能还没有任何会话记录**——在"为了不指向已移除的会话"
        而换名字这条路上，解析失败等于把状态卡在坏的地方。
        """
        with self._lock:
            prev = self._target
            self._target = Target(
                root=root,
                session=name,
                workspace_source=(
                    prev.workspace_source if prev is not None else "explicit"
                ),
                session_source="default",
            )
            self._version += 1
        self._remember(self._target, explicit=True)

    def _remember(self, target: Target, *, explicit: bool) -> None:
        """把这次显式选择写进网页壳指针（`clients.json` 里 `client="web"` 那条）。

        为什么要写：`WebState` 只在**进程内**，刷新页面停得住，**重启服务就回到内核
        顺位**了；而有多个工作区时，`register()` 的排序副作用（登记表里排第一）
        不足以让"上次选的那个"回来——登记表里排第一的可能是别的入口刚登记的。
        指针是内核顺位里跑在最前面的那一层，写它才等于"我上次在页面上选了它"。

        不写的情形：机器级启动时由内核顺位定出来的初始目标（那是内核按指针/登记表
        推断的，不是人选的结果），以及空 body 的 no-op 切换。

        **非回环（只读壳）也不写**（设计 §3.7）：这一条是"远程只能看"的落地点。
        `POST /target` 仍然放行——它改的只是"这个进程现在看谁"，不驱动任何东西
        （在跑的任务不会被它碰到）；但**落盘是另一回事**：指针会留到下次启动，
        等于让一个网络来的人改这台机器的持久状态，而本批没有任何鉴权来兜底。
        所以线划在"改进程内视图可以，改机器级持久状态不行"，而不是"整个 /target 拒掉"：
        拒掉的话远程连"看一眼别的工作区"都做不到，而那是这个功能存在的理由。

        写不进去**不能挡住切换**：目标已经在进程内生效了，这时报错等于告诉人
        "没切成"，而它其实是切成了。指针是便利，不是数据（`kernel/pointers.py` 的自述）。
        """
        if not explicit:
            return
        if self._readonly:
            return
        try:
            kernel_pointers.set(POINTER_CLIENT, target.root, target.session)
        except (OSError, ValueError) as exc:
            print(f"网页壳指针没写进去（切换已经生效）：{exc}", file=sys.stderr)

    def close(self) -> None:
        """收掉这张表里自己拉起的子进程 + 后台探活线程。进程退出时调一次。

        探针两种情况都要收（它跟 Runner 的归属无关：都是**这个进程**起的），
        而 Runner 表只在"表是自己的"时候收——老用法里那个 Runner 是调用方给的。
        """
        with self._lock:
            fixed = self._fixed
            probe = self._probe
        if probe is not None:
            probe.stop()
        if fixed is not None:
            # 老用法：Runner 是调用方给的，它的生命周期由调用方管（这里不动）。
            return
        self._registry.close()


def _workspace_rows(target: Target | None) -> list[dict]:
    """这台机器上登记过的工作区（名字 / 别名 / 路径 / 能不能干活）。"""
    current = str(target.root) if target is not None else ""
    rows: list[dict] = []
    for item in kernel_workspaces.list_workspaces():
        rows.append(
            {
                "name": item.name,
                "alias": item.alias,
                "root": str(item.root),
                "ready": (item.root / kernel_workspaces.MARK).is_dir(),
                "current": str(item.root) == current,
            }
        )
    return rows


def _session_rows(target: Target | None) -> list[dict]:
    """当前工作区的会话（id / 标题）。没有当前目标就是空的。

    注意当前会话**不一定在这张表里**：会话表按"跑过没有"记，像 `cli` 这种还没
    跑过的名字不会出现。页面要拿 `target.session` 当权威值，这张表只是备选。
    """
    if target is None:
        return []
    try:
        items = kernel_sessions.list_sessions(target.root)
    except Exception:  # noqa: BLE001 - 会话表读坏了不该把状态接口弄挂
        return []
    return [
        {
            "id": item.id,
            "title": item.title,
            "archived": item.archived,
            "last_active": item.last_active,
            "current": item.id == target.session,
        }
        for item in items
    ]


def _removed_rows(target: Target | None) -> list[dict]:
    """这个工作区里**已移除**（归档）的会话：id + 标题。

    页面拿它摆一段"已移除（N）— 点「恢复」放回来"。单独一个字段而不是把
    `sessions` 改成"连归档的一起给"：后者是本批明令不动的既有语义
    （会话列表一直只列没归档的）。
    """
    if target is None:
        return []
    try:
        items = kernel_sessions.list_sessions(target.root, include_archived=True)
    except Exception:  # noqa: BLE001 - 会话表读坏了不该把状态接口弄挂
        return []
    return [
        {"id": item.id, "title": item.title}
        for item in items
        if item.archived
    ]


def _handler_for(state: WebState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            """默认会往 stderr 刷每一行请求，本地调试时全是噪音。"""

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY:
                # 太大不收，但**要读完**：见 `_drain_body` 的理由。
                self._drain_body()
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

        def _drain_body(self) -> None:
            """把请求体读掉（丢弃），**回话之前**必须做。

            为什么这不是洁癖，两个后果都是真的：

            - 客户端用 keep-alive 时，**没读完的字节会被当成下一个请求的行**——
              第二个响应变成 http.server 自己发的 400/501，而不是我们发的那一个
              （报错还说"Bad request syntax"，看起来像客户端发了坏包）；
            - 客户端说 `Connection: close` 时（urllib 默认就是这样），带着没读完的
              接收数据去关 socket，Windows 发的是 **RST 不是 FIN**，客户端会在收
              那一口响应时随机拿到 `ConnectionAbortedError`——**随机**是关键：
              本批真踩过，403 那条路径原先没读 body，单跑那个文件全绿、跑整个
              `tests/web` 就红一条，症状与代码毫无关系的样子。

            读的字节数以 `Content-Length` 为准，分块丢弃：body 可以很大，不必整个
            装进内存。它撒谎（说得多、发得少）时 `read` 会一直等到客户端关连接——
            这一点与 `_body()` 原先的行为一致，没有变坏。
            """
            remaining = int(self.headers.get("Content-Length") or 0)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, MAX_BODY))
                if not chunk:
                    return
                remaining -= len(chunk)

        # --- GET ---

        def do_GET(self) -> None:  # noqa: N802 - 基类约定的方法名
            if self.path == "/":
                self._html(HTML)
            elif self.path == "/state":
                self._json(200, state.snapshot())
            elif self.path == "/events":
                self._stream()
            else:
                self._json(404, {"error": "没有这个路径"})

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            # 首帧给完整状态：EventSource 重连不会补发断线期间的事件，
            # 不给快照的话页面会停在断线前那一刻，而运行其实还在继续。
            runner: Runner | None = None
            listener: queue.Queue | None = None
            # 初值用一个"谁都不是"的哨兵：没有它，第一圈会以为"目标没变"而不订阅。
            subscribed: object = _UNSET
            # `switch()` 每次都会把版本号 +1，而**重订阅**的判据是 Runner 身份
            # （认错对象会订阅到别的目标上）。但"补一帧 state"要看版本号：否则
            # A→B→A 这种"一个周期里来回切"会既没换对象、也没补帧，页面就永远
            # 不知道目标动过（评审记的就是这条：`_version` 取了不用）。
            seen_version = -1
            last_write = time.monotonic()
            try:
                while True:
                    version, current = state.current()
                    if current is not subscribed or version != seen_version:
                        if current is not subscribed:
                            # 目标换了（第一次连上也算"换"）：先退订旧的，再订阅新的。
                            # ⚠️ **别把理由写成"反了就会两条流一起推同一个页面"**——本设计里
                            # 不可能：一条 `/events` 只有一个 `listener`，另一个订阅者的队列
                            # 没有读者（评审把这两步对调后用例照样全绿，即无可观测后果）。
                            # 真正的理由是卫生：不给"已经没人读的 Runner"继续攒订阅，
                            # 并保证 finally 里退掉的正是当前这一个。
                            if listener is not None and runner is not None:
                                runner.unsubscribe(listener)
                            runner = current
                            listener = runner.subscribe() if runner is not None else None
                            subscribed = current
                        seen_version = version
                        self._push({"type": "state", **state.snapshot()})
                        last_write = time.monotonic()
                        continue
                    if listener is not None:
                        try:
                            event = listener.get(timeout=TARGET_POLL_SECONDS)
                        except queue.Empty:
                            event = None
                        if event is not None:
                            self._push({"type": event.type, **event.data})
                            last_write = time.monotonic()
                    else:
                        # 还没有当前目标：等它被选出来（或者等这条连接断掉）。
                        time.sleep(TARGET_POLL_SECONDS)
                    if time.monotonic() - last_write >= KEEPALIVE_SECONDS:
                        # 心跳：代理和浏览器会掐掉长时间没数据的连接。
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_write = time.monotonic()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                if listener is not None and runner is not None:
                    runner.unsubscribe(listener)
                # 事件流一旦收场，这条连接就**明确关掉**（别留给基类去读"下一个请求"）。
                #
                # 这一句是踩出来的：客户端（urllib / 浏览器 / 测试）走人之后，服务端要到
                # 下次写才发现对方不在了（心跳 15 秒一次），而那时内核早已收到过 RST——
                # 基类接着去读下一个请求，就在 `rfile.readline` 上抛 ConnectionAbortedError，
                # 由 socketserver 打成一段回溯进 **stderr**：
                #
                #     Exception occurred during processing of request from ('127.0.0.1', …)
                #
                # 症状极难查：它出现在**十几秒之后、完全不相干的用例里**（本批真踩过：
                # 一个已经没人读的 `/events` 在 20 秒后把回溯打进了 pytest 的 stderr），
                # 而 `scripts/pytest.ps1` 见到子进程写 stderr 就当致命错误——整轮被掐掉。
                # SSE 本来就是一连接一条流（浏览器重连也是新开一条），关掉不吃亏。
                self.close_connection = True

        def _push(self, payload: dict) -> None:
            line = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
            self.wfile.flush()

        # --- POST ---

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/target":
                payload = self._body()
                status, body = state.switch(
                    str(payload.get("workspace") or ""),
                    str(payload.get("session") or ""),
                )
                self._json(status, body)
                return

            # 驱动这一台机器的端点：非回环绑定一律 403（设计 §3.7）。
            #
            # 为什么判在**这里**、而且排在最前面：安全边界不能"看 payload 再决定"。
            # 排在后头的话，一个远程请求会因为"缺少 goal"拿到 400——那说明它已经
            # 进到业务分支里了，而"远程不许驱动"这句话就不再是这条壳的性质。
            # 会话的移除 / 恢复也是"驱动"（它们改这台机器的持久状态），
            # 所以走**同一张** `WRITE_PATHS` 表，不另开一条判据。
            if self.path in WRITE_PATHS and state.readonly():
                # 请求体先读掉再回话：不读的话这条连接上会留下残字节（keep-alive 时
                # 被当成下一个请求），close 时还会触发 RST——见 `_drain_body`。
                self._drain_body()
                self._json(
                    403,
                    {
                        "error": f"{REMOTE_READONLY_NOTE}（这条壳绑在 "
                        f"{state.bound_host()} 上）",
                        "readonly": True,
                    },
                )
                return

            if self.path in ("/session/delete", "/session/restore"):
                payload = self._body()
                status, body = state.remove_session(
                    str(payload.get("session") or ""),
                    restore=self.path == "/session/restore",
                )
                self._json(status, body)
                return

            if self.path == "/session/new":
                payload = self._body()
                # `session` 这个键**在不在这**是有意义的：给了空串是"名字非法"（400），
                # 没给才是"你替我起一个"（照既有约定自动生成 id）。
                raw = payload.get("session")
                status, body = state.create_session(
                    None if raw is None else str(raw),
                    title=str(payload.get("title") or ""),
                )
                self._json(status, body)
                return

            if self.path == "/run":
                payload = self._body()
                goal = str(payload.get("goal") or "").strip()
                if not goal:
                    self._json(400, {"error": "缺少 goal"})
                    return
                _version, runner = state.current()
                if runner is None:
                    self._json(
                        409,
                        {
                            "error": "还没有选工作区：先在页面顶上的选择栏里选一个"
                            "（或 POST /target），再发任务。"
                        },
                    )
                    return
                if not runner.start(goal):
                    self._json(409, {"error": "已有运行在进行，等它结束再发"})
                    return
                self._json(202, {"ok": True})
                return

            if self.path == "/run/stop":
                # 温和停止这一轮：复用 `Runner.stop()`（terminate → 等它自己收尾 →
                # 到点才 kill），**不另写一套 kill**——停止语义只能有一处。
                #
                # 请求体照读不误（哪怕这里用不到）：不读的话这条 keep-alive 连接上
                # 会留下残字节，被当成下一个请求的行（见 `_drain_body` 的自述）。
                self._body()
                _version, runner = state.current()
                if runner is None:
                    self._json(
                        409,
                        {
                            "error": "还没有选工作区：没有在跑的任务，也就没有可停的。"
                            "先在页面顶上的选择栏里选一个（或 POST /target）。"
                        },
                    )
                    return
                if not runner.running:
                    # 空闲时说人话：点一下"停止"却什么都没发生时，人会以为壳坏了。
                    self._json(409, {"error": "现在没有在跑的任务，不用停。"})
                    return
                runner.stop()
                # 200 而不是 202：这一句回话的时候进程已经收掉了（`stop()` 会等它），
                # 所以"停了没有"有确定答案，不必让调用方再猜。
                self._json(200, {"ok": True, "stopped": True})
                return

            if self.path == "/confirm":
                payload = self._body()
                _version, runner = state.current()
                if runner is None:
                    self._json(
                        409,
                        {"error": "还没有选工作区，没有可确认的东西。"},
                    )
                    return
                if not runner.confirm(bool(payload.get("apply"))):
                    self._json(409, {"error": "当前没有待确认的改动"})
                    return
                self._json(200, {"ok": True})
                return

            self._json(404, {"error": "没有这个路径"})

    return Handler


def _as_state(source: WebState | Runner) -> WebState:
    """把老用法的单个 `Runner` 包成一个钉住的状态。

    老调用方（测试、嵌入）只构造一个 Runner 就起服务：那时它就是这个进程唯一的
    目标，`POST /target` 会被拒绝并说明原因——与机器级"只有一个目标"的表现一致。
    """
    if isinstance(source, WebState):
        return source
    runner = source
    return WebState(
        RunnerRegistry(lambda _root, _session: runner, limit=1),
        Target(
            root=runner.project_root,
            session=runner.session,
            workspace_source="explicit",
            session_source="explicit",
        ),
        pinned=True,
        fixed_runner=runner,
        extra_args=runner.extra_args,
    )


def build_state(
    project_root: Path | None = None,
    *,
    session: str = "",
    pinned: bool = False,
    target: Target | None = None,
    default_session: str = "cli",
    extra_args: Sequence[str] = (),
    extra_health: Callable[[], dict] | None = None,
) -> WebState:
    """按启动参数定出这个进程的初始目标。

    两种入口：

    - `pinned=True`（调用方给了 `--root`）：**只有一个目标**，老行为逐字保留；
      调用方若已经把来源解析清楚了（`Target` 里带着"谁给的"），原样收下——
      自己再解析一遍会把"这条会话来自指针"记成"显式给的"。
    - 机器级：初始目标走内核顺位（网页壳指针 → 登记表），**不认 cwd**；
      说不清就是"还没有目标"，把可选工作区清单交给页面，让人自己选
      （`resolve_target(use_cwd=False)` 在这种情形下是**抛错**的：
      内核不猜，这里要接住它，否则机器级网页壳根本起不来）。
    """
    args = tuple(extra_args)
    # **给了 `project_root` 就是"钉住这个工作区"**，不是"机器级的初始目标"。
    # 不这么归一的话，`build_state(root)` / `serve(root)` 会静默地忽略 root、去钉
    # 指针或登记表里的**另一个**工作区（评审实测：`serve(SA)` 的实际 target 是 SB，
    # 而且一声不响）——`serve(project_root)` 是这套壳最初的签名，它的意思不能被改掉。
    #
    # 判据只看 `project_root`，**不看 `target`**：早先写成 `and target is None` 时，
    # `build_state(root, target=T)` 与 `serve(root, target=T)` 会得出不同的 `pinned`
    # （复审记的就是这处不对称）。虽然当时没有调用点，但"同一组参数、两个入口两个结果"
    # 是后面一定会踩的坑。
    if project_root is not None:
        pinned = True
    if pinned and target is None and project_root is None:
        # 钉住却没给钉什么：这种组合没有意义，而"悄悄当成机器级"会让调用方以为
        # 自己钉住了。宁可当场说清楚（`serve_command` / `hub_command` 都不会这么调）。
        raise ValueError("pinned=True 必须给 project_root 或 target：钉住要有东西可钉。")
    registry = RunnerRegistry(
        lambda root, name: Runner(
            root,
            session=name,
            extra_args=list(args),
            extra_health=extra_health,
        )
    )
    if target is not None:
        return WebState(
            registry,
            target,
            pinned=pinned,
            default_session=target.session or default_session,
            extra_args=args,
            extra_health=extra_health,
        )
    if pinned and project_root is not None:
        return WebState(
            registry,
            Target(
                root=Path(project_root).resolve(),
                session=str(session or default_session),
                workspace_source="explicit",
                session_source="explicit" if session else "default",
            ),
            pinned=True,
            default_session=str(session or default_session),
            extra_args=args,
            extra_health=extra_health,
        )
    try:
        found = resolve_target(
            session=str(session or "") or None,
            client=POINTER_CLIENT,
            session_fallback=default_session,
            use_cwd=False,
        )
    except WorkspaceError as exc:
        return WebState(
            registry,
            None,
            pinned=False,
            note=f"还没有当前目标：{exc} {NO_TARGET_HINT}",
            default_session=str(session or default_session),
            extra_args=args,
            extra_health=extra_health,
        )
    return WebState(
        registry,
        found,
        pinned=False,
        default_session=found.session or default_session,
        extra_args=args,
        extra_health=extra_health,
    )


class WebServer(ThreadingHTTPServer):
    """网页壳的 HTTP 服务：关掉它的时候，后台探活线程也要收掉。

    为什么线程挂在这里而不是 `WebState`：老用法（`build_server(runner, …)`）里
    那个 `WebState` 是 `build_server` 内部造的，调用方拿不到它，也就没人会去
    `close()`——线程会一直陪到进程退出。**服务的生命周期才是这条线程该有的边界**，
    而这条边界只有一个入口：`server_close()`（`serve()` 的 finally 与用例的
    teardown 都走它）。
    """

    #: 这个服务用的后台探针（`build_server` 填）。没有探针也能关。
    probe: HealthProbe | None = None

    def server_close(self) -> None:
        try:
            if self.probe is not None:
                self.probe.stop()
        finally:
            super().server_close()


def build_server(source: WebState | Runner, host: str, port: int) -> WebServer:
    """构造服务。port 传 0 表示由系统分配。

    参数既可以是 `WebState`（机器级：一个 target 状态 + 一张表），也可以是
    单个 `Runner`（老用法：它被当成一个**钉住的**目标）。

    只读判据在**绑好端口之后**用实际绑上的地址定（`server_address[0]`）：
    `localhost` 落到哪个协议栈由系统决定，`0.0.0.0` 是不是真全网可达也只有绑完
    才知道。判据不能放在 CLI 那一层——测试与嵌入代码直接调这里。

    后台探活**在绑好端口之后**才起：绑不上（端口被占、地址不认）时不该留下一条
    没人收的线程。它挂在这个服务上，`server_close()` 里收（见 `WebServer`）。
    """
    state = _as_state(source)
    httpd = WebServer((host, port), _handler_for(state))
    address = getattr(httpd, "server_address", None)
    bound = address[0] if isinstance(address, tuple) and address else host
    state.set_bound_host(str(bound))
    httpd.probe = state.start_probe()
    return httpd


def serve(
    project_root: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    session: str = "",
    extra_args: Sequence[str] = (),
    extra_health: Callable[[], dict] | None = None,
    pinned: bool = False,
    target: Target | None = None,
) -> None:
    """启动服务，直到被中断。

    `project_root=None` 且 `pinned=False` = 机器级：初始目标由内核顺位定，
    定不出来就先空着（页面来选），**不看这个进程的 cwd**。

    `target` 可选：调用方已经把目标连"来源说明"一起解析好了就原样给过来，
    免得这里再解析一遍、把来源记错（`serve_command` 走这条）。

    `host` 给了非回环地址时这条壳**只读**（设计 §3.7）：起手就往终端说明"只能看"，
    具体拦在 handler 里（判据是实际绑定的地址，见 `build_server`）。
    """
    # 与 `build_state` 里同一条归一（那里也有一份，因为它是个公开入口）：
    # 给了 `project_root` 就是钉住它。这里还要这份，是因为下面那句提示语
    # 直接读 `pinned`——不归一的话，`serve(root)` 会去钉别的目标、终端还印着
    # "机器级网页壳"，两句话都在骗人。
    pinned = pinned or project_root is not None
    state = build_state(
        project_root,
        session=session,
        pinned=pinned,
        target=target,
        extra_args=extra_args,
        extra_health=extra_health,
    )
    httpd = build_server(state, host, port)
    print(f"Web UI: http://{host}:{httpd.server_port}/")
    if state.readonly():
        # 起手就说，别让人自己发现：绑了非回环地址之后，页面上那个「开始」按钮
        # 点下去只会得到 403，而"为什么"不该等踩到才知道（设计 §3.7）。
        print(
            f"绑在 {state.bound_host()} 上（非回环）：{REMOTE_READONLY_NOTE}",
            file=sys.stderr,
        )
    if not pinned:
        where = state.target()
        print(
            "机器级网页壳：目标是 "
            + (f"{where.root}（会话 {where.session}）" if where else "还没定，在页面上选"),
            file=sys.stderr,
        )
    stop = threading.Event()
    # 终端那一行读的是**同一个后台探针**的结果（见 `_watch_health` 的自述）：
    # 两个线程各探一次同一个地址只是白花力气，而且两边可能暂时不一致。
    watcher = threading.Thread(
        target=_watch_health,
        args=(getattr(httpd, "probe", None), stop),
        daemon=True,
        name="spool-health",
    )
    watcher.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        # 退出时把自己拉起的子进程收干净（照 `hub.stop_children` 的做法）：
        # 机器级壳同时可能有好几个目标在跑，留一堆孤儿进程比什么都糟。
        state.close()


def _watch_health(probe: HealthProbe | None, stop: threading.Event) -> None:
    """通↔断时往终端打一行。页面横幅靠 /state，终端靠这一头。

    读的是**后台探针已经拿到的**结果（`probe.snapshot()`），不自己再探一遍：
    探活是发 HTTP，两个线程各打一次同一个地址只是白花力气，而且两边的结论
    可能暂时不一样（终端说通、页面说断——同一条壳上两种说法，最难查）。

    `probe` 是 None 时（调用方自制的 `build_server`：测试与嵌入）这一头就闭嘴：
    页面横幅仍然由 `/state` 给，那条路不依赖这里。
    """
    if probe is None:
        return
    last_model: dict | None = None
    last_bridge: bool | None = None
    last_services: dict[str, bool] = {}
    while True:
        payload = probe.snapshot()
        state_name = str(payload.get("state") or STATE_UNKNOWN)
        if state_name == STATE_UNKNOWN:
            # 还没探过：不报"不通"，那是假话（页面上那一档会说"还没探过"）。
            pass
        else:
            model = payload.get("model")
            if isinstance(model, dict):
                line = describe_change(last_model, model)
                if line:
                    print(line, file=sys.stderr)
                last_model = dict(model)
        rows = payload.get("services")
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            name = str(row.get("name") or "")
            if not name:
                continue
            prev = last_services.get(name)
            last_services[name] = bool(row.get("ok"))
            note = describe_service_change(
                None if prev is None else {"ok": prev},
                row,
            )
            if note:
                print(note, file=sys.stderr)
        bridge = payload.get("bridge")
        if isinstance(bridge, dict):
            alive = bool(bridge.get("ok"))
            if last_bridge is not None and alive != last_bridge:
                where = str(bridge.get("target") or "").strip()
                place = f"（{where}）" if where else ""
                print(("通道在跑" if alive else "通道已退出") + place, file=sys.stderr)
            last_bridge = alive
        # 还没拿到第一份结果时用**短**节拍等它（后台探针通常几百毫秒就有，
        # 本机连不上时是它的超时）；有了结果再回到 `HEALTH_POLL_SECONDS`。
        # 不这么分的话，"终端起手那一行"会晚最多 5 秒才出现。
        delay = (
            HEALTH_POLL_SECONDS
            if state_name != STATE_UNKNOWN
            else FIRST_RESULT_POLL_SECONDS
        )
        if stop.wait(delay):
            break
