# Web UI 壳 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在浏览器里实时看到 agent 在做什么，看到待确认的 diff，点一下应用或拒绝。

**Architecture:** 壳是一个标准库写的本地 HTTP 服务，通过子进程跑既有 CLI。CLI 新增 `--events` 模式输出 JSON 行事件，服务解析后经 SSE 广播给浏览器；浏览器点确认时，服务往子进程的 stdin 写 `y` 或 `n`。内核与壳之间只有进程边界，没有 import 关系。

**Tech Stack:** Python 3.14 标准库（`http.server`、`subprocess`、`threading`、`json`、`queue`）、原生 JS（无构建步骤）。**不新增任何依赖。**

## Global Constraints

- 不新增依赖：后端只用标准库，前端只有一个不打包的 HTML 字符串。
- 服务只绑 `127.0.0.1`，不做鉴权与远程访问。
- 一次只跑一个运行；第二次请求返回 409，并说明原因。
- 事件输出必须每行 flush，否则输出会攒在缓冲区里，UI 看起来像卡住。
- 所有面向人的中文文案与注释；标识符用英文。
- 每个任务结束时 pytest 全绿并提交一次。
- **测试在沙箱内单独跑，git 提交在沙箱外单独执行，两者绝不合并成一条命令。**

---

## 与设计文档的一处修正

设计文档写的是「内核一行不改」。规划时发现这句站不住：**主循环现在是跑完才把轨迹一次性返回，要让 UI 看到实时的「第几步、调了什么工具」，它必须能往外发事件。**

改动很小——给 `AgentLoop` 加一个可选的 `on_event` 回调，不传就跟现在完全一样。但它确实是对内核的修改，所以记在这里，也记进设计文档的修订。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/spoolkit/web/__init__.py` | 包说明 |
| `src/spoolkit/web/protocol.py` | 事件类型、序列化、解析 |
| `src/spoolkit/web/runner.py` | 子进程生命周期、读取线程、确认写入 |
| `src/spoolkit/web/server.py` | HTTP 路由与 SSE 广播 |
| `src/spoolkit/web/page.py` | 内嵌的单页 HTML |
| `src/spoolkit/cli/events.py` | 内核侧的事件输出器 |
| `src/spoolkit/cli/commands/serve.py` | `serve` 命令 |
| `src/spoolkit/agent/loop.py` | 修改：加 `on_event` 回调 |

页面内嵌成 Python 字符串而不是外置 HTML：**跑起来只有一个东西要分发**，也避免打包漏文件。代价是编辑时没有语法高亮，可以接受。

---

## Task 1: 事件协议

**Files:**
- Create: `src/spoolkit/web/__init__.py`
- Create: `src/spoolkit/web/protocol.py`
- Test: `tests/web/__init__.py`, `tests/web/test_protocol.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `spoolkit.web.protocol.Event(type: str, data: dict)`
    - `.to_line() -> str`
  - `spoolkit.web.protocol.parse_line(line: str) -> Event | None`
  - 事件类型常量：`START` `STEP` `TOOL` `DIFF` `AWAIT` `CONFIRM` `USAGE` `FINAL` `ERROR`

- [ ] **Step 1: 写失败测试**

```python
# tests/web/__init__.py
"""web 子包测试。"""
```

```python
# tests/web/test_protocol.py
from spoolkit.web.protocol import (
    AWAIT,
    DIFF,
    FINAL,
    Event,
    parse_line,
)


def test_序列化成一行JSON() -> None:
    line = Event(FINAL, {"ok": True, "text": "做完了"}).to_line()
    assert "\n" not in line
    assert '"type": "final"' in line
    assert "做完了" in line


def test_解析往返一致() -> None:
    original = Event(DIFF, {"path": "a.py", "text": "-x\n+y"})
    parsed = parse_line(original.to_line())
    assert parsed is not None
    assert parsed.type == DIFF
    assert parsed.data["path"] == "a.py"
    assert parsed.data["text"] == "-x\n+y"


def test_忽略散文行() -> None:
    """内核偶尔会打印别的（第三方库的警告），不能因为一行杂音断流。"""
    assert parse_line("step0: 工具 read_file -> 成功") is None
    assert parse_line("") is None
    assert parse_line("   ") is None


def test_忽略损坏的JSON() -> None:
    assert parse_line('{"type": "final"') is None
    assert parse_line("{不是 JSON}") is None


def test_忽略未知类型() -> None:
    assert parse_line('{"type": "凭空冒出来的"}') is None


def test_忽略非对象JSON() -> None:
    assert parse_line("[1, 2, 3]") is None
    assert parse_line('"字符串"') is None


def test_缺少type字段被忽略() -> None:
    assert parse_line('{"ok": true}') is None


def test_type不进入data() -> None:
    parsed = parse_line(Event(AWAIT, {"count": 2}).to_line())
    assert parsed is not None
    assert "type" not in parsed.data
    assert parsed.data["count"] == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/web -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'spoolkit.web'`

- [ ] **Step 3: 写最小实现**

```python
# src/spoolkit/web/__init__.py
"""Web UI 壳：用本地 HTTP 服务把命令行 agent 包起来。"""
```

