"""MCP（Model Context Protocol）stdio 服务：让别的 agent 用它。

为什么不自己发明协议：**外部 agent 已经会说 MCP**。Codex、Claude Code、
Cursor 都能挂 MCP 服务，自己定一套 JSON 就得让对方改代码——那等于要求
用户先写适配层，这件事就不会发生。

传输是 stdio 上的 JSON-RPC 2.0，一行一条消息。这里只实现需要的部分：
`initialize` / `tools/list` / `tools/call`（外加忽略未知通知）——协议允许
服务端只提供子集，客户端也这么期待。

## 信任模型（必须说清楚）

外部 agent 在这里**扮演用户**：`confirm_changes(true)` 等于用户按了"应用"。
所以边界不设在协议层，设在**作用域**上：子进程仍旧按 `--policy` 与 `--scope`
跑，"自动落盘"只覆盖 scope 划定的范围，越界一样退回确认。
不放心就让外部 agent 只读（不调 confirm），或者把 scope 收窄到具体目录。

## 为什么任务是异步的

长任务可以跑几十分钟，而 MCP 的一次 `tools/call` 是请求/响应：把它做成同步，
调用方只能等或超时——两样都不对。所以 `delegate_task` 立刻返回 task_id，
之后用 `task_status` / `task_events` 轮询，用 `confirm_changes` 回答审批。
"""

import json
import sys
import threading
import queue
from pathlib import Path
from typing import Any, Callable, TextIO

from spoolkit import __version__, settings
from spoolkit.agents.plan import load_plan, plan_path
from spoolkit.kernel import sessions as kernel_sessions
from spoolkit.kernel import workspaces as kernel_workspaces
from spoolkit.kernel.target import Target, resolve_target
from spoolkit.kernel.workspaces import WorkspaceError
from spoolkit.policy import load_policy, policy_path
from spoolkit.web.runner import Runner

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "spool"


