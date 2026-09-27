"""HTTP 服务与 SSE 广播。

必须用 ThreadingHTTPServer：默认的单线程服务会让一个 SSE 长连接
把整个服务堵死——连首页都打不开，而你会以为服务崩了。

SSE 每写完一条都要 flush：不 flush 的话事件攒在缓冲里，
界面看起来像卡住。

机器级（不给 `--root`）时这个进程服务**整台机器**：它持有"当前目标"这个状态
（`WebState`）和一张按 (工作区, 会话) 复用的 Runner 表（`web/registry.py`）。
于是路由分成两类：打在**当前目标**上的（`/run`、`/confirm`），和改这个状态的
（`POST /target`）。切换**不杀任务**——在跑的那条留在它自己的 Runner 里继续跑
（设计 §3.4），切回去还能看到它的事件与历史。

`/events` 是长连接而目标可能在中途被换掉：服务端发现目标变了要**退订旧的、
订阅新的、补一帧 state**，否则页面会一直停在旧工作区的事件流上。这是这块最
容易写错的地方。
"""

import json
import queue
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

from spoolkit.health import describe_change, describe_service_change, probe_from_extra, probe_services
from spoolkit.kernel import sessions as kernel_sessions
from spoolkit.kernel import workspaces as kernel_workspaces
from spoolkit.kernel.sessions import SessionError
from spoolkit.kernel.target import Target, resolve_target
from spoolkit.kernel.workspaces import WorkspaceError
from spoolkit.web.page import HTML
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

# 机器级网页壳还没有当前目标时，`target.note` 要能告诉人下一步做什么。
NO_TARGET_HINT = "在页面上选一个工作区，或用一个 `--root` 起服务把它钉住。"

# `_stream` 里那个"还没订阅过"的哨兵：没有它，第一圈会以为"目标没变"而不订阅。
_UNSET = object()


def _empty_snapshot(extra_args: Sequence[str] = (), extra_health=None) -> dict:
    """还没有当前目标时的快照：字段一个不少，值全是空态。

    为什么要空态而不是少几个键：页面（以及外部驱动者）不该去猜"键缺了是因为
    没这个功能、还是因为现在没有值"。`health` 仍要探——那是"这台机器连不连得上
    模型"，与选了哪个工作区无关。
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
        "health": health_snapshot(extra_args, extra_health),
    }


class WebState:
    """这一个进程的"当前目标" + 一张 Runner 表。

    为什么单独一个对象持有它：handler 是**每个连接一个实例**（BaseHTTPRequestHandler
    的约定），而"现在在看哪个工作区、哪条会话"是整个进程的状态，必须共享。

    `version` 是给 SSE 用的：它一变就说明目标换了，那条长连接该退订旧的、订阅新的。
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

    # --- 读 ---

    def snapshot(self) -> dict:
        """给页面的完整状态：当前目标的快照 + 目标本身 + 有哪些可选 + 谁在跑。"""
        _version, runner = self.current()
        target = self.target()
        data = (
            runner.snapshot()
            if runner is not None
            else _empty_snapshot(self._extra_args, self._extra_health)
        )
        data["target"] = self._target_payload(target)
        data["workspaces"] = _workspace_rows(target)
        data["sessions"] = _session_rows(target)
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
        return 200, self.snapshot()

    def close(self) -> None:
        """收掉这张表里自己拉起的子进程。进程退出时调一次。"""
        with self._lock:
            fixed = self._fixed
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
            if length <= 0 or length > MAX_BODY:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

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
                            # 目标换了（第一次连上也算"换"）：**先退订旧的，再订阅新的**。
                            # 顺序不能反——先订阅的话，两条流会短暂地同时把事件推进
                            # 同一个页面，而页面会把它们当成同一段历史。
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
                            "error": "还没有选工作区：先在页面上选一个"
                            "（POST /target），再发任务。"
                        },
                    )
                    return
                if not runner.start(goal):
                    self._json(409, {"error": "已有运行在进行，等它结束再发"})
                    return
                self._json(202, {"ok": True})
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
            client="web",
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


def build_server(source: WebState | Runner, host: str, port: int) -> ThreadingHTTPServer:
    """构造服务。port 传 0 表示由系统分配。

    参数既可以是 `WebState`（机器级：一个 target 状态 + 一张表），也可以是
    单个 `Runner`（老用法：它被当成一个**钉住的**目标）。
    """
    return ThreadingHTTPServer((host, port), _handler_for(_as_state(source)))


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
    if not pinned:
        where = state.target()
        print(
            "机器级网页壳：目标是 "
            + (f"{where.root}（会话 {where.session}）" if where else "还没定，在页面上选"),
            file=sys.stderr,
        )
    stop = threading.Event()
    watcher = threading.Thread(
        target=_watch_health,
        args=(list(extra_args), extra_health, stop),
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


def _watch_health(
    extra_args: list[str],
    extra_health: Callable[[], dict] | None,
    stop: threading.Event,
) -> None:
    """通↔断时往终端打一行。页面横幅靠 /state，终端靠这一头。"""
    last_model: dict | None = None
    last_bridge: bool | None = None
    last_services: dict[str, bool] = {}
    while True:
        try:
            model = probe_from_extra(extra_args)
        except Exception as exc:
            model = {
                "ok": False,
                "target": "",
                "detail": f"探活失败：{exc}",
            }
        line = describe_change(last_model, model)
        if line:
            print(line, file=sys.stderr)
        last_model = model
        try:
            rows = probe_services()
        except Exception:
            rows = []
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
        if extra_health is not None:
            try:
                extra = extra_health() or {}
            except Exception:
                extra = {}
            bridge = extra.get("bridge") if isinstance(extra, dict) else None
            if isinstance(bridge, dict):
                alive = bool(bridge.get("ok"))
                if last_bridge is not None and alive != last_bridge:
                    target = str(bridge.get("target") or "").strip()
                    where = f"（{target}）" if target else ""
                    print(
                        ("通道在跑" if alive else "通道已退出") + where,
                        file=sys.stderr,
                    )
                last_bridge = alive
        if stop.wait(HEALTH_POLL_SECONDS):
            break