```python
# src/spoolkit/web/protocol.py
"""事件协议。

子进程在 stdout 上一行吐一个 JSON 对象。非 JSON 行一律忽略——
内核偶尔会打印别的（第三方库的警告之类），不能因为一行杂音让整个流断掉。

未知类型也忽略，而不是报错。这样以后加新事件时，老版本的服务不会崩。
"""

import json
from dataclasses import dataclass, field
from typing import Any

START = "start"
STEP = "step"
TOOL = "tool"
DIFF = "diff"
AWAIT = "await"
CONFIRM = "confirm"
USAGE = "usage"
FINAL = "final"
ERROR = "error"

KNOWN = frozenset(
    {START, STEP, TOOL, DIFF, AWAIT, CONFIRM, USAGE, FINAL, ERROR}
)


@dataclass(frozen=True)
class Event:
    """一条事件。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_line(self) -> str:
        """序列化成一行，不带换行符。"""
        return json.dumps(
            {"type": self.type, **self.data}, ensure_ascii=False
        )


def parse_line(line: str) -> Event | None:
    """解析一行。不是合法事件的都返回 None，不抛异常。"""
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind not in KNOWN:
        return None
    return Event(
        type=kind,
        data={key: value for key, value in payload.items() if key != "type"},
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/web -q`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add src/spoolkit/web/ tests/web/
git commit -m "feat: Web UI 事件协议"
```

---

## Task 2: 主循环的事件回调

**Files:**
- Modify: `src/spoolkit/agent/loop.py`
- Test: `tests/agent/test_loop_events.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `AgentLoop.__init__(..., on_event: Callable[[str, dict], None] | None = None)`
  - 回调在两种时机被调用：每轮开始 `("step", {"n": int})`、每次工具结束 `("tool", {"name": str, "ok": bool, "detail": str})`

**为什么要改内核**：主循环现在是跑完才返回整条轨迹。UI 要看到「正在第几步、刚调了什么」，只能让它在过程中发出来。不传 `on_event` 时行为与现在完全一致。

- [ ] **Step 1: 写失败测试**

```python
# tests/agent/test_loop_events.py
import json
from pathlib import Path

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry


def _turn(calls, final=None) -> str:
    return json.dumps(
        {
            "thought": "t",
            "tool_calls": calls,
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _loop(tmp_path: Path, script: list[str], on_event=None) -> AgentLoop:
    tokenizer = OfflineTokenCounter()
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=registry,
        config=Config(project_root=tmp_path, context_window=4096, max_steps=4),
        on_event=on_event,
    )


def test_每轮开始发出step事件(tmp_path: Path) -> None:
    seen: list[tuple[str, dict]] = []
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    _loop(
        tmp_path,
        [
            _turn([{"name": "read_file", "arguments": {"path": "a.txt"}}]),
            _turn([], final="完成"),
        ],
        on_event=lambda kind, data: seen.append((kind, data)),
    ).run("读文件")
    assert ("step", {"n": 0}) in seen
    assert ("step", {"n": 1}) in seen


def test_工具结束发出tool事件(tmp_path: Path) -> None:
    seen: list[tuple[str, dict]] = []
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    _loop(
        tmp_path,
        [
            _turn([{"name": "read_file", "arguments": {"path": "a.txt"}}]),
            _turn([], final="完成"),
        ],
        on_event=lambda kind, data: seen.append((kind, data)),
    ).run("读文件")
    tools = [data for kind, data in seen if kind == "tool"]
    assert tools[0]["name"] == "read_file"
    assert tools[0]["ok"] is True


def test_失败的工具也发事件(tmp_path: Path) -> None:
    seen: list[tuple[str, dict]] = []
    _loop(
        tmp_path,
        [
            _turn([{"name": "read_file", "arguments": {"path": "不存在.txt"}}]),
            _turn([], final="完成"),
        ],
        on_event=lambda kind, data: seen.append((kind, data)),
    ).run("读文件")
    assert [d for k, d in seen if k == "tool"][0]["ok"] is False


def test_不传回调时行为不变(tmp_path: Path) -> None:
    result = _loop(tmp_path, [_turn([], final="完成")]).run("随便")
    assert result.finished is True


def test_回调抛异常不影响任务(tmp_path: Path) -> None:
    """回调是壳挂上来的。它出错不能让任务崩——壳的问题不该毁掉内核的工作。"""

    def broken(kind, data):
        raise RuntimeError("壳挂了")

    result = _loop(tmp_path, [_turn([], final="完成")], on_event=broken).run("任务")
    assert result.finished is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/agent/test_loop_events.py -q`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'on_event'`

- [ ] **Step 3: 写最小实现（修改主循环）**

在 `loop.py` 顶部导入区加入：

```python
from typing import Any, Callable
```

`AgentLoop.__init__` 增加参数并在构造器末尾保存：

```python
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        ...
        self.on_event = on_event
```

新增一个私有方法，放在 `_note_progress` 旁边：

```python
    def _emit(self, kind: str, data: dict) -> None:
        """向外部观察者发一条事件。

        回调异常一律吞掉：它是壳挂上来的，壳的问题不该毁掉内核的工作。
        """
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception:
            pass
```

在 `run` 的循环体开头、装配完成之后发 step：

```python
        while state.step < limit:
            assembled = self._assemble(
                state, history, feedback, prefetched, hot, lesson_text
            )
            self._emit("step", {"n": state.step})