class WorkspaceRunner(Runner):
    """按调用方给的模式拼命令行的 Runner。

    `mode` 决定用哪条路径：普通任务 / 计划推进 / 自主编排。默认走普通任务——
    它是"把一件事交给它做完"，不需要调用方先了解我们的计划机制。
    """

    def __init__(self, *args, mode: str = "task", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.mode = mode

    def command(self, goal: str) -> list[str]:
        command = super().command(goal)
        if self.mode == "autonomous":
            # 自主编排要 scope，没有 scope 它自己会拒绝并说明原因——
            # 这里不替它编一个默认范围：那是安全边界，必须由调用方给。
            command.insert(command.index("--events") + 1, "--autonomous")
        elif self.mode == "plan":
            command.insert(command.index("--events") + 1, "--plan")
        return command


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "delegate_task",
        "description": (
            "把一件事交给本地 agent，在它的工作区里实施。立刻返回 task_id"
            "（长任务不会阻塞这次调用）；之后用 task_status / task_events 跟进。"
            "`workspace` 可选：用名字/序号/路径点名一个**已登记**的工作区"
            "（用 list_workspaces 先看有什么）；不给就用这台机器上 mcp 的指向，"
            "指向为空而登记表里有多个时会**报错要求点名**，不会替你挑一个。"
            "工作区只能由人建：没有 `.agent/` 的目录要让主人先 `spool ws add <目录>`。"
            "需要已配置真实模型（llamacpp 或 gemini）。"
            "fake 只回放 --script、不能写代码；没有 LLM 时不要调用本工具当兜底，"
            "改由调用方自己写。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "要做什么，写清楚验收标准"},
                "workspace": {
                    "type": "string",
                    "description": "已登记工作区的名字 / 序号 / 路径（可选）",
                },
                "session": {
                    "type": "string",
                    "description": "会话 id（可选）；不给就用这个工作区当前的会话",
                },
                "mode": {
                    "type": "string",
                    "enum": ["task", "autonomous", "plan"],
                    "description": (
                        "task=做完这一件事（默认）；autonomous=自己拆解并逐步做完"
                        "（需要先设好 scope）；plan=推进已有计划的下一个待办步骤"
                    ),
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "list_workspaces",
        "description": (
            "**只读**：列出这台机器上已登记的工作区（名字、别名、路径、"
            "是否已经是工作区），供你在 delegate_task 里点名。"
            "工作区只能由人建——要加一个，让主人在服务器上敲 `spool ws add <目录>`。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_sessions",
        "description": (
            "**只读**：列出某个工作区里的会话（id、标题、最近活跃、是否归档）。"
            "不给 `workspace` 就按这台机器上 mcp 的指向解析。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "工作区名字 / 序号 / 路径（可选）"}
            },
        },
    },
    {
        "name": "task_status",
        "description": "当前任务的快照：是否在跑、是否结束、结局、用量、待确认的改动。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "task_events",
        "description": (
            "按序号拉事件（步骤、工具调用、diff、结局）。带上一次看到的 last_seq，"
            "只拿新的；首次调用传 0。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"since": {"type": "integer", "description": "上次看到的最大 seq"}},
        },
    },
    {
        "name": "confirm_changes",
        "description": (
            "回答 agent 的审批请求：apply=true 落盘，false 丢弃。"
            "**这等于用户按了确认**——它只该在你看过 diffs 之后调用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"apply": {"type": "boolean"}},
            "required": ["apply"],
        },
    },
    {
        "name": "workspace_status",
        "description": (
            "工作区现状：计划进度、授权策略、上一次改动、当前供应商配置。"
            "若 provider 为 fake，当前不能生成代码，应改配 llamacpp 或 gemini，"
            "不要继续下发任务。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
)


class McpServer:
    """一条 JSON-RPC 会话。测试里可以直接喂 dict，不必起子进程。"""

    def __init__(
        self,
        project_root: Path | None,
        runner: Runner | None = None,
        writer: Any = None,
        *,
        mode: str = "task",
        extra_args: tuple[str, ...] = (),
    ) -> None:
        # `project_root` 为 None = "还没定"：每次调用给的 workspace，或这台机器上
        # mcp 的指向说了算。`runner` 给了就固定用它（老用法，也是测试注入假 Runner
        # 的路子）；不给就按 (工作区, 会话) 各建一个。
        self.project_root = project_root
        self.runner = runner
        self.writer = writer
        self.mode = mode
        self.extra_args = tuple(extra_args)
        self._runners: dict[str, Runner] = {}
        self._active: Runner | None = runner
        self._last_target: Target | None = None
        # 调用方给的进度令牌（tools/call 的 _meta.progressToken）。
        # 有它才推通知：没要进度还一直推，对方只是多收一堆噪音。
        self._progress_token: Any = None
        self._progress_count = 0
        self._forwarding = False

    # --- JSON-RPC ---

    def handle(self, message: dict) -> dict | None:
        """处理一条消息。通知（没有 id）返回 None。"""
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if request_id is None:
            return None  # 通知：不需要回话
        try:
            result = self._call(method, message.get("params") or {})
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成 JSON-RPC 错误
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _call(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [dict(item) for item in TOOLS]}
        if method == "tools/call":
            return self._call_tool(
                str(params.get("name") or ""), params.get("arguments") or {}
            )
        raise ValueError(f"不支持的方法: {method}")

    # --- 工具 ---

    def _call_tool(self, name: str, arguments: dict) -> dict:
        handlers: dict[str, Callable[[dict], dict]] = {
            "delegate_task": self._delegate,
            "task_status": lambda _a: self._status(),
            "task_events": self._events,
            "confirm_changes": self._confirm,
            "workspace_status": lambda _a: self._workspace(),
            "list_workspaces": lambda _a: self._list_workspaces(),
            "list_sessions": self._list_sessions,
        }
        handler = handlers.get(name)
        if handler is None:
            return _text(f"没有这个工具：{name}", is_error=True)
        return handler(arguments)

    # --- 目标与 Runner ---

    def _target(self, arguments: dict) -> Target:
        """这次调用在哪儿干活：调用方点名的 > 这条 MCP 会话钉住的 > mcp 指向。

        说不清时**抛 `WorkspaceError`**，由调用方那边翻成一句"要求点名"的报错——
        绝不替外部 agent 挑一个最近的。
        """
        named = str(arguments.get("workspace") or "").strip()
        workspace: Path | None
        if named:
            # 名字 / 序号 / 别名 / 路径都认（`resolve_workspace` 那四条规则）。
            # 不能直接把名字当路径丢给 `resolve_target`：`Path("查无此区")` 会
            # 变成本机下的一个路径，于是"点名一个不存在的工作区"变成悄悄新建。
            resolved = kernel_workspaces.resolve_workspace(named)
            if self.project_root is not None and resolved != Path(self.project_root).resolve():
                # `--root` 是主人给的**收紧手段**（MCP 这条入口没有身份级放行，
                # `--root` 就是唯一的范围限制），所以它必须真的压得住点名，
                # 否则"我钉住了它"是句空话。而且**既不静默照做、也不静默改道**：
                # 点名了 b 却在 a 里干活，比直接报错更坏。
                raise WorkspaceError(
                    f"这条 MCP 会话被启动参数 --root 钉在 {self.project_root}，"
                    f"不能点名 {resolved}。要按次点名，请让主人去掉 --root 再启动。"
                )
            workspace = resolved
        else:
            workspace = self.project_root
        return resolve_target(
            workspace=workspace,
            session=str(arguments.get("session") or "") or None,
            client="mcp",
            session_fallback="mcp",
            # 没给 `--root` 时**不看这个服务进程的 cwd**：它是被 MCP 客户端按配置
            # 拉起来的，"启动目录"跟调用方想在哪干活毫无关系。给了 `--root` 就已经
            # 是显式值，这一层用不上。说不清就报错要求点名，不猜。
            use_cwd=self.project_root is not None,
        )

    def _runner_for(self, target: Target) -> Runner:
        """一个 (工作区, 会话) 一个 Runner；换目标就换 Runner。

        "当前任务"跟着最近一次 delegate 走（`task_status` / `task_events` /
        `confirm_changes` 都作用在它上面）——与原先"一个 MCP 会话一个活跃任务"
        的语义一致，只是现在那个任务可能落在另一个工作区里。
        """
        if self.runner is not None:  # 注入了固定 runner（老用法 / 测试）
            self._active = self.runner
            self._last_target = target
            return self.runner
        key = f"{target.root}\u0000{target.session}"
        runner = self._runners.get(key)
        if runner is None:
            runner = WorkspaceRunner(
                target.root,
                session=target.session,
                extra_args=self.extra_args,
                mode=self.mode,
            )
            self._runners[key] = runner
        self._active = runner
        self._last_target = target
        return runner

    def _active_or_none(self) -> Runner | None:
        return self._active

    def _no_task(self) -> dict:
        return _text(
            "还没有任务。先调 delegate_task（可以带 workspace 点名工作区）。",
            is_error=True,
        )

    # --- 只读工具 ---

    def _list_workspaces(self) -> dict:
        rows = []
        for index, item in enumerate(kernel_workspaces.list_workspaces(), start=1):
            rows.append(
                {
                    "index": index,
                    "name": item.name,
                    "alias": item.alias,
                    "root": str(item.root),
                    "ready": (item.root / kernel_workspaces.MARK).is_dir(),
                }
            )
        return _json(
            {
                "workspaces": rows,
                "note": (
                    "工作区只能由人建：要加一个，让主人在服务器上敲 "
                    "`spool ws add <目录>`（`ready=false` 的还没有 .agent/）。"
                ),
            }
        )

    def _list_sessions(self, arguments: dict) -> dict:
        try:
            target = self._target(arguments)
        except WorkspaceError as exc:
            return _text(str(exc), is_error=True)
        items = kernel_sessions.list_sessions(target.root, include_archived=True)
        return _json(
            {
                "workspace": str(target.root),
                "current": target.session,
                "sessions": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "archived": item.archived,
                        "last_active": item.last_active,
                        "is_current": item.id == target.session,
                    }
                    for item in items
                ],
            }
        )

    def set_progress_token(self, token: Any) -> None:
        self._progress_token = token
        self._progress_count = 0

    def start_forwarding(self) -> None:
        """把子进程的事件转成 MCP 进度通知推给调用方。

        为什么是推送而不是只让它们轮询：轮询的代价是"要么慢、要么白问"——
        长任务里最需要的恰恰是"它现在走到哪了"。MCP 有进度通知这条路，
        就没理由让对方隔几秒问一次。
        """
        if self._forwarding or self.writer is None:
            return
        runner = self._active_or_none()
        if runner is None:
            return
        self._forwarding = True
        listener = runner.subscribe()

        def pump() -> None:
            while True:
                event = listener.get()
                token = self._progress_token
                if token is None:
                    continue
                self._progress_count += 1
                self.writer.line(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": token,
                            "progress": self._progress_count,
                            "message": _summarize(event),
                        },
                    }
                )

        threading.Thread(target=pump, daemon=True).start()

    def _delegate(self, arguments: dict) -> dict:
        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return _text("goal 不能为空", is_error=True)
        mode = str(arguments.get("mode") or "task")
        try:
            target = self._target(arguments)
        except WorkspaceError as exc:
            # 说不清是哪一个时**要求点名**，不要挑一个最近的替调用方决定。
            return _text(str(exc), is_error=True)
        runner = self._runner_for(target)
        if runner.running:
            return _text(
                "已经有一个任务在跑。先 task_status 看它，或者等它结束。",
                is_error=True,
            )
        runner.mode = mode
        # _meta 由调用方给（协议里就长这样）；有令牌就按进度推。
        meta = arguments.get("_meta") if isinstance(arguments, dict) else None
        if isinstance(meta, dict) and meta.get("progressToken") is not None:
            self.set_progress_token(meta["progressToken"])
            self.start_forwarding()
        started = runner.start(goal)
        if not started:  # pragma: no cover - 上面刚判过
            return _text("任务没能启动", is_error=True)
        return _json(
            {
                "task_id": 1,
                "goal": goal,
                "mode": mode,
                "workspace": str(target.root),
                "session": target.session,
                "status": "running",
                "hint": "用 task_status / task_events 跟进；需要审批时 status.awaiting > 0",
            }
        )

    def _status(self) -> dict:
        runner = self._active_or_none()
        if runner is None:
            return self._no_task()
        snapshot = runner.snapshot()
        snapshot.pop("messages", None)  # 聊天记录是给人看的，外部驱动者用不上
        with runner._lock:  # noqa: SLF001 - 同一个进程里的兄弟类，取序号够用
            snapshot["last_seq"] = runner._log_seq  # noqa: SLF001
        if self._last_target is not None:
            snapshot["workspace"] = str(self._last_target.root)
        return _json(snapshot)

    def _events(self, arguments: dict) -> dict:
        runner = self._active_or_none()
        if runner is None:
            return self._no_task()
        since = int(arguments.get("since") or 0)
        events = runner.events(since)
        last = events[-1]["seq"] if events else since
        return _json({"events": events, "last_seq": last})

    def _confirm(self, arguments: dict) -> dict:
        runner = self._active_or_none()
        if runner is None:
            return self._no_task()
        apply = bool(arguments.get("apply"))
        if not runner.confirm(apply):
            return _text("当前没有待确认的改动", is_error=True)
        return _text("已应用" if apply else "已丢弃")

    def _workspace(self) -> dict:
        runner = self._active_or_none()
        root = (
            self._last_target.root
            if self._last_target is not None
            else self.project_root
        )
        if root is None:
            return _text(
                "还没定在哪个工作区。先 delegate_task（带 workspace）或用 "
                "list_workspaces 看一眼。",
                is_error=True,
            )
        # 计划是**会话级**的（见 session_state.py）：这里读的必须是这个 runner
        # 那个会话的计划，否则外部 agent 看到的是别人的进度。
        session = getattr(runner, "session", "") or "mcp"
        plan_file = plan_path(root, session)
        plan = load_plan(plan_file) if plan_file.is_file() else None
        progress = None
        if plan is not None:
            done = [step for step in plan.steps if step.status == "done"]
            progress = {
                "steps": len(plan.steps),
                "done": len(done),
                "next": next(
                    (step.goal for step in plan.steps if step.status == "pending"), ""
                ),
            }
        provider, source = settings.resolve("provider")
        return _json(
            {
                "root": str(root),
                "session": session,
                # 工作区里**存着**的默认策略，不是这次运行实际用的那个
                # （后者由调用方用 --policy 给，从 start 事件里能看到）。
                "default_policy": load_policy(policy_path(root)),
                "plan": progress,
                "provider": {"name": provider, "source": source},
            }
        )


