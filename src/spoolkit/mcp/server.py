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
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "要做什么，写清楚验收标准"},
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
        "description": "工作区现状：计划进度、授权策略、上一次改动、当前供应商配置。",
        "inputSchema": {"type": "object", "properties": {}},
    },
)


class McpServer:
    """一条 JSON-RPC 会话。测试里可以直接喂 dict，不必起子进程。"""

    def __init__(
        self,
        project_root: Path,
        runner: Runner,
        writer: Any = None,
    ) -> None:
        self.project_root = project_root
        self.runner = runner
        self.writer = writer
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
        }
        handler = handlers.get(name)
        if handler is None:
            return _text(f"没有这个工具：{name}", is_error=True)
        return handler(arguments)

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
        self._forwarding = True
        listener = self.runner.subscribe()

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
        if self.runner.running:
            return _text(
                "已经有一个任务在跑。先 task_status 看它，或者等它结束。",
                is_error=True,
            )
        self.runner.mode = mode
        # _meta 由调用方给（协议里就长这样）；有令牌就按进度推。
        meta = arguments.get("_meta") if isinstance(arguments, dict) else None
        if isinstance(meta, dict) and meta.get("progressToken") is not None:
            self.set_progress_token(meta["progressToken"])
            self.start_forwarding()
        started = self.runner.start(goal)
        if not started:  # pragma: no cover - 上面刚判过
            return _text("任务没能启动", is_error=True)
        return _json(
            {
                "task_id": 1,
                "goal": goal,
                "mode": mode,
                "status": "running",
                "hint": "用 task_status / task_events 跟进；需要审批时 status.awaiting > 0",
            }
        )

    def _status(self) -> dict:
        snapshot = self.runner.snapshot()
        snapshot.pop("messages", None)  # 聊天记录是给人看的，外部驱动者用不上
        with self.runner._lock:  # noqa: SLF001 - 同一个进程里的兄弟类，取序号够用
            snapshot["last_seq"] = self.runner._log_seq  # noqa: SLF001
        return _json(snapshot)

    def _events(self, arguments: dict) -> dict:
        since = int(arguments.get("since") or 0)
        events = self.runner.events(since)
        last = events[-1]["seq"] if events else since
        return _json({"events": events, "last_seq": last})

    def _confirm(self, arguments: dict) -> dict:
        apply = bool(arguments.get("apply"))
        if not self.runner.confirm(apply):
            return _text("当前没有待确认的改动", is_error=True)
        return _text("已应用" if apply else "已丢弃")

    def _workspace(self) -> dict:
        plan_file = plan_path(self.project_root)
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
                "root": str(self.project_root),
                # 工作区里**存着**的默认策略，不是这次运行实际用的那个
                # （后者由调用方用 --policy 给，从 start 事件里能看到）。
                "default_policy": load_policy(policy_path(self.project_root)),
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
    project_root: Path,
    runner: Runner,
    source: TextIO | None = None,
    sink: TextIO | None = None,
) -> None:
    """从 stdin 读、往 stdout 写，一行一条 JSON-RPC。到 EOF 就结束。"""
    reader = source if source is not None else sys.stdin
    writer = _LockedWriter(sink if sink is not None else sys.stdout)
    server = McpServer(project_root, runner, writer=writer)
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
