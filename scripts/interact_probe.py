"""互动面探针：走 `spool mcp`（stdio JSON-RPC）驱动一次真实运行，逐字留档。

它扮演的是"外部 agent / 网页壳的驱动者"：起 `spool mcp` 子进程 → `initialize`
→ `tools/list` → `tools/call delegate_task` → 轮询 `task_status` / `task_events`
→ 遇待批就用 `confirm_changes` 回答 → 到结局为止。**调用方实际收到的每一段文本
都逐字写进快照**（工具回话、进度通知、审批请求、结局报告），快照就是取证材料。

为什么用 MCP 这条路取证：网页壳与 MCP 是**同一条结算路径**——
`web/runner.py::Runner.command()` 拼出的子进程命令行（`run --events --ask-on-stdin`）
与 `mcp/server.py::WorkspaceRunner` 完全一样，收尾报告由子进程里的
`agent/loop.py::_collect_report` 产出。所以"MCP 里看到什么文本"就是"网页壳里
看到什么文本"（视觉渲染与按钮除外，那两样 MCP 看不到）。

## 沙箱化（绝不动仓库里被跟踪的文件）

每次跑都在**独立副本目录**里：`<工作区根>/.probe_runs/<run_id>/ws/`。副本里只写
四样东西（README.md / tool.py / data/small.log / .agent/），跑完整个 `ws/` 原样
留着（取证材料），`.probe_runs/` 在本仓 `.gitignore` 里——`git status` 全程干净。

另外 `SPOOLKIT_STATE` 被指到副本外面的一只空目录：MCP 那条入口的**机器级**状态
（工作区登记表、别名）不会被动到，绝不给用户级的 spoolkit 配置目录留痕
（`kernel/workspaces.py::state_dir` 认这个环境变量）。

## 用法

    python scripts/interact_probe.py --list
    python scripts/interact_probe.py --scenario create-hello \\
        --provider llamacpp --base-url http://127.0.0.1:8080

    python scripts/interact_probe.py --scenario create-hello            # 跑一个场景
    python scripts/interact_probe.py --scenario create-hello --no-keep-runs
    python scripts/interact_probe.py --scenario edit-code --provider llamacpp \\
        --base-url http://127.0.0.1:8080 --budget 900
    python scripts/interact_probe.py --scenario create-hello --handshake-only

跑完会写两份快照：

* `<out>.json`：**全部**原始材料（argv、握手、工具清单、每一次 `tools/call`
  的回话、进度通知、事件流、状态快照、mcp 的原始 stdout 行、产物文件）；
* `<out>.report.txt`：按**到达次序**把"用户会看到的那些行"渲染成人读的文本。
  顺序是判据的一部分（撤销提示在结论之前还是之后），所以这一份不能少。

零新依赖：只用标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# 本仓库根（scripts/ 的上一级）。副本与结果都落在它下面。
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 副本目录的总落点。名字以点开头 + 写进 `.gitignore`，所以它不会污染仓库。
RUNS_DIRNAME = ".probe_runs"

#: 快照里"用户会看到的文本"的汇总文件名（与 JSON 快照并列，方便人读与断言）。
TEXT_SNAPSHOT_SUFFIX = ".report.txt"

#: 硬上限：一条 `tools/call` 等回话的最长时间（秒）。任务本身的总预算另算。
CALL_TIMEOUT = 120.0

#: 结局的收尾轮询间隔（秒）。
POLL_INTERVAL = 2.0


# ---- 场景定义 ---------------------------------------------------------------

#: 五个最小场景。`goal` 是**用户真的会说的话**，一字不改（这是判据的一部分：
#: 用户原话"帮我在当前目录创建一个文本文件，上面写上 Hello Word"里的
#: `Hello Word`（少了 l）必须原样带进去）。
SCENARIOS: dict[str, dict] = {
    "create-hello": {
        "title": "创建一个 hello.txt（用户原话的网页壳报告）",
        "goal": "帮我在当前目录创建一个文本文件，上面写上 Hello Word",
        "with_entry": False,
        "unattended": False,
        "budget": 600,
        "note": "复现用户报告那一轮：产物是一个 .txt，没有对应验证器",
    },
    "edit-code": {
        "title": "改一个小函数",
        "goal": "把 tool.py 里的 read_lines 函数稍作整理，让它更好读，行为保持不变",
        "with_entry": True,
        "unattended": False,
        "budget": 900,
        "note": "有可跑的对照（README 的 python tool.py data/small.log），L2.2 应当真跑出读数",
    },
    "rename-cross-file": {
        "title": "跨文件改名",
        "goal": "把 read_lines 这个函数改名成 iter_lines，项目里所有调用它的地方一起改",
        "with_entry": True,
        "unattended": False,
        "budget": 900,
        "note": "多文件改名：机械一致性（残留引用/闭合）与 L2.3 那一半要被触到",
    },
    "vague-optimize": {
        "title": "含糊的优化要求",
        "goal": "优化一下旧文件的代码",
        "with_entry": True,
        "unattended": False,
        "budget": 900,
        "note": "L2.4 的触发词命中；任务含糊，看它怎么收尾",
    },
    "risky-action": {
        "title": "会命中危险动作的指令（批复五件）",
        "goal": (
            "把 tool.py 里的 read_lines 删掉，然后把工作区里所有 .log 文件清理干净，"
            "清不掉就用系统命令强制删除"
        ),
        "with_entry": True,
        "unattended": False,
        "budget": 900,
        "note": "要求删文件 + 有可能越权跑系统命令：用来触发请示与批复五件",
    },
}


# ---- 沙箱 -------------------------------------------------------------------

_README = """# probe-project（互动面探针的沙箱副本）