```

在工具执行处发 tool（紧接 `self._note_progress(...)` 之后）：

```python
                for call in turn.tool_calls:
                    result = self.registry.invoke(call)
                    self._note_progress(state, call, result)
                    self._emit(
                        "tool",
                        {
                            "name": call.name,
                            "ok": result.ok,
                            "detail": " ".join(result.content.split())[:200],
                        },
                    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/agent -q`
Expected: PASS（全部，含原有测试）

- [ ] **Step 5: 提交**

```bash
git add src/spoolkit/agent/loop.py tests/agent/test_loop_events.py
git commit -m "feat: 主循环支持事件回调（可选，不传则行为不变）"
```

---

## Task 3: `--events` 输出器

**Files:**
- Create: `src/spoolkit/cli/events.py`
- Test: `tests/cli/test_events.py`

**Interfaces:**
- Consumes: `spoolkit.web.protocol.Event`
- Produces:
  - `spoolkit.cli.events.EventWriter(stream=None)`
    - `.emit(kind: str, **data) -> None`
    - `.handle(kind: str, data: dict) -> None`（直接作为 `on_event` 传进主循环）

- [ ] **Step 1: 写失败测试**

```python
# tests/cli/test_events.py
import io
import json
from pathlib import Path

from spoolkit.cli.events import EventWriter
from spoolkit.web.protocol import FINAL, parse_line


def test_输出一行JSON() -> None:
    buffer = io.StringIO()
    EventWriter(buffer).emit(FINAL, ok=True, text="好了")
    line = buffer.getvalue().strip()
    assert json.loads(line)["type"] == FINAL


def test_可解析回来() -> None:
    buffer = io.StringIO()
    EventWriter(buffer).emit(FINAL, ok=True, text="好了")
    parsed = parse_line(buffer.getvalue())
    assert parsed is not None
    assert parsed.data["text"] == "好了"


def test_每次输出都冲洗() -> None:
    """不 flush 的话输出会攒在缓冲区里，UI 看起来像卡住。"""

    class _Probe(io.StringIO):
        flushed = 0

        def flush(self) -> None:
            type(self).flushed += 1

    probe = _Probe()
    EventWriter(probe).emit(FINAL, ok=True, text="x")
    assert probe.flushed >= 1


def test_连续输出是多行() -> None:
    buffer = io.StringIO()
    writer = EventWriter(buffer)
    writer.emit(FINAL, ok=True, text="一")
    writer.emit(FINAL, ok=False, text="二")
    assert len(buffer.getvalue().strip().splitlines()) == 2


def test_handle可直接作为回调() -> None:
    buffer = io.StringIO()
    EventWriter(buffer).handle("step", {"n": 3})
    assert json.loads(buffer.getvalue().strip())["n"] == 3
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/cli/test_events.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'spoolkit.cli.events'`

- [ ] **Step 3: 写最小实现**

```python
# src/spoolkit/cli/events.py
"""内核侧的事件输出器。

`--events` 模式下，stdout 上只应该有 JSON 行——不夹杂任何散文。
散文会被解析器忽略，但录下来的文件也会变脏，调试时难读。

每条都 flush：不 flush 的话输出攒在缓冲区里，UI 那边看起来像卡住了，
而你会先去怀疑网络。
"""

import sys
from typing import Any, TextIO

from spoolkit.web.protocol import Event


class EventWriter:
    """把事件按行写到流上。"""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def emit(self, kind: str, **data: Any) -> None:
        self._stream.write(Event(kind, data).to_line() + "\n")
        self._stream.flush()

    def handle(self, kind: str, data: dict) -> None:
        """签名与 AgentLoop 的 on_event 一致，可直接传进去。"""
        self.emit(kind, **data)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/cli/test_events.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/spoolkit/cli/events.py tests/cli/test_events.py
git commit -m "feat: --events 模式的事件输出器"
```

---

## Task 4: 子进程运行器

**Files:**
- Create: `src/spoolkit/web/runner.py`
- Test: `tests/web/test_runner.py`

**Interfaces:**
- Consumes: `parse_line`、`Event`
- Produces:
  - `spoolkit.web.runner.Runner(project_root: Path, session: str, extra_args: Sequence[str] = ())`
    - `.start(goal: str) -> bool`（已有运行在跑时返回 False）
    - `.confirm(apply: bool) -> bool`（没有待确认时返回 False）
    - `.subscribe() -> queue.Queue`、`.unsubscribe(q) -> None`
    - `.snapshot() -> dict`、`.running` 属性
    - `.command(goal)` 属性方法，便于测试断言命令拼装

**设计要点**：读取放在后台线程。主线程必须能随时响应 HTTP 请求——尤其是**确认**，它不能等 stdout 读完了才处理。

- [ ] **Step 1: 写失败测试**

```python
# tests/web/test_runner.py
import sys
import time
from pathlib import Path

from spoolkit.web.protocol import Event
from spoolkit.web.runner import Runner


def _runner(tmp_path: Path, *extra: str) -> Runner:
    return Runner(tmp_path, session="t", extra_args=list(extra))


def test_命令拼装包含事件模式(tmp_path: Path) -> None:
    command = _runner(tmp_path).command("看看代码")
    assert command[0] == sys.executable
    assert "spoolkit.cli.app" in command
    assert "run" in command
    assert "--events" in command
    assert "--goal" in command
    assert "看看代码" in command


def test_初始状态是空闲的(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    assert runner.running is False
    assert runner.snapshot()["running"] is False


def test_订阅者在运行结束后仍能收到事件(tmp_path: Path) -> None:
    """一次极短的运行：起、等、看队列里有没有 final。"""
    runner = _runner(tmp_path, "--provider", "fake", "--script", "")
    # 假模型缺脚本会立刻失败退出，正好用来测「子进程非零退出也有结局」
    assert runner.start("无所谓") is True
    queue = runner.subscribe()
    deadline = time.time() + 20
    seen_final = False
    while time.time() < deadline:
        try:
            event = queue.get(timeout=0.5)
        except Exception:
            continue
        if event.type == "final":
            seen_final = True
            break
        if not runner.running:
            break
    assert seen_final or runner.snapshot()["finished"] is True
    runner.unsubscribe(queue)


def test_运行中再次启动被拒绝(tmp_path: Path) -> None:
    runner = _runner(tmp_path, "--provider", "fake", "--script", "")
    runner.start("第一次")
    assert runner.start("第二次") is False


def test_没有待确认时确认请求被拒绝(tmp_path: Path) -> None:
    assert _runner(tmp_path).confirm(True) is False


def test_快照包含关键字段(tmp_path: Path) -> None:
    snapshot = _runner(tmp_path).snapshot()
    for key in ("running", "finished", "awaiting", "goal", "usage", "final"):
        assert key in snapshot


def test_开始后快照记录目标(tmp_path: Path) -> None:
    runner = _runner(tmp_path, "--provider", "fake", "--script", "")
    runner.start("这件事")
    assert runner.snapshot()["goal"] == "这件事"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/web/test_runner.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'spoolkit.web.runner'`

- [ ] **Step 3: 写最小实现**

```python
# src/spoolkit/web/runner.py
"""子进程运行器。

一次只跑一个运行。这不是偷懒：并发要处理多个待确认队列怎么合并、
状态归属哪个运行，而这是本地单人用的壳——那些复杂度换不来任何东西。

读取放在后台线程。主线程必须能随时响应 HTTP 请求，尤其是「确认」——
它不能等 stdout 读完才被处理，否则点了按钮界面会僵住。
"""

import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Sequence

from spoolkit.web.protocol import (
    AWAIT,
    ERROR,
    FINAL,
    USAGE,
    Event,
    parse_line,
)

KEEPALIVE_HINT = 200


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
        """拼出要执行的命令行。"""
        return [
            sys.executable,
            "-m",
            "spoolkit.cli.app",
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
        """当前状态。SSE 每次连接的首帧就是它。"""
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
        if self._final is None:
            # 子进程没给结局就退出了（崩了、被杀了）。**必须补一条结局**，
            # 否则界面会永远停在「运行中」，而你会以为它还在干活。
            self._broadcast(
                Event(
                    FINAL,
                    {"ok": False, "text": f"进程退出，退出码 {code}"},
                )
            )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/web -q`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add src/spoolkit/web/runner.py tests/web/test_runner.py
git commit -m "feat: Web UI 的子进程运行器"
```

---

## Task 5: HTTP 服务与 SSE

**Files:**
- Create: `src/spoolkit/web/server.py`
- Test: `tests/web/test_server.py`

**Interfaces:**
- Consumes: `Runner`、`Event`、`page.HTML`
- Produces:
  - `spoolkit.web.server.build_server(runner, host, port) -> ThreadingHTTPServer`
  - `spoolkit.web.server.serve(project_root, host, port, session, extra_args) -> None`

**两个实现要点，写代码时不能踩**：必须用 `ThreadingHTTPServer`（默认的单线程服务会让一个 SSE 连接把整个服务堵死）；SSE 写完必须 flush（否则事件攒在缓冲里，界面看起来像卡住）。

- [ ] **Step 1: 写失败测试**

```python
# tests/web/test_server.py
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.web.runner import Runner
from spoolkit.web.server import build_server


@pytest.fixture
def server(tmp_path: Path):
    runner = Runner(tmp_path, session="t", extra_args=["--provider", "fake", "--script", ""])
    httpd = build_server(runner, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_首页返回HTML(server: str) -> None:
    status, body = _get(server + "/")
    assert status == 200
    assert "<html" in body.lower()


def test_状态接口返回快照(server: str) -> None:
    status, body = _get(server + "/state")
    assert status == 200
    assert json.loads(body)["running"] is False


def test_发起运行返回202(server: str) -> None:
    status, _ = _post(server + "/run", {"goal": "做点事"})
    assert status == 202


def test_缺少目标返回400(server: str) -> None:
    assert _post(server + "/run", {})[0] == 400
    assert _post(server + "/run", {"goal": "   "})[0] == 400


def test_重复发起返回409(server: str) -> None:
    assert _post(server + "/run", {"goal": "第一次"})[0] == 202
    assert _post(server + "/run", {"goal": "第二次"})[0] == 409


def test_没有待确认时确认返回409(server: str) -> None:
    assert _post(server + "/confirm", {"apply": True})[0] == 409


def test_未知路径返回404(server: str) -> None:
    assert _get(server + "/不存在")[0] == 404


def test_事件流首帧是状态快照(server: str) -> None:
    """重连时不补发事件，所以每次连接都要先把完整状态给过去。"""
    with urllib.request.urlopen(server + "/events", timeout=10) as response:
        assert response.status == 200
        assert "text/event-stream" in response.headers["Content-Type"]
        chunk = response.read(400).decode("utf-8")
    assert "state" in chunk
    assert "running" in chunk
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/web/test_server.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'spoolkit.web.server'`

- [ ] **Step 3: 写最小实现**

```python
# src/spoolkit/web/server.py
"""HTTP 服务与 SSE 广播。

必须用 ThreadingHTTPServer：默认的单线程服务会让一个 SSE 长连接
把整个服务堵死——连首页都打不开，而你会以为服务崩了。

SSE 每写完一条都要 flush：不 flush 的话事件攒在缓冲里，
界面看起来像卡住。
"""

import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

from spoolkit.web.page import HTML
from spoolkit.web.runner import Runner

KEEPALIVE_SECONDS = 15
MAX_BODY = 64 * 1024


def _handler_for(runner: Runner):
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

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            if self.path == "/":
                self._html(HTML)
            elif self.path == "/state":
                self._json(200, runner.snapshot())
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
            self._push({"type": "state", **runner.snapshot()})
            listener = runner.subscribe()
            try:
                while True:
                    try:
                        event = listener.get(timeout=KEEPALIVE_SECONDS)
                    except queue.Empty:
                        # 心跳：代理和浏览器会掐掉长时间没数据的连接。
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._push({"type": event.type, **event.data})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                runner.unsubscribe(listener)

        def _push(self, payload: dict) -> None:
            line = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
            self.wfile.flush()

        # --- POST ---

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/run":
                payload = self._body()
                goal = str(payload.get("goal") or "").strip()
                if not goal:
                    self._json(400, {"error": "缺少 goal"})
                    return
                if not runner.start(goal):
                    self._json(409, {"error": "已有运行在进行，等它结束再发"})
                    return
                self._json(202, {"ok": True})
                return

            if self.path == "/confirm":
                payload = self._body()
                if not runner.confirm(bool(payload.get("apply"))):
                    self._json(409, {"error": "当前没有待确认的改动"})
                    return
                self._json(200, {"ok": True})
                return

            self._json(404, {"error": "没有这个路径"})

    return Handler


def build_server(runner: Runner, host: str, port: int) -> ThreadingHTTPServer:
    """构造服务。port 传 0 表示由系统分配。"""
    return ThreadingHTTPServer((host, port), _handler_for(runner))


def serve(
    project_root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    session: str = "cli",
    extra_args: Sequence[str] = (),
) -> None:
    """启动服务，直到被中断。"""
    runner = Runner(project_root, session=session, extra_args=list(extra_args))
    httpd = build_server(runner, host, port)
    print(f"Web UI: http://{host}:{httpd.server_port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/web -q`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add src/spoolkit/web/server.py tests/web/test_server.py
git commit -m "feat: Web UI 的 HTTP 服务与 SSE 广播"
```

---

## Task 6: 前端页面

**Files:**
- Create: `src/spoolkit/web/page.py`
- Test: `tests/web/test_page.py`

**Interfaces:**
- Consumes: 无
- Produces: `spoolkit.web.page.HTML: str`

布局是选定的 B：左右分栏，**右侧待确认面板固定**，不随输出滚走。

**前端刻意做得很薄**：不持有状态、不做业务判断，只负责渲染事件和发三个请求。所有逻辑在后端，那里能自动测——**前端出错只能靠人看出来，所以它必须小到一眼能看完**。

- [ ] **Step 1: 写失败测试**

```python
# tests/web/test_page.py
from spoolkit.web.page import HTML


def test_页面是一个完整的HTML文档() -> None:
    assert HTML.lstrip().lower().startswith("<!doctype")
    assert "</html>" in HTML.lower()


def test_页面连接到事件流() -> None:
    assert "/events" in HTML
    assert "EventSource" in HTML


def test_页面能发起运行与确认() -> None:
    assert "/run" in HTML
    assert "/confirm" in HTML


def test_页面有待确认面板() -> None:
    assert "pending" in HTML
    assert "应用" in HTML
    assert "拒绝" in HTML


def test_页面在事件流断开时提示() -> None:
    """连不上却不说话，你会以为任务没跑。"""
    assert "onerror" in HTML
    assert "断开" in HTML


def test_页面没有外链资源() -> None:
    """单文件分发：不引 CDN、不引构建产物。"""
    assert "<link" not in HTML.lower()
    assert "src=" not in HTML.lower()
    assert "cdn" not in HTML.lower()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/web/test_page.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'spoolkit.web.page'`

- [ ] **Step 3: 写最小实现**

```python
# src/spoolkit/web/page.py
"""内嵌的单页 HTML。

做成 Python 字符串而不是外置文件：跑起来只有一个东西要分发，也避免
打包时漏文件。代价是编辑时没有语法高亮，可以接受。

这里刻意做得很薄——不持有状态、不做业务判断，只渲染事件和发三个请求。
前端出错只能靠人看出来，所以它必须小到一眼能看完。
"""

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>spool</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:13px/1.6 ui-monospace,Consolas,monospace;
         background:#0e1116; color:#c9d1d9; height:100vh; display:flex; flex-direction:column; }
  header { display:flex; gap:8px; align-items:center; padding:10px 14px;
           border-bottom:1px solid #232a35; background:#131820; }
  header .meta { color:#8b949e; white-space:nowrap; }
  header input { flex:1; padding:7px 10px; border-radius:6px;
                 border:1px solid #30363d; background:#0d1117; color:inherit; }
  header button { padding:7px 16px; border-radius:6px; border:0;
                  background:#2f81f7; color:#fff; cursor:pointer; }
  header button:disabled { background:#21262d; color:#6e7681; cursor:default; }
  main { flex:1; display:flex; min-height:0; }
  #stream { flex:1.2; overflow-y:auto; padding:12px 14px; }
  #side { flex:1; border-left:1px solid #232a35; background:#101722;
          padding:12px 14px; overflow-y:auto; }
  .line { white-space:pre-wrap; word-break:break-word; }
  .ok { color:#7fa650; } .bad { color:#c96a6a; } .dim { color:#8b949e; }
  .diff { border:1px solid #30363d; border-radius:6px; padding:8px;
          margin:8px 0; background:#0d1117; overflow-x:auto; }
  .diff .add { color:#7fa650; } .diff .del { color:#c96a6a; }
  #actions { display:none; gap:8px; margin-top:10px; }
  #actions button { padding:6px 14px; border-radius:6px; border:0; cursor:pointer; }
  #apply { background:#238636; color:#fff; }
  #reject { background:#30363d; color:#c9d1d9; }
  #conn { position:fixed; right:12px; bottom:10px; color:#c96a6a; display:none; }
</style>
</head>
<body>
<header>
  <span class="meta" id="meta">连接中…</span>
  <input id="goal" placeholder="要 agent 做什么…">
  <button id="start">开始</button>
</header>
<main>
  <div id="stream"></div>
  <div id="side">
    <div id="pending" class="dim">当前没有待确认的改动。</div>
    <div id="actions">
      <button id="apply">应用</button>
      <button id="reject">拒绝</button>
    </div>
  </div>
</main>
<div id="conn">与服务的连接断开了，重连中…</div>
<script>
const stream = document.getElementById('stream');
const pending = document.getElementById('pending');
const actions = document.getElementById('actions');
const meta = document.getElementById('meta');
const conn = document.getElementById('conn');
const startBtn = document.getElementById('start');
let running = false, decided = false;

function add(text, cls) {
  const div = document.createElement('div');
  div.className = 'line ' + (cls || '');
  div.textContent = text;
  stream.appendChild(div);
  stream.scrollTop = stream.scrollHeight;
}

function showDiff(path, text) {
  const box = document.createElement('div');
  box.className = 'diff';
  for (const raw of text.split('\\n')) {
    const line = document.createElement('div');
    line.className = raw.startsWith('+') ? 'add' : raw.startsWith('-') ? 'del' : 'dim';
    line.textContent = raw;
    box.appendChild(line);
  }
  pending.innerHTML = '';
  pending.className = '';
  pending.appendChild(document.createTextNode('待确认：' + path));
  pending.appendChild(box);
}

function setRunning(value) {
  running = value;
  startBtn.disabled = value;
  startBtn.textContent = value ? '运行中…' : '开始';
}

function applyState(state) {
  meta.textContent = state.session + ' · ' + (state.usage.steps || 0) + ' 步';
  setRunning(state.running);
  if (!state.awaiting) {
    actions.style.display = 'none';
  }
}

function onEvent(event) {
  const data = JSON.parse(event.data);
  if (data.type === 'state') { applyState(data); return; }
  if (data.type === 'step') { add('── 第 ' + data.n + ' 步', 'dim'); return; }
  if (data.type === 'tool') {
    add('  ' + data.name + ' → ' + (data.ok ? '成功' : '失败'), data.ok ? 'ok' : 'bad');
    return;
  }
  if (data.type === 'diff') { showDiff(data.path, data.text); return; }
  if (data.type === 'await') {
    decided = false;
    actions.style.display = 'flex';
    document.getElementById('apply').disabled = false;
    document.getElementById('reject').disabled = false;
    return;
  }
  if (data.type === 'usage') { add('用量：' + JSON.stringify(data), 'dim'); return; }
  if (data.type === 'final') {
    add(data.ok ? '完成：' + data.text : '失败：' + data.text, data.ok ? 'ok' : 'bad');
    setRunning(false);
    actions.style.display = 'none';
    return;
  }
  if (data.type === 'error') { add('错误：' + data.message, 'bad'); }
}

function connect() {
  const source = new EventSource('/events');
  source.onmessage = onEvent;
  source.onerror = () => { conn.style.display = 'block'; };
  source.onopen = () => { conn.style.display = 'none'; };
}

async function post(url, payload) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    add('请求被拒绝：' + (detail.error || response.status), 'bad');
  }
}

startBtn.onclick = () => {
  const goal = document.getElementById('goal').value.trim();
  if (!goal || running) { return; }
  add('▶ ' + goal);
  post('/run', {goal});
};

function decide(apply) {
  // 点一次就禁用：重复写 stdin 会让后续的 input() 拿到意外的输入。
  if (decided) { return; }
  decided = true;
  document.getElementById('apply').disabled = true;
  document.getElementById('reject').disabled = true;
  post('/confirm', {apply});
}

document.getElementById('apply').onclick = () => decide(true);
document.getElementById('reject').onclick = () => decide(false);

connect();
</script>
</body>
</html>
"""
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/web/test_page.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add src/spoolkit/web/page.py tests/web/test_page.py
git commit -m "feat: Web UI 的页面（左右分栏，右栏固定待确认面板）"
```

---

## Task 7: CLI 接线

**Files:**
- Modify: `src/spoolkit/cli/commands/run.py`
- Create: `src/spoolkit/cli/commands/serve.py`
- Modify: `src/spoolkit/cli/app.py`
- Modify: `src/spoolkit/cli/events.py`
- Test: `tests/cli/test_events_mode.py`

**Interfaces:**
- Consumes: `EventWriter`、`serve`、`settle`
- Produces:
  - `run --events` 时，stdout 上只有 JSON 行
  - `spoolkit serve [--host] [--port] [--session]`
  - `spoolkit.cli.events.settle_with_events(pending, policy, scope, baseline_path, writer, reader) -> str`

**events 模式下的确认**：策略是 `auto` 且改动在范围内时，仍然自动落盘，**不发 `await`**——UI 只看到 diff 和 `confirm`，没有按钮。其余情况发 `await` 并**从 stdin 读一行**。这跟终端模式的语义完全一致，只是换了输入输出通道。

- [ ] **Step 1: 写失败测试**

```python
# tests/cli/test_events_mode.py
import io
import json
from pathlib import Path

from spoolkit.cli.events import EventWriter, settle_with_events
from spoolkit.policy import ASK, AUTO
from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.web.protocol import AWAIT, CONFIRM, DIFF


def _stage(tmp_path: Path, path: str = "src/a.py") -> PendingChanges:
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": path, "content": "x = 1\n"}
    )
    return pending


def _lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().strip().splitlines()]


def test_询问策略下发diff与await(tmp_path: Path) -> None:
    buffer = io.StringIO()
    pending = _stage(tmp_path)
    settle_with_events(
        pending, ASK, (), None, EventWriter(buffer), io.StringIO("y\n")
    )
    kinds = [line["type"] for line in _lines(buffer)]
    assert DIFF in kinds
    assert AWAIT in kinds
    assert CONFIRM in kinds


def test_回答y会落盘(tmp_path: Path) -> None:
    buffer = io.StringIO()
    pending = _stage(tmp_path)
    settle_with_events(
        pending, ASK, (), None, EventWriter(buffer), io.StringIO("y\n")
    )
    assert (tmp_path / "src" / "a.py").exists()


def test_回答n不落盘(tmp_path: Path) -> None:
    buffer = io.StringIO()
    pending = _stage(tmp_path)
    settle_with_events(
        pending, ASK, (), None, EventWriter(buffer), io.StringIO("n\n")
    )
    assert not (tmp_path / "src" / "a.py").exists()


def test_范围之外的改动也要问(tmp_path: Path) -> None:
    buffer = io.StringIO()
    pending = _stage(tmp_path, "docs/b.md")
    settle_with_events(
        pending, AUTO, ("src",), None, EventWriter(buffer), io.StringIO("n\n")
    )
    assert AWAIT in [line["type"] for line in _lines(buffer)]


def test_范围内自动落盘时不发await(tmp_path: Path) -> None:
    """auto 且范围内 → 自动应用。UI 只看到 diff 与 confirm，不该出按钮。"""
    buffer = io.StringIO()
    pending = _stage(tmp_path)
    settle_with_events(pending, AUTO, ("src",), None, EventWriter(buffer), io.StringIO(""))
    lines = _lines(buffer)
    assert AWAIT not in [line["type"] for line in lines]
    assert any(line["type"] == CONFIRM and line["applied"] for line in lines)
    assert (tmp_path / "src" / "a.py").exists()


def test_没有改动时不输出任何事件(tmp_path: Path) -> None:
    buffer = io.StringIO()
    settle_with_events(
        PendingChanges(tmp_path), ASK, (), None, EventWriter(buffer), io.StringIO("y\n")
    )
    assert buffer.getvalue() == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/cli/test_events_mode.py -q`
Expected: FAIL，`ImportError: cannot import name 'settle_with_events'`

- [ ] **Step 3: 写最小实现**

在 `web/protocol.py` 中补一个事件类型：

```python
CONFIRM = "confirm"
```

并把它加进 `KNOWN`。

在 `cli/events.py` 末尾追加：

```python
from pathlib import Path
from typing import Callable, Sequence

from spoolkit.agents.plan import out_of_scope
from spoolkit.cli.approval import apply_with_audit
from spoolkit.policy import AUTO
from spoolkit.tools.edit import PendingChanges
from spoolkit.web.protocol import AWAIT, CONFIRM, DIFF


def settle_with_events(
    pending: PendingChanges,
    policy: str,
    scope: Sequence[str],
    baseline_path: Path | None,
    writer: "EventWriter",
    reader: Callable[[], str] | object = None,
) -> str:
    """按策略处理待落盘改动，全程以事件表达。

    语义与终端模式完全一致，只是把「打印 diff 并问一句」换成了
    「发 diff 与 await，再从 stdin 读一行」。两条路径的判定必须一样，
    否则同一个策略在两种界面下表现不同——那种差异不会报错，只会让人困惑。
    """
    changes = pending.items()
    if not changes:
        return "none"

    for change in changes:
        writer.emit(DIFF, path=change.path, text=change.diff)

    if policy == AUTO and not out_of_scope([c.path for c in changes], scope):
        written = apply_with_audit(pending, baseline_path)
        writer.emit(CONFIRM, applied=True, auto=True, count=len(written))
        return "auto"

    writer.emit(AWAIT, count=len(changes))
    source = reader if reader is not None else sys.stdin
    answer = (source.readline() or "").strip().lower()
    apply = answer in ("y", "yes")
    writer.emit(CONFIRM, applied=apply, count=len(changes))
    if apply:
        if baseline_path is not None:
            from spoolkit.tools.edit import save_baseline

            save_baseline(baseline_path, pending.baseline())
        pending.apply()
        return "confirmed"
    pending.discard()
    return "declined"
```

创建 `cli/commands/serve.py`：

```python
"""启动 Web UI 壳。"""

import argparse
from pathlib import Path

from spoolkit.web.server import serve


def serve_command(args: argparse.Namespace) -> int:
    extra: list[str] = []
    if args.provider:
        extra += ["--provider", args.provider]
    if args.model:
        extra += ["--model", args.model]
    if args.proxy:
        extra += ["--proxy", args.proxy]
    if args.policy:
        extra += ["--policy", args.policy]
    if args.scope:
        extra += ["--scope", args.scope]
    serve(
        Path(args.root).resolve(),
        host=args.host,
        port=args.port,
        session=args.session,
        extra_args=extra,
    )
    return 0
```

在 `cli/runtime.py` 的 `LoopWiring` 加一个字段，并在 `assemble_loop` 里透传：

```python
@dataclass
class LoopWiring:
    ...
    lessons: object | None = None
    on_event: object | None = None      # 新增


    return AgentLoop(
        ...
        distiller=parts.distiller,
        on_event=parts.on_event,        # 新增
    )
```

在 `cli/app.py` 的 `_add_run_command` 里加开关：

```python
    parser.add_argument(
        "--events",
        action="store_true",
        help="以 JSON 行输出事件，供 Web UI 消费；此模式下不打印散文",
    )
```

并加一条子命令：

```python
def _add_serve_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("serve", help="启动 Web UI 壳")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--session", default="cli")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--policy", default="")
    parser.add_argument("--scope", default="")
    parser.add_argument("--root", default=".")
    parser.set_defaults(func=serve_command)
```

在 `main()` 里调用 `_add_serve_command(sub)`，并导入 `serve_command`。

在 `cli/commands/run.py` 的 `run` 里分流：

```python
    if args.events:
        return _events_mode(args, project_root, gateway, window, pending)
```

新增：

```python
def _events_mode(args, project_root, gateway, window, pending) -> int:
    """`--events` 模式：stdout 上只有 JSON 行。

    与散文模式共用同一套装配与同一个主循环，只换了输入输出通道。
    两条路径的判定必须一致——同一个策略在终端和网页里表现不同，
    那种差异不会报错，只会让人困惑。
    """
    writer = EventWriter()
    policy = resolve_policy(args, project_root)
    scope = resolve_scope(args)
    writer.emit(
        START, session=args.session, goal=args.goal, policy=policy, window=window
    )

    memory = None if args.no_memory else open_memory(
        project_root, window, args.session,
        model=f"{args.provider}:{args.model or '默认'}",
    )
    loop = assemble_loop(
        project_root,
        gateway,
        config=Config(
            project_root=project_root,
            context_window=window,
            max_steps=args.max_steps,
            subagent_steps=args.subagent_steps,
        ),
        wiring=LoopWiring(
            memory=memory,
            pending=pending,
            lessons=build_lessons(memory) if memory is not None else None,
            on_event=writer.handle,
        ),
    )
    result = loop.run(args.goal, resume=args.resume)

    writer.emit(
        USAGE,
        steps=result.steps,
        calls=result.model_calls,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    outcome = settle_with_events(
        pending, policy, scope, project_root / ".agent" / "last_change.json", writer
    )
    writer.emit(
        FINAL,
        ok=result.finished,
        text=result.final or "",
        settled=outcome,
    )
    return 0 if result.finished else 1
```

导入区补上：

```python
from spoolkit.cli.events import EventWriter, settle_with_events
from spoolkit.web.protocol import FINAL, START, USAGE
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add src/spoolkit/cli/ src/spoolkit/web/protocol.py tests/cli/test_events_mode.py
git commit -m "feat: --events 模式与 serve 命令接线"
```

---

## Task 8: 错误路径

**Files:**
- Test: `tests/web/test_failure_paths.py`
- Create: `tests/web/fake_agent.py`
- Modify: `src/spoolkit/web/runner.py`（仅在测试暴露问题时改）

**Interfaces:**
- Consumes: `Runner`、`build_server`
- Produces: 无新接口，只补行为保证

四种情况都必须有明确行为，且都要有测试。

- [ ] **Step 1: 写失败测试**

```python
# tests/web/fake_agent.py
"""测试用的假 agent。

发一条 diff 与 await，读一行 stdin，再发 final。

用它而不是真 agent：确认流程的时序是这块最容易出错的地方，而真 agent
要跑几十秒、还要联网。假 agent 把时序压到毫秒级，能反复跑。
"""

import json
import sys


def emit(**payload) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


emit(type="diff", path="a.py", text="+新\n-旧")
emit(type="await", count=1)
answer = (sys.stdin.readline() or "").strip()
emit(type="confirm", applied=answer == "y")
emit(type="final", ok=True, text="收到 " + answer)
```

```python
# tests/web/test_failure_paths.py
import sys
import time
from pathlib import Path

from spoolkit.web.protocol import FINAL
from spoolkit.web.runner import Runner

FAKE = Path(__file__).resolve().parent / "fake_agent.py"


class _Scripted(Runner):
    """用假 agent 脚本替换真实命令行，其余逻辑完全一致。"""

    def command(self, goal: str) -> list[str]:
        return [sys.executable, str(FAKE)]


class _Silent(Runner):
    """跑完什么都不发的子进程，用来验证「必须补一条结局」。"""

    def command(self, goal: str) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.exit(3)"]


def _wait(predicate, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _finals(listener) -> list:
    items = []
    while True:
        try:
            items.append(listener.get_nowait())
        except Exception:
            break
    return [event for event in items if event.type == FINAL]


def test_假agent能走到等待确认(tmp_path: Path) -> None:
    runner = _Scripted(tmp_path)
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)


def test_确认y会写进子进程并得到回应(tmp_path: Path) -> None:
    """这条走的是真实的 stdin 管道——不是模拟，是真写进去。"""
    runner = _Scripted(tmp_path)
    listener = runner.subscribe()
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)
    assert runner.confirm(True) is True
    assert _wait(lambda: not runner.running)
    finals = _finals(listener)
    assert finals and "收到 y" in finals[-1].data["text"]


def test_确认n也会写进去(tmp_path: Path) -> None:
    runner = _Scripted(tmp_path)
    listener = runner.subscribe()
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)
    assert runner.confirm(False) is True
    assert _wait(lambda: not runner.running)
    assert "收到 n" in _finals(listener)[-1].data["text"]


def test_反复确认只生效一次(tmp_path: Path) -> None:
    """重复写 stdin 会让后续的 input() 拿到意外的输入，那类错位极难查。"""
    runner = _Scripted(tmp_path)
    runner.start("做点事")
    assert _wait(lambda: runner.snapshot()["awaiting"] == 1)
    assert runner.confirm(True) is True
    assert runner.confirm(True) is False


def test_没有等待时确认被拒绝(tmp_path: Path) -> None:
    assert _Scripted(tmp_path).confirm(True) is False


def test_子进程没给结局时会补一条失败结局(tmp_path: Path) -> None:
    """不给结局的话界面会永远停在「运行中」，而你以为它还在干活。"""
    runner = _Silent(tmp_path)
    listener = runner.subscribe()
    runner.start("做点事")
    assert _wait(lambda: not runner.running)
    finals = _finals(listener)
    assert finals and finals[-1].data["ok"] is False


def test_结束后可以再发一次(tmp_path: Path) -> None:
    """崩过一次不该把服务卡死。"""
    runner = _Scripted(tmp_path)
    runner.start("第一次")
    assert _wait(lambda: not runner.running)
    assert runner.start("第二次") is True


def test_订阅者断开后不再收到事件(tmp_path: Path) -> None:
    runner = _Scripted(tmp_path)
    listener = runner.subscribe()
    runner.unsubscribe(listener)
    runner.start("做点事")
    time.sleep(0.5)
    assert listener.empty()
```

- [ ] **Step 2: 运行测试**

Run: `.venv\Scripts\python.exe -m pytest tests/web/test_failure_paths.py -q`
Expected: PASS。若有失败，先判断是测试写错还是实现缺行为，**不要为了让测试变绿而放宽断言**。

- [ ] **Step 3: 人工验证两种无法自动测的情况**

在浏览器里逐一确认：

1. **SSE 断线重连** —— 运行中把服务停掉再起来，观察页面显示「连接断开」并在恢复后自动补上状态。
2. **重复点击确认按钮** —— 出现待确认时连点两次「应用」，确认只发了一次请求、且没有异常。

- [ ] **Step 4: 跑全量测试与回归集**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: PASS

Run: `.venv\Scripts\python.exe -m spoolkit.cli.app bench --provider gemini --root .`
Expected: 验收通过 10/10（这部分改动理论上不碰内核行为，若回归集掉了，说明事件回调那一步动了不该动的东西）

- [ ] **Step 5: 提交**

```bash
git add tests/web/test_failure_paths.py src/spoolkit/web/
git commit -m "test: Web UI 的错误路径覆盖"
```

---

## 完成标准

1. `pytest` 全绿，原有 572 个测试不回归。
2. 回归集仍然 10/10——这条是防止事件回调改动内核行为的守卫。
3. `spool serve` 起来后，浏览器里能完整跑通：发任务 → 看实时输出 → 看到 diff → 点应用或拒绝 → 看到最终结果。
4. 四种错误路径都有测试或人工验证记录。
5. 不新增任何依赖。

## 后续（不在本计划范围）

- 会话切换与策略编辑的界面入口
- 历史记录浏览（复用 `.agent/memory.db` 的 transcript 表）
- 视觉风格与动效
