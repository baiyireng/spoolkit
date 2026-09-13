"""MCP 服务：别的 agent 眼里的接口。

为什么用一套**假 Runner** 而不是真起子进程：这里要量的是协议与判定
（谁校验、什么时候报错、事件怎么按序号取），而真跑一个 agent 会把
这些断言埋在几十秒的等待里。端到端那一条另有冒烟脚本（见 README）。
"""

import io
import json
from pathlib import Path

from agents_dev.agents.plan import DONE, parse_plan, plan_path, save_plan
from agents_dev.mcp.server import McpServer, serve_stdio


class 假Runner:
    """只实现 MCP 用到的那几样。"""

    def __init__(self) -> None:
        self.running = False
        self.mode = "task"
        self.started: list[str] = []
        self.confirmed: list[bool] = []
        self._events: list[dict] = []
        self._log_seq = 0
        self._lock = __import__("threading").Lock()

    def start(self, goal: str) -> bool:
        if self.running:
            return False
        self.started.append(goal)
        self.running = True
        return True

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "finished": not self.running,
            "awaiting": 0,
            "goal": self.started[-1] if self.started else "",
            "usage": {"steps": 1},
            "final": {"ok": True, "text": "做完了"},
            "diffs": [],
            "session": "t",
            "messages": [{"role": "user", "content": "不该出现在这里"}],
        }

    def events(self, since: int = 0) -> list[dict]:
        return [item for item in self._events if item["seq"] > since]

    def confirm(self, apply: bool) -> bool:
        self.confirmed.append(apply)
        return True

    def push(self, type_: str, **data) -> None:
        self._log_seq += 1
        self._events.append({"seq": self._log_seq, "type": type_, **data})


def _server(tmp_path: Path) -> tuple[McpServer, 假Runner]:
    runner = 假Runner()
    return McpServer(tmp_path, runner), runner


def _call(server: McpServer, name: str, arguments: dict | None = None) -> dict:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )
    return response["result"]


def test_initialize_报出协议版本与服务名(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    result = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )["result"]
    assert result["protocolVersion"]
    assert result["serverInfo"]["name"] == "agents-dev"
    assert "tools" in result["capabilities"]


def test_工具清单带_schema(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    tools = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )["result"]["tools"]
    names = {item["name"] for item in tools}
    assert names == {
        "delegate_task",
        "task_status",
        "task_events",
        "confirm_changes",
        "workspace_status",
    }
    for item in tools:
        assert item["description"]
        assert item["inputSchema"]["type"] == "object"


def test_通知不回话(tmp_path: Path) -> None:
    """没有 id 的是通知（比如 initialized），回话会打乱对方的会话。"""
    server, _ = _server(tmp_path)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_未知方法变成错误而不是崩(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})
    assert "error" in response
    assert "nope" in response["error"]["message"]


def test_delegate_空目标被拒(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    result = _call(server, "delegate_task", {"goal": "  "})
    assert result["isError"] is True
    assert "goal" in result["content"][0]["text"]


def test_delegate_忙碌时拒绝而不是排队(tmp_path: Path) -> None:
    server, runner = _server(tmp_path)
    _call(server, "delegate_task", {"goal": "第一件"})
    second = _call(server, "delegate_task", {"goal": "第二件"})
    assert second["isError"] is True
    assert "已经有一个任务在跑" in second["content"][0]["text"]
    assert runner.started == ["第一件"]


def test_delegate_把模式传下去(tmp_path: Path) -> None:
    server, runner = _server(tmp_path)
    _call(server, "delegate_task", {"goal": "大活", "mode": "autonomous"})
    assert runner.mode == "autonomous"


def test_status_不带聊天记录(tmp_path: Path) -> None:
    """聊天记录是给人看的；外部驱动者要的是结局与用量。"""
    server, _ = _server(tmp_path)
    payload = json.loads(_call(server, "task_status")["content"][0]["text"])
    assert "messages" not in payload
    assert payload["goal"] == ""
    assert payload["last_seq"] == 0


def test_events_按序号取(tmp_path: Path) -> None:
    server, runner = _server(tmp_path)
    runner.push("step", n=1)
    runner.push("tool", name="read_file")
    runner.push("final", ok=True, text="好了")
    payload = json.loads(_call(server, "task_events", {"since": 1})["content"][0]["text"])
    assert [item["type"] for item in payload["events"]] == ["tool", "final"]
    assert payload["last_seq"] == 3
    # 再拉一次不该重复拿到旧事件
    again = json.loads(
        _call(server, "task_events", {"since": payload["last_seq"]})["content"][0]["text"]
    )
    assert again["events"] == []


def test_confirm_把决定传给子进程(tmp_path: Path) -> None:
    server, runner = _server(tmp_path)
    _call(server, "confirm_changes", {"apply": True})
    _call(server, "confirm_changes", {"apply": False})
    assert runner.confirmed == [True, False]


def test_workspace_status_报计划进度(tmp_path: Path) -> None:
    plan = parse_plan(
        json.dumps(
            {
                "steps": [
                    {"goal": "甲", "acceptance": "a", "scope": ["x"], "executor": "self"},
                    {"goal": "乙", "acceptance": "b", "scope": ["y"], "executor": "self"},
                ]
            },
            ensure_ascii=False,
        ),
        "目标",
    )
    plan.steps[0].status = DONE
    save_plan(plan_path(tmp_path), plan)

    server, _ = _server(tmp_path)
    payload = json.loads(_call(server, "workspace_status")["content"][0]["text"])
    assert payload["plan"] == {"steps": 2, "done": 1, "next": "乙"}
    assert "default_policy" in payload


def test_stdio_一行一条_杂音被忽略(tmp_path: Path) -> None:
    server_runner = 假Runner()
    stdin = io.StringIO(
        "这是杂音，不是 JSON\n"
        + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    stdout = io.StringIO()

    serve_stdio(tmp_path, server_runner, source=stdin, sink=stdout)

    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [item["id"] for item in lines] == [1, 2]
    assert lines[1]["result"]["tools"]