def _text(text: str, is_error: bool = False) -> dict:
    payload = {"content": [{"type": "text", "text": text}]}
    if is_error:
        payload["isError"] = True
    return payload


def _json(data: Any) -> dict:
    return _text(json.dumps(data, ensure_ascii=False, indent=2))


def _summarize(event: Any) -> str:
    """把一条内部事件折成一句给人看的话（进度通知的 message）。"""
    kind = getattr(event, "type", "")
    data = getattr(event, "data", {}) or {}
    if kind == "start":
        return f"开始：{data.get('goal', '')[:60]}"
    if kind == "step":
        return f"第 {data.get('n', '?')} 步"
    if kind == "tool":
        state = "成功" if data.get("ok") else "失败"
        return f"工具 {data.get('name', '?')} → {state}"
    if kind == "diff":
        return f"待确认改动：{data.get('path', '?')}"
    if kind == "await":
        return f"等待确认（{data.get('count', 1)} 处改动）——用 confirm_changes 回答"
    if kind == "confirm":
        return "改动已处理"
    if kind == "usage":
        return (
            f"用量：{data.get('steps', 0)} 步 / {data.get('calls', 0)} 次调用 / "
            f"{data.get('prompt_tokens', 0)}+{data.get('completion_tokens', 0)} token"
        )
    if kind == "final":
        return "结束：" + ("完成" if data.get("ok") else "未完成")
    if kind == "note":
        return str(data.get("text", ""))[:80]
    return kind or "事件"