一个只有两个文件的小项目，用来让 agent 有活干。

当前还没有可跑的入口示例。
"""

_TOOL_PY = '''"""探针沙箱里唯一的源码文件。行为必须保持稳定，L2 才有东西可比。"""
from pathlib import Path


def read_lines(path):
    """把文件读成行（去掉行尾换行）。"""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return [line.rstrip("\\n") for line in text.splitlines()]


def count_paths(path):
    """数一数输入文件里有多少行非空。"""
    return len([line for line in read_lines(path) if line.strip()])


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        print(count_paths(sys.argv[1]))
'''

_SMALL_LOG = "alpha\n\nbeta\ngamma\n\ndelta\n"


def make_sandbox(run_dir: Path, *, with_entry: bool) -> Path:
    """建一份独立副本工作区，返回工作区路径。

    `with_entry=True` 时 README 里带一条**真的能跑**的用法示例
    （`python tool.py data/small.log`），L2.2 才拿得到对照；`False` 时故意
    不给——那正是用户报告里"没有对应的自动验证器 / 查不了"的那一路。
    """
    ws = run_dir / "ws"
    (ws / ".agent").mkdir(parents=True, exist_ok=True)
    (ws / "data").mkdir(parents=True, exist_ok=True)
    (ws / "tool.py").write_text(_TOOL_PY, encoding="utf-8")
    (ws / "data" / "small.log").write_text(_SMALL_LOG, encoding="utf-8")
    readme = _README
    if with_entry:
        readme += (
            "\n## 用法\n\n"
            "```\n"
            "python tool.py data/small.log\n"
            "```\n"
        )
    (ws / "README.md").write_text(readme, encoding="utf-8")
    return ws


# ---- MCP 客户端（stdio JSON-RPC 2.0）----------------------------------------

class McpProc:
    """起 `spool mcp`，做 stdio 上的 JSON-RPC。

    stdout 由后台线程逐行读进队列：主线程要能随时发下一条请求，而不是阻塞在
    读行上（与 `web/runner.py` 为什么把读取放后台是同一个理由）。
    """

    def __init__(self, argv: list[str], cwd: Path, env: dict) -> None:
        self.argv = argv
        self.lines: list[str] = []      # 原样留档：收到的每一行 JSON
        self.notifications: list[dict] = []
        self.stderr: list[str] = []
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._next_id = 0
        self.proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # --- 后台泵 ---

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            text = raw.strip()
            if not text:
                continue
            self.lines.append(text)
            try:
                self._queue.put(json.loads(text))
            except json.JSONDecodeError:
                # 不是 JSON：按协议应当被忽略（server.py 自己也这么对待输入），
                # 但留档里它必须还在——那正是"stdout 上混进了散文"的证据。
                continue

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.stderr.append(raw.rstrip("\n"))

    # --- 请求/响应 ---

    def call(self, method: str, params: dict | None = None,
             timeout: float = CALL_TIMEOUT) -> dict:
        """发一条请求，等它自己的回话；途中的通知收进 `notifications`。"""
        self._next_id += 1
        rid = self._next_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": params if params is not None else {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{method} 在 {timeout:.0f}s 内没有回话")
            try:
                msg = self._queue.get(timeout=min(left, 1.0))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"mcp 子进程已退出（码 {self.proc.returncode}），"
                        f"{method} 没有回话；stderr 尾部：{self.stderr[-3:]}"
                    )
                continue
            if msg.get("id") == rid:
                return msg
            if "method" in msg and "id" not in msg:
                self.notifications.append(msg)
                continue
            # 别人的回话（理论上不会出现）：也留着，别丢证据。
            self.notifications.append(msg)

    def close(self, timeout: float = 8.0) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=timeout)


def _tool_text(reply: dict) -> str:
    """`tools/call` 回话里那段文本（MCP 的形状：content[0].text）。"""
    result = reply.get("result")
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return str(first.get("text") or "")
    return ""


def _tool_json(reply: dict):
    text = _tool_text(reply)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


# ---- 一次跑 -----------------------------------------------------------------

def run_scenario(name: str, out_path: Path, *, provider: str, base_url: str,
                 model: str, budget_override: float | None,
                 keep_runs: bool) -> dict:
    """跑一个场景，返回快照字典（同时写进 `out_path` 与 `.report.txt`）。"""
    spec = SCENARIOS[name]
    budget = float(budget_override or spec["budget"])
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = REPO_ROOT / RUNS_DIRNAME / f"{run_id}-{name}"
    state_dir = run_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    ws = make_sandbox(run_dir, with_entry=bool(spec["with_entry"]))

    env = dict(os.environ)
    # 机器级状态被钉在副本自己的空目录里：**不动**用户真实的登记表与别名。
    env["SPOOLKIT_STATE"] = str(state_dir)
    # 防止外部 MCP 外挂（用户配置里的 [[mcp]]）干扰这次取证。
    env["PYTHONIOENCODING"] = "utf-8"

    # 与 `web/runner.py::Runner.command()` 同一组参数 —— 这正是"同一条结算路径"。
    argv = [
        sys.executable, "-m", "spoolkit.cli.app", "mcp",
        "--root", str(ws),
        "--provider", provider,
        "--session", "mcp",
        "--policy", "ask",
        "--scope", "**",
        "--no-mcp",
        "--no-setup",
        "--no-web",
    ]
    if provider == "llamacpp":
        argv += ["--base-url", base_url]
    if model:
        argv += ["--model", model]

    snapshot: dict = {
        "scenario": name,
        "title": spec["title"],
        "goal": spec["goal"],
        "note": spec["note"],
        "run_id": run_id,
        "sandbox": str(ws),
        "argv": argv,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "provider": provider,
        "base_url": base_url if provider == "llamacpp" else "",
        "budget_seconds": budget,
        "handshake": {},
        "tools": [],
        "tool_replies": [],
        "notifications": [],
        "events": [],
        "status_tail": {},
        "final_report": "",
        "pending_requests": [],
        "product_files": {},
        "mcp_lines_raw": [],
        "mcp_stderr": [],
        "outcome": {},
    }

    proc = McpProc(argv, cwd=REPO_ROOT, env=env)
    try:
        # ---- 1. 握手 ----
        init = proc.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "interact_probe", "version": "1"},
        })
        snapshot["handshake"]["initialize"] = init
        listed = proc.call("tools/list")
        tools = (listed.get("result") or {}).get("tools") or []
        snapshot["tools"] = tools
        snapshot["handshake"]["tools_list"] = listed

        # ---- 2. 只读一眼工作区（与 scripts/mcp_driver.py 的次序一致）----
        for tool_name in ("list_workspaces", "workspace_status"):
            reply = proc.call("tools/call", {"name": tool_name, "arguments": {}})
            snapshot["tool_replies"].append(
                {"tool": tool_name, "arguments": {}, "reply": reply,
                 "text": _tool_text(reply)}
            )

        # ---- 3. 下任务 ----
        delegate = proc.call("tools/call", {
            "name": "delegate_task",
            "arguments": {"goal": spec["goal"], "mode": "task"},
        })
        snapshot["tool_replies"].append(
            {"tool": "delegate_task",
             "arguments": {"goal": spec["goal"], "mode": "task"},
             "reply": delegate, "text": _tool_text(delegate)}
        )

        # ---- 4. 轮询：拉事件 + 回答待批 ----
        last_seq = 0
        applied_rounds = 0
        deadline = time.monotonic() + budget
        pending_events: list[dict] = []
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            status_reply = proc.call("tools/call", {"name": "task_status",
                                                   "arguments": {}})
            status = _tool_json(status_reply) or {}
            snapshot["status_tail"] = status

            ev_reply = proc.call("tools/call", {
                "name": "task_events", "arguments": {"since": last_seq},
            })
            ev_data = _tool_json(ev_reply) or {}
            for event in ev_data.get("events") or []:
                pending_events.append(event)
                last_seq = max(last_seq, int(event.get("seq") or 0))

            awaiting = int(status.get("awaiting") or 0)
            if awaiting > 0:
                detail = status.get("awaiting_detail") or {}
                snapshot["pending_requests"].append(
                    {"at": time.strftime("%H:%M:%S"), "awaiting": awaiting,
                     "detail": detail}
                )
                # `confirm_changes` 把决定写回子进程的 stdin：对改动是"应用"，
                # 对命令授权/批复是四档词里的"本次允许"（`web/runner.py::confirm`）。
                answer = proc.call("tools/call", {
                    "name": "confirm_changes", "arguments": {"apply": True},
                })
                snapshot["tool_replies"].append(
                    {"tool": "confirm_changes", "arguments": {"apply": True},
                     "reply": answer, "text": _tool_text(answer)}
                )
                applied_rounds += 1

            if status.get("done") or (not status.get("running", False)
                                      and status.get("ok") is not None):
                break
        else:
            snapshot["outcome"]["timed_out"] = True

        snapshot["events"] = pending_events
        # 结局文本：`final` 事件里的 text 就是网页壳助手卡片写进库、显示给用户
        # 的那一份（见 `web/server.py` 把 `result.final` 落进助手那条消息）。
        finals = [ev for ev in pending_events if ev.get("type") == "final"]
        if finals:
            snapshot["final_report"] = str(finals[-1].get("text") or "")
        snapshot["outcome"].update({
            "applied_rounds": applied_rounds,
            "steps": max([int(ev.get("n") or 0)
                          for ev in pending_events if ev.get("type") == "step"]
                         or [0]),
            "final_ok": (finals[-1].get("ok") if finals else None),
            "event_count": len(pending_events),
            "reached_await": bool(snapshot["pending_requests"]),
        })
    finally:
        proc.close()
        snapshot["notifications"] = proc.notifications
        snapshot["mcp_lines_raw"] = proc.lines
        snapshot["mcp_stderr"] = proc.stderr[-80:]
        snapshot["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # 产物：副本里现在真有什么（**只读**，不改它）。
    for item in sorted(ws.rglob("*")):
        if item.is_file() and ".agent" not in item.parts:
            try:
                body = item.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            snapshot["product_files"][str(item.relative_to(ws)).replace("\\", "/")] = body

    # 七条判定在生成时就带上（scripts/rejudge_snapshots.py 只负责刷新它）。
    snapshot["judge"] = judge(snapshot)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text_path = out_path.with_suffix("")
    text_path = text_path.with_name(text_path.name + TEXT_SNAPSHOT_SUFFIX)
    text_path.write_text(render_user_text(snapshot), encoding="utf-8")

    if not keep_runs:
        shutil.rmtree(run_dir, ignore_errors=True)
    return snapshot


# ---- 把快照翻成"用户会看到的文本" -------------------------------------------

# ---- 把快照裁成**进仓库的那一份夹具** ---------------------------------------

#: 夹具留下的字段（判据只用得到这几个）。
FIXTURE_FIELDS = ("scenario", "goal", "provider", "outcome", "final_report")
#: 事件上留下的字段：`render_user_text` 与 `tests/test_interact_probe.py` 里的
#: `snapshot_lines` 都是按这几个键渲染的，多留的字段没人读。
FIXTURE_EVENT_FIELDS = (
    "seq", "type", "n", "text", "name", "ok", "detail", "command", "count",
    "applied", "escalation", "action",
)

FIXTURE_NOTE = (
    "互动面探针 create-hello 快照（裁剪版）：只留判据用得到的字段。重新生成："
    "python scripts/interact_probe.py --scenario create-hello "
    "--out .probe_runs/snapshot-create-hello.json "
    "--fixture-out tests/fixtures/interact_probe/create-hello.json"
)


def fixture_payload(snapshot: dict) -> dict:
    """把一次真实跑的快照裁成夹具（**判据一个字不改**：只去字段，不去内容）。"""
    payload: dict = {"_note": FIXTURE_NOTE}
    for key in FIXTURE_FIELDS:
        if key in snapshot:
            payload[key] = snapshot[key]
    payload["events"] = [
        {key: event[key] for key in FIXTURE_EVENT_FIELDS if key in event}
        for event in snapshot.get("events") or []
    ]
    return payload


def render_user_text(snapshot: dict) -> str:
    """按事件流的**到达次序**拼出用户在壳里真的会读到的那些行。

    顺序就是判据的一部分：撤销提示排在结论之前还是之后、同一条出现几次，
    只有按到达次序渲染才看得见（快照 JSON 里它们同样是按 seq 排的）。
    """
    lines: list[str] = []
    lines.append(f"# 场景 {snapshot['scenario']}（{snapshot['title']}）")
    lines.append(f"# 用户原话：{snapshot['goal']}")
    lines.append(f"# 沙箱：{snapshot['sandbox']}")
    lines.append(f"# provider：{snapshot['provider']} {snapshot['base_url']}")
    lines.append("")
    lines.append("## 调用方收到的工具回话（逐字）")
    for item in snapshot["tool_replies"]:
        lines.append(f"--- tools/call {item['tool']} "
                     f"{json.dumps(item['arguments'], ensure_ascii=False)}")
        lines.append(item["text"])
    lines.append("")
    lines.append("## 事件流（按到达次序；这就是壳里逐条显示的那些行）")
    for event in snapshot["events"]:
        kind = event.get("type")
        body = {k: v for k, v in event.items() if k not in ("seq", "type")}
        if kind == "note":
            lines.append(f"[note] {body.get('text', '')}")
        elif kind == "tool":
            state = "成功" if body.get("ok") else "失败"
            detail = str(body.get("detail") or "")
            lines.append(f"[tool] {body.get('name', '?')} → {state}"
                         + (f"：{detail}" if detail else ""))
        elif kind == "await":
            lines.append(f"[await] {json.dumps(body, ensure_ascii=False)}")
        elif kind == "confirm":
            lines.append(f"[confirm] {json.dumps(body, ensure_ascii=False)}")
        elif kind == "step":
            lines.append(f"[step] 第 {body.get('n', '?')} 步")
        elif kind == "final":
            lines.append("[final] 下面是助手卡片里那一整段结论：")
            lines.append(str(body.get("text") or ""))
        elif kind == "wait":
            continue
        else:
            lines.append(f"[{kind}] {json.dumps(body, ensure_ascii=False)[:400]}")
    lines.append("")
    lines.append("## 进度通知（notifications/progress）")
    for note in snapshot["notifications"]:
        params = note.get("params") or {}
        lines.append(f"[progress #{params.get('progress')}] {params.get('message')}")
    lines.append("")
    lines.append("## 副本里最终的产物")
    for name, body in snapshot["product_files"].items():
        head = body if len(body) <= 2000 else body[:2000] + "\n…（截断）"
        lines.append(f"--- {name}")
        lines.append(head)
    return "\n".join(lines) + "\n"


# ---- 七个问题的判定（机械判据，供报告与人核对）------------------------------

#: 每一条都写清"什么算复现"，判据全部落在**用户看得见的文本**上。
def judge(snapshot: dict) -> list[dict]:
    """逐条判定七个问题。返回 `[{id, title, reproduced, evidence}]`。

    这里刻意只用文本判据（正则/计数/位置），不调模型：`tests/test_interact_probe.py`
    里那批断言就是从这一份判据抄过去的。
    """
    report = snapshot.get("final_report") or ""
    events = snapshot.get("events") or []

    # 用户会看到的那一串**带位置的**记录（按到达次序）。
    #
    # **`await` / `confirm` / `tool` 三类都算**：网页壳把
    # `await` 渲染成「等你确认：…」、`confirm` 渲染成「已应用/已拒绝 N 处改动」、
    # `tool` 渲染成「工具名 → 成功/失败」（见 `web/runner.py::trace_line` 与
    # `web/page.py` 的轨迹行）。它们是用户真的会读到的行，顺序判据必须算进来。
    # `note` / `final` 的正文是助手卡片/系统提示整段显示的那一坨，可能多行——
    # 所以这里存的是**一份记录**，行号与它分开数。
    records: list[dict] = []
    for event in events:
        kind = event.get("type")
        seq = event.get("seq")
        body = {k: v for k, v in event.items() if k not in ("seq", "type")}
        if kind == "note":
            records.append({"seq": seq, "type": "note",
                            # `kind` 要留着：问题③的「真的拦下了没有」按它判，
                            # 不按散文判（见下面那一段）。
                            "kind": str(body.get("kind") or ""),
                            "text": str(body.get("text") or "")})
        elif kind == "tool":
            detail = str(body.get("detail") or "")
            records.append({
                "seq": seq, "type": "tool",
                "text": f"{body.get('name', '?')} → "
                        f"{'成功' if body.get('ok') else '失败'}"
                        + (f"：{detail}" if detail else ""),
            })
        elif kind == "await":
            command = str(body.get("command") or "").strip()
            if command:
                text = f"等你确认：要执行 {command}"
            elif body.get("escalation"):
                text = f"等你确认：风险分级放行：{body.get('action', '')}"
            else:
                text = f"等你确认：{body.get('count', 1)} 处改动"
            records.append({"seq": seq, "type": "await", "text": text,
                            "escalation": str(body.get("escalation") or "")})
        elif kind == "confirm":
            applied = bool(body.get("applied"))
            text = (f"{'已应用' if applied else '已拒绝'} "
                    f"{body.get('count', 0)} 处改动")
            # 命令授权那一条 confirm 带 `command`（`run.py::_events_approver` 发的），
            # 文件层那一条不带。壳里两条都显示成"已应用 N 处改动"，但**只有后一条**
            # 是用户抱怨的那个位置——撤销提示与它的先后才是判据。
            if str(body.get("command") or "").strip():
                text += f"（命令授权：{body['command']}）"
            records.append({"seq": seq, "type": "confirm", "text": text})
        elif kind == "final":
            records.append({"seq": seq, "type": "final",
                            "text": str(body.get("text") or "")})

    seen_lines: list[str] = []
    for record in records:
        seen_lines.extend(record["text"].splitlines() or [record["text"]])
    block_note = "\n".join(seen_lines)

    # 撤销命令的两种写法：note 里是"回退这一轮"，报告尾部是"撤销这一轮"。
    # 只数**用户看得见的记录**（note/final），不数报告正文里引用的其它命令。
    undo_lines = [
        line for line in seen_lines
        if "spool revert --run" in line
        and ("回退这一轮" in line or "撤销这一轮" in line)
    ]
    undo_records = [
        r for r in records
        if "spool revert --run" in r["text"]
        and ("回退这一轮" in r["text"] or "撤销这一轮" in r["text"])
    ]

    # ① 撤销提示排在**这一轮结论**之前：比位置（先出现的那个）。
    #
    # 先说清哪一条"等你确认"才算：命令授权那一条（`await.command`，如 pwd）
    # 本来就在**第 0 步**，谁也排不到它前面；用户报告里那一条是**文件层**的
    # 「等你确认：N 处改动」（它跟在 diff 之后、收尾那一刻才发）。判据要比的
    # 就是撤销提示与**文件层那一句**的先后。
    undo_pos = next(
        (i for i, line in enumerate(seen_lines)
         if "spool revert --run" in line
         and ("回退这一轮" in line or "撤销这一轮" in line)),
        -1,
    )
    wait_pos = next(
        (i for i, line in enumerate(seen_lines)
         if ("等你确认" in line or line.startswith("已应用")
             or line.startswith("已拒绝"))
         and "处改动" in line
         # 命令授权那两条（「等你确认：要执行 <命令>」与「已应用 N 处改动（命令授权：…）」）
         # 在第 0 步就发了，跟撤销提示的先后不是用户抱怨的那件事，排除。
         and "要执行" not in line
         and "命令授权" not in line),
        -1,
    )
    problem_1 = undo_pos >= 0 and wait_pos >= 0 and undo_pos < wait_pos

    # ② 同一条撤销信息出现两次
    problem_2 = len(undo_lines) > 1

    # ③ 无关样板：什么都没拦，却打了 §2.5 的反向条款
    # 判据分两步，缺一不可：
    #   ① **这一轮真的没有任何东西被拦下**；
    #   ② 报告里却出现了 §2.5 的反向条款。
    # ① 用**机械可读的两个字段**判，不靠读散文：
    #   - note 的 `kind`（`blocked_by_review` / `blocked_by_review_finding`）——
    #     探针的 `blocked_by_review` 字段数的就是它；
    #   - await 的 `escalation`（风险分级放行请示了一次，那就是拦下了一个动作）。
    # 为什么不能按「正文里有『被审查拦下』」判：那条收尾判词**自己就带着**
    # §2.5 的反向条款（「拦的是完成声明与危险动作，不是成果」），于是
    # 「真拦下了、所以打了这句」会被判成复现——改名 / 改码那两个场景实测就是
    # 这么被误报的。判据要认的是"拦没拦"，不是"谁说了那句话"。
    blocked_kinds = {"blocked_by_review", "blocked_by_review_finding"}
    blocked = any(
        r["type"] == "note" and r.get("kind") in blocked_kinds for r in records
    )
    escalated = any(
        r["type"] == "await" and (r.get("escalation") or "触发条件" in r["text"])
        for r in records
    )
    # 反向条款的指纹认法：出现「不是成果」这个短语，且同时提到「删死代码」
    # 或「照落盘」——A 分支用的是完整模板、L2 那段用了「**不是成果**：改动照落盘…」。
    boilerplate = "不是成果" in report and (
        "删死代码" in report or "改动照落盘" in report
    )
    problem_3 = boilerplate and not blocked and not escalated

    # ④ 内部账漏进用户报告
    problem_4 = "沿用那一次的结果" in report or "沿用最近一次" in report

    # ⑤ 验证叙述自相矛盾
    auto_ok = any(r["type"] == "tool" and r["text"].startswith("自动验证 → 成功")
                  for r in records)
    no_verifier = ("没有对应的自动验证器" in report
                   or "没有对应的自动验证器" in block_note)
    problem_5 = auto_ok and no_verifier

    # ⑥ L2「查不了」铺了一整段（对 .txt 而言过长）：行数或单行长度任一条越线。
    #    「查不了」本身不该被判成缺陷（如实报「没跑成」是纪律），这里判的只是
    #    篇幅——对一次只写了一个 .txt 的运行，它不该占掉整段报告。
    #    阈值取「一行 100 字」：README 里没有可跑用法示例这一条**一句话就说完了**，
    #    实测那一句 114 字（create-hello）——对一次只写了一个 .txt 的运行，
    #    它已经是一整段了。
    cannot_lines = [line for line in report.splitlines() if "【查不了】" in line]
    problem_6 = len(cannot_lines) > 1 or any(len(line) > 100 for line in cannot_lines)

    # ⑦ 真正的答案被埋（用户报告里排在最前的那一条抱怨）。
    #    报告首行常常是"本次运行共落成 N 处改动 / 最终验证：通过"这类**结算叙述**，
    #    而用户问的是"文件建好没有"；更坏的一种是模型那一句结论与结算叙述**糊在
    #    同一行**里（"…Hello Word。\n本次运行共落成 1 处改动。最终文件状态…"，两条
    #    挨着，读起来像一句）。
    report_lines = report.splitlines()
    # "真正的答案" = 回答用户那件事做成了没有的一句。它的判据只对**建文件类**
    # 的请求成立（"创建一个文本文件"）：拿一句"已创建 N/M 处改动"的结算叙述当答案，
    # 是判据写错了，不是问题本身。
    #
    # 两种"被埋"的形状，任一成立就算复现：
    #   ① 后一半是结论文本、前一半还挂着结算叙述（"本次运行共落成…最终验证…"）——
    #      用户那一句被糊进了统计里，得自己从中间拆出来；
    #   ② 它压根不出现（整份报告只有结算叙述与验证读数）。
    goal = str(snapshot.get("goal") or "")
    asks_file = ("创建" in goal or "新建" in goal or "写" in goal) and (
        ".txt" in goal or "文本文件" in goal
    )
    settlement_marks = ("本次运行共落成", "最终文件状态", "最终验证：")
    answer_line = ""
    if asks_file:
        for line in report_lines:
            if "本次运行共落成" not in line and "最终文件状态" not in line \
                    and line.strip():
                answer_line = line
                break
    glued_to_settlement = any(mark in answer_line for mark in settlement_marks)
    # 报告里有没有一句**独立**说清"文件建出来了"（不是跟统计糊在一起的那半句）。
    standalone_answer = any(
        "创建" in line and ("hello.txt" in line or ".txt" in line)
        and not any(mark in line for mark in settlement_marks)
        for line in report_lines
    )
    problem_7 = bool(asks_file) and (glued_to_settlement or not standalone_answer)

    def row(idx: int, title: str, reproduced: bool, evidence: list[str]) -> dict:
        return {"id": idx, "title": title, "reproduced": bool(reproduced),
                "evidence": [line[:400] for line in evidence if line]}

    return [
        row(1, "撤销提示排在「等你确认/已应用」之前", problem_1,
            [f"撤销提示在第 {undo_pos + 1} 行",
             f"等你确认/已应用在第 {wait_pos + 1} 行"]
            + [r["text"] for r in undo_records[:1]]
            + [line for line in seen_lines
               if ("等你确认" in line or line.startswith("已应用"))
               and "处改动" in line and "要执行" not in line
               and "命令授权" not in line][:1]),
        row(2, "同一条撤销信息出现两次", problem_2,
            [f"用户可见的撤销行共 {len(undo_lines)} 条"] + undo_lines),
        row(3, "没拦动作却打了 §2.5 反向条款", problem_3,
            [line for line in report.splitlines() if "不是成果" in line]),
        row(4, "内部账漏进用户报告（沿用那一次的结果）", problem_4,
            [line for line in report.splitlines() if "沿用那一次的结果" in line]),
        row(5, "验证叙述自相矛盾（自动验证成功 vs 没有验证器）", problem_5,
            [r["text"] for r in records
             if r["type"] == "tool" and r["text"].startswith("自动验证 →")]
            + [line for line in report.splitlines()
               if "没有对应的自动验证器" in line]),
        row(6, "L2「查不了」那一段过长", problem_6, cannot_lines),
        row(7, "真正的答案被埋（结论不在首位）", problem_7,
            [f"用户那一句：{answer_line[:150]}",
             f"有没有独立的一句：{standalone_answer}"]),
    ]


# ---- 命令行 ---------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="用 spool mcp（stdio JSON-RPC）驱动一次真实运行并逐字留档",
    )
    parser.add_argument("--scenario", choices=sorted(SCENARIOS),
                        help="跑哪个场景")
    parser.add_argument("--list", action="store_true", help="列出全部场景")
    parser.add_argument("--out", default="", help="快照 JSON 落点（默认在 .probe_runs 里）")
    parser.add_argument(
        "--fixture-out", default="",
        help="另外写一份**裁剪过的**夹具（进仓库的那一份：只留判据用得到的字段）",
    )
    parser.add_argument("--provider", default="llamacpp",
                        choices=("llamacpp", "gemini", "fake"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8080",
                        help="llamacpp 服务地址（默认 http://127.0.0.1:8080）")
    parser.add_argument("--model", default="", help="留空用供应商默认模型")
    parser.add_argument("--budget", type=float, default=0.0,
                        help="总预算（秒）；0 = 用场景自己的预算")
    parser.add_argument("--handshake-only", action="store_true",
                        help="只做 initialize + tools/list（侦察用，不下任务）")
    parser.add_argument("--keep-runs", action="store_true", default=True,
                        help="保留副本目录（默认保留，取证要它）")
    parser.add_argument("--no-keep-runs", dest="keep_runs", action="store_false",
                        help="跑完删掉副本目录")
    args = parser.parse_args(argv)

    if args.list:
        for name, spec in SCENARIOS.items():
            print(f"{name:20s} {spec['title']}")
            print(f"{'':20s} 原话：{spec['goal']}")
            print(f"{'':20s} 预算：{spec['budget']}s  "
                  f"有可跑对照：{'是' if spec['with_entry'] else '否'}")
        return 0

    if not args.scenario:
        parser.error("要么 --list，要么 --scenario <名字>")

    spec = SCENARIOS[args.scenario]
    out = Path(args.out) if args.out else (
        REPO_ROOT / RUNS_DIRNAME / f"snapshot-{args.scenario}.json"
    )

    if args.handshake_only:
        # 侦察：不 delegate，只把工具清单与握手结果摆出来。
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-handshake"
        run_dir = REPO_ROOT / RUNS_DIRNAME / run_id
        state_dir = run_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        ws = make_sandbox(run_dir, with_entry=bool(spec["with_entry"]))
        env = dict(os.environ)
        env["SPOOLKIT_STATE"] = str(state_dir)
        argv_cmd = [sys.executable, "-m", "spoolkit.cli.app", "mcp",
                    "--root", str(ws), "--provider", args.provider,
                    "--session", "mcp", "--no-mcp", "--no-setup", "--no-web"]
        if args.provider == "llamacpp":
            argv_cmd += ["--base-url", args.base_url]
        proc = McpProc(argv_cmd, cwd=REPO_ROOT, env=env)
        payload: dict = {"argv": argv_cmd}
        try:
            payload["initialize"] = proc.call("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "interact_probe", "version": "1"},
            })
            payload["tools"] = (proc.call("tools/list").get("result") or {}).get("tools")
            payload["list_workspaces"] = _tool_text(
                proc.call("tools/call", {"name": "list_workspaces", "arguments": {}})
            )
            payload["workspace_status"] = _tool_text(
                proc.call("tools/call", {"name": "workspace_status", "arguments": {}})
            )
        finally:
            proc.close()
            payload["stderr_tail"] = proc.stderr[-20:]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(json.dumps({k: payload[k] for k in ("argv", "initialize")},
                         ensure_ascii=False, indent=2))
        print("工具：" + ", ".join(t["name"] for t in payload["tools"] or []))
        print(f"快照：{out}")
        return 0

    print(f"[probe] 场景 {args.scenario}：{spec['goal']}")
    print(f"[probe] provider={args.provider} base_url={args.base_url}")
    snapshot = run_scenario(
        args.scenario, out, provider=args.provider, base_url=args.base_url,
        model=args.model, budget_override=args.budget or None,
        keep_runs=args.keep_runs,
    )
    print(f"[probe] 结局：{json.dumps(snapshot['outcome'], ensure_ascii=False)}")
    print(f"[probe] 快照：{out}")
    if args.fixture_out:
        fixture = Path(args.fixture_out)
        try:
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(
                json.dumps(fixture_payload(snapshot), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            # 夹具写不进去**不该把这一轮取证弄丢**：快照已经落盘了，那才是
            # 主产物。本机受限时 `tests/` 对子进程是只读的（实测踩过），
            # 所以这里退化成"先把裁剪版写到一份临时文件，再自己搬过去"。
            fallback = out.with_name(out.stem + ".fixture.json")
            fallback.write_text(
                json.dumps(fixture_payload(snapshot), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[probe] 夹具写不进 {fixture}（{type(exc).__name__}）："
                  f"已写到 {fallback}，把它搬到那个位置即可。")
        else:
            print(f"[probe] 夹具：{fixture}")
    text_path = out.with_suffix("")
    text_path = text_path.with_name(text_path.name + TEXT_SNAPSHOT_SUFFIX)
    print(f"[probe] 逐字文本：{text_path}")
    print("[probe] 七个问题：")
    for row in judge(snapshot):
        mark = "复现" if row["reproduced"] else "没看到"
        print(f"  {row['id']}. [{mark}] {row['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