class _LockedWriter:
    """多线程往同一路 stdout 写时的互斥。

    事件转发是在后台线程里发生的（子进程还在跑），而工具回话在主线程——
    不互斥的话两条 JSON 会**交错成一行**，对方的解析器只能报"格式错误"，
    而那种错误看起来像我们这边坏了。
    """

    def __init__(self, sink: TextIO) -> None:
        self._sink = sink
        self._lock = threading.Lock()

    def line(self, payload: dict) -> None:
        with self._lock:
            self._sink.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._sink.flush()


def serve_stdio(
    project_root: Path | None,
    runner: Runner | None = None,
    source: TextIO | None = None,
    sink: TextIO | None = None,
    *,
    mode: str = "task",
    extra_args: tuple[str, ...] = (),
) -> None:
    """从 stdin 读、往 stdout 写，一行一条 JSON-RPC。到 EOF 就结束。

    `runner` 不给就按每次调用的目标各建一个（`delegate_task` 里点名工作区那条路）。
    """
    reader = source if source is not None else sys.stdin
    writer = _LockedWriter(sink if sink is not None else sys.stdout)
    server = McpServer(
        project_root, runner, writer=writer, mode=mode, extra_args=tuple(extra_args)
    )
    for line in reader:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            # 不是 JSON 就忽略：stdout 上是协议，一行杂音不该让整条会话断掉。
            continue
        response = server.handle(message)
        if response is None:
            continue
        writer.line(response)
