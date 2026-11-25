# 模型等待心跳 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 任意一次 `gateway.chat()` 等待满 15 秒后发出心跳，CLI 走 stderr、`--events` 走 `wait` JSON，让人看见模型还在生成。

**Architecture:** `WaitingGateway` 只拦 `chat()`，用 `threading.Event.wait(15)` 做可中断间隔；`provider_gateway` 返回前包上它。监听器在命令入口按模式绑（散文 / 事件）。HTTP 仍 `stream: false`，超时公式不动。

**Tech Stack:** 现有 Python 标准库（`threading`）、httpx 客户端保持非流式、现有事件协议与网页壳。

## Global Constraints

- 不新增第三方依赖。
- 不改 `SECONDS_PER_OUTPUT_TOKEN`、`_timeout_for`、不设墙钟空闲杀请求。
- 不接 SSE、不把 token 打到屏幕。
- 心跳间隔写死 15 秒，不进 `limits.KNOBS`。
- 标识符英文，注释与面向人的文案中文。
- **测试在沙箱内单独跑，git 提交在沙箱外单独执行，两者绝不合并成一条命令。** 未明确要求提交则不要 commit。
- 每个任务自带 pytest；全程不打真 llama-server。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/spoolkit/llm/gateway.py` | 新增 `HEARTBEAT_SECONDS`、`wait_line`、`WaitingGateway`、`attach_wait_listener` |
| `src/spoolkit/cli/runtime.py` | `provider_gateway` 返回前包 `WaitingGateway`；新增 `bind_model_wait` |
| `src/spoolkit/cli/commands/run.py` | `provider_gateway` 成功后立刻 `bind_model_wait` |
| `src/spoolkit/cli/commands/plan.py` | `make_plan` / `enter_autonomous` / `enter_advance` 同样绑 |
| `src/spoolkit/cli/commands/chat.py` | `chat_command` 同样绑 |
| `src/spoolkit/web/protocol.py` | 登记 `WAIT = "wait"` 进 `KNOWN` |
| `src/spoolkit/web/page.py` | 事件流灰字显示心跳；不碰 `#health` |
| `tests/llm/test_waiting.py` | 等待器次数、抛错停线程、回调抛错不打崩、`_timeout_for` 透传 |
| `tests/web/test_protocol.py` | `wait` 能解析；无 `limit` 也能解析 |
| `tests/web/test_runner.py` | `_absorb(wait)` 不改 `awaiting` |
| `tests/web/test_page.py` | 页面处理 `wait`，文案与探活横幅分离 |
| `tests/cli/test_waiting_bind.py` | 散文写 stderr；events 发 JSON |
| `docs/getting-started.md` / `README.md` / `docs/dogfooding.md` | 故障表与已知限制 |

Spec：`docs/superpowers/specs/2026-09-21-model-wait-heartbeat-design.md`

---

### Task 1: WaitingGateway

**Files:**
- Modify: `src/spoolkit/llm/gateway.py`
- Test: `tests/llm/test_waiting.py`

**Interfaces:**
- Consumes: `ChatRequest` / `ChatResponse`（`spoolkit.llm.types`）；inner 可选 `_timeout_for(max_tokens: int) -> float`
- Produces:
  - `HEARTBEAT_SECONDS: float = 15.0`
  - `wait_line(elapsed: int, limit: float | None) -> str`
  - `WaitingGateway(inner, on_wait=None, heartbeat=HEARTBEAT_SECONDS, clock=time.monotonic)`
  - `WaitingGateway.chat(request: ChatRequest) -> ChatResponse`
  - `WaitingGateway.on_wait: Callable[[int, float | None], None] | None`
  - `attach_wait_listener(gateway, on_wait) -> None`（透过 `CountingGateway._inner` 找到 `WaitingGateway`）

`on_wait(elapsed, limit)`：`elapsed` 为从本次 `chat()` 起算的整秒；没有上限时 `limit` 为 `None`。

心跳线程用 `stop.wait(self._heartbeat)`：`stop.set()` 必须立刻唤醒，不能用不可打断的 `time.sleep`。

- [ ] **Step 1: Write the failing tests**

Create `tests/llm/test_waiting.py`:

```python
import threading
import time

import pytest

from spoolkit.llm.gateway import (
    HEARTBEAT_SECONDS,
    CountingGateway,
    WaitingGateway,
    attach_wait_listener,
    wait_line,
)
from spoolkit.llm.types import ChatRequest, ChatResponse, Message


def _request() -> ChatRequest:
    return ChatRequest(messages=(Message(role="user", content="x"),), max_tokens=256)


def _ok() -> ChatResponse:
    return ChatResponse(text="ok", prompt_tokens=1, completion_tokens=1)


class _Immediate:
    def chat(self, request):
        return _ok()


class _Block:
    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate

    def chat(self, request):
        self.gate.wait(timeout=5)
        return _ok()

    def _timeout_for(self, max_tokens: int) -> float:
        return 180.0


class _Boom:
    def chat(self, request):
        raise RuntimeError("down")


def test_间隔是十五秒() -> None:
    assert HEARTBEAT_SECONDS == 15.0


def test_立刻返回零次心跳() -> None:
    ticks: list[tuple[int, float | None]] = []
    gateway = WaitingGateway(
        _Immediate(), on_wait=lambda e, lim: ticks.append((e, lim)), heartbeat=0.05
    )
    assert gateway.chat(_request()).text == "ok"
    time.sleep(0.12)
    assert ticks == []


def test_堵住后按间隔跳() -> None:
    ticks: list[tuple[int, float | None]] = []
    gate = threading.Event()
    gateway = WaitingGateway(
        _Block(gate), on_wait=lambda e, lim: ticks.append((e, lim)), heartbeat=0.05
    )
    worker = threading.Thread(target=lambda: gateway.chat(_request()))
    worker.start()
    deadline = time.monotonic() + 2.0
    while len(ticks) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    gate.set()
    worker.join(timeout=2)
    assert len(ticks) >= 2
    assert ticks[0][1] == 180.0


def test_inner抛错也停线程() -> None:
    ticks: list = []
    gateway = WaitingGateway(
        _Boom(), on_wait=lambda e, lim: ticks.append(e), heartbeat=0.05
    )
    with pytest.raises(RuntimeError, match="down"):
        gateway.chat(_request())
    n = len(ticks)
    time.sleep(0.12)
    assert len(ticks) == n


def test_回调抛错不打断chat() -> None:
    def bad(elapsed, limit):
        raise RuntimeError("listener")

    gate = threading.Event()
    gateway = WaitingGateway(_Block(gate), on_wait=bad, heartbeat=0.05)
    worker = threading.Thread(target=lambda: gateway.chat(_request()), daemon=True)
    worker.start()
    time.sleep(0.12)
    gate.set()
    worker.join(timeout=2)
    assert worker.is_alive() is False


def test_没有timeout_for时limit为None() -> None:
    ticks: list = []
    gate = threading.Event()

    class Plain:
        def chat(self, request):
            gate.wait(timeout=5)
            return _ok()

    gateway = WaitingGateway(
        Plain(), on_wait=lambda e, lim: ticks.append(lim), heartbeat=0.05
    )
    worker = threading.Thread(target=lambda: gateway.chat(_request()))
    worker.start()
    deadline = time.monotonic() + 2.0
    while not ticks and time.monotonic() < deadline:
        time.sleep(0.02)
    gate.set()
    worker.join(timeout=2)
    assert ticks
    assert ticks[0] is None


def test_透过CountingGateway绑定() -> None:
    inner = WaitingGateway(_Immediate())
    wrapped = CountingGateway(inner)
    attach_wait_listener(wrapped, lambda e, lim: None)
    assert inner.on_wait is not None


def test_wait_line有无上限() -> None:
    assert wait_line(15, None) == "模型生成中，已等待 15s"
    assert wait_line(15, 180.0) == "模型生成中，已等待 15s / 上限 180s"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/llm/test_waiting.py -q -p no:cacheprovider`

Expected: FAIL with `ImportError` / `WaitingGateway` 未定义

- [ ] **Step 3: Write minimal implementation**

Append to `src/spoolkit/llm/gateway.py`（保留现有 `ModelGateway` / `CountingGateway`）。文件顶部补 `import threading`、`import time`、`from typing import Callable`，以及 `from spoolkit.llm.types import ChatRequest, ChatResponse`（若还没有）。

```python
HEARTBEAT_SECONDS = 15.0


def wait_line(elapsed: int, limit: float | None) -> str:
    """心跳给人看的那一行。网页和 stderr 用同一句。"""
    if limit is None:
        return f"模型生成中，已等待 {elapsed}s"
    return f"模型生成中，已等待 {elapsed}s / 上限 {int(limit)}s"


class WaitingGateway:
    """chat() 等待期间按固定间隔叫醒监听器。不改超时、不读流。"""

    def __init__(
        self,
        inner: ModelGateway,
        on_wait: Callable[[int, float | None], None] | None = None,
        heartbeat: float = HEARTBEAT_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self._inner = inner
        self.on_wait = on_wait
        self._heartbeat = heartbeat
        self._clock = clock

    def chat(self, request: ChatRequest) -> ChatResponse:
        started = self._clock()
        limit = self._limit_for(request)
        stop = threading.Event()

        def tick() -> None:
            while not stop.wait(self._heartbeat):
                callback = self.on_wait
                if callback is None:
                    continue
                elapsed = int(self._clock() - started)
                try:
                    callback(elapsed, limit)
                except Exception:
                    pass

        worker = threading.Thread(target=tick, name="spool-model-wait", daemon=True)
        worker.start()
        try:
            return self._inner.chat(request)
        finally:
            stop.set()
            worker.join(timeout=1.0)

    def _limit_for(self, request: ChatRequest) -> float | None:
        fn = getattr(self._inner, "_timeout_for", None)
        if not callable(fn):
            return None
        try:
            value = fn(request.max_tokens)
        except Exception:
            return None
        if value is None:
            return None
        return float(value)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def attach_wait_listener(gateway, on_wait) -> None:
    """找到 WaitingGateway（可能被 CountingGateway 包着）并绑回调。找不到就静默。"""
    current = gateway
    for _ in range(8):
        if isinstance(current, WaitingGateway):
            current.on_wait = on_wait
            return
        current = getattr(current, "_inner", None)
        if current is None:
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/llm/test_waiting.py -q -p no:cacheprovider`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spoolkit/llm/gateway.py tests/llm/test_waiting.py
git commit -m "$(cat <<'EOF'
feat(llm): chat 等待满 15 秒发出心跳

非流式生成期间三端都无输出；先用可中断间隔标明还在等。
EOF
)"
```

---

### Task 2: 协议登记 `wait`

**Files:**
- Modify: `src/spoolkit/web/protocol.py`
- Test: `tests/web/test_protocol.py`

**Interfaces:**
- Consumes: 无（Task 1 的心跳尚未接入 stdout）
- Produces: `WAIT = "wait"`；`KNOWN` 含 `WAIT`；`parse_line` 认有/无 `limit` 的 `wait`

不加白名单，网页 `parse_line` 会丢掉心跳。

- [ ] **Step 1: Write the failing tests**

`tests/web/test_protocol.py` 的 import 加上 `WAIT`，文末追加：

```python
def test_wait事件被接受() -> None:
    parsed = parse_line(Event(WAIT, {"elapsed": 15, "limit": 491}).to_line())
    assert parsed is not None
    assert parsed.type == WAIT
    assert parsed.data["elapsed"] == 15
    assert parsed.data["limit"] == 491
    assert "type" not in parsed.data


def test_wait可以没有limit() -> None:
    parsed = parse_line(Event(WAIT, {"elapsed": 15}).to_line())
    assert parsed is not None
    assert parsed.type == WAIT
    assert "limit" not in parsed.data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/web/test_protocol.py::test_wait事件被接受 tests/web/test_protocol.py::test_wait可以没有limit -q -p no:cacheprovider`

Expected: FAIL（`WAIT` 未定义，或 `parse_line` 返回 `None`）

- [ ] **Step 3: Write minimal implementation**

In `src/spoolkit/web/protocol.py`，`NOTE` 旁增加 `WAIT = "wait"`，`KNOWN` 集合加入 `WAIT`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/web/test_protocol.py -q -p no:cacheprovider`

Expected: PASS（旧测试仍过）

- [ ] **Step 5: Commit**

```bash
git add src/spoolkit/web/protocol.py tests/web/test_protocol.py
git commit -m "$(cat <<'EOF'
feat(web): 事件协议登记 wait

未知类型会被丢掉；网页要看见心跳必须进白名单。
EOF
)"
```

---

### Task 3: 装配并绑到 CLI

**Files:**
- Modify: `src/spoolkit/cli/runtime.py`（`provider_gateway` 返回前包一层；新增 `bind_model_wait`）
- Modify: `src/spoolkit/cli/commands/run.py`
- Modify: `src/spoolkit/cli/commands/plan.py`
- Modify: `src/spoolkit/cli/commands/chat.py`
- Test: `tests/cli/test_waiting_bind.py`

**Interfaces:**
- Consumes: `WaitingGateway`、`attach_wait_listener`、`wait_line`（Task 1）；`WAIT`（Task 2）；`EventWriter`
- Produces:
  - `provider_gateway(...)` 成功时返回 `WaitingGateway`
  - `bind_model_wait(gateway, args, writer=None) -> None`
    - `getattr(args, "events", False)` 为真：`writer.emit(WAIT, elapsed=..., limit=int(limit)?)`，无 limit 则省略键
    - 否则：`print(wait_line(...), file=sys.stderr, flush=True)`

`run.py` 在 `if gateway is None: return 2` 之后立刻 `bind_model_wait(gateway, args)`，这样 `--plan` / `--autonomous` / `--events` 共用同一份监听器。

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_waiting_bind.py`:

```python
import argparse
import io
import json
from pathlib import Path

from spoolkit.cli.events import EventWriter
from spoolkit.cli.runtime import bind_model_wait, provider_gateway
from spoolkit.llm.gateway import WaitingGateway, wait_line
from spoolkit.web.protocol import WAIT, parse_line


def test_散文监听器写stderr(capsys) -> None:
    gateway = WaitingGateway(object())
    args = argparse.Namespace(events=False)
    bind_model_wait(gateway, args)
    gateway.on_wait(15, 180.0)
    err = capsys.readouterr().err
    assert wait_line(15, 180.0) in err


def test_events监听器发wait JSON() -> None:
    buf = io.StringIO()
    gateway = WaitingGateway(object())
    bind_model_wait(
        gateway, argparse.Namespace(events=True), writer=EventWriter(buf)
    )
    gateway.on_wait(15, 491.0)
    parsed = parse_line(buf.getvalue())
    assert parsed is not None
    assert parsed.type == WAIT
    assert parsed.data == {"elapsed": 15, "limit": 491}


def test_events没有limit时省略键() -> None:
    buf = io.StringIO()
    gateway = WaitingGateway(object())
    bind_model_wait(
        gateway, argparse.Namespace(events=True), writer=EventWriter(buf)
    )
    gateway.on_wait(15, None)
    parsed = parse_line(buf.getvalue())
    assert parsed is not None
    assert "limit" not in parsed.data


def test_provider_gateway包WaitingGateway(tmp_path: Path) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps(["{\"done\":true,\"final\":\"x\"}"]), encoding="utf-8")
    args = argparse.Namespace(
        provider="fake",
        script=str(script),
        model="",
        base_url="",
        proxy="",
        engine="",
    )
    gateway = provider_gateway(args, tmp_path)
    assert isinstance(gateway, WaitingGateway)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_waiting_bind.py -q -p no:cacheprovider`

Expected: FAIL（`bind_model_wait` 未定义，或返回的不是 `WaitingGateway`）

- [ ] **Step 3: Write minimal implementation**

In `src/spoolkit/cli/runtime.py`：import `WaitingGateway`、`attach_wait_listener`、`wait_line`、`WAIT`、`EventWriter`、`sys`。

`provider_gateway` 所有失败分支仍 `return None`；成功的 `return gateway` 改为 `return WaitingGateway(gateway)`。

新增：

```python
def bind_model_wait(gateway, args, writer=None) -> None:
    """按本次命令的输出模式绑心跳。漏绑等于今天（静默），不崩溃。"""
    events = bool(getattr(args, "events", False))

    def on_wait(elapsed: int, limit: float | None) -> None:
        if events:
            payload = {"elapsed": elapsed}
            if limit is not None:
                payload["limit"] = int(limit)
            stream = writer if writer is not None else EventWriter()
            stream.emit(WAIT, **payload)
            return
        print(wait_line(elapsed, limit), file=sys.stderr, flush=True)

    attach_wait_listener(gateway, on_wait)
```

`run.py`：import `bind_model_wait`，网关装载成功后立刻 `bind_model_wait(gateway, args)`。

`plan.py` 的 `make_plan` / `enter_autonomous` / `enter_advance`：同样。

`chat.py` 的 `chat_command`：同样。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_waiting_bind.py tests/llm/test_waiting.py -q -p no:cacheprovider`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spoolkit/cli/runtime.py src/spoolkit/cli/commands/run.py src/spoolkit/cli/commands/plan.py src/spoolkit/cli/commands/chat.py tests/cli/test_waiting_bind.py
git commit -m "$(cat <<'EOF'
feat(cli): 所有 chat 入口包上等待心跳

散文写 stderr，--events 发 wait JSON，同一进程只走一条路。
EOF
)"
```

---

### Task 4: 网页吸收 `wait`、灰字显示

**Files:**
- Modify: `src/spoolkit/web/page.py`（`onEvent` 增加 `wait` 分支）
- Test: `tests/web/test_runner.py`、`tests/web/test_page.py`

**Interfaces:**
- Consumes: `WAIT`（Task 2）；`wait_line` 的文案约定（Task 1）
- Produces: `_absorb(wait)` 不改 `_awaiting`；页面 `data.type === 'wait'` 时 `add(..., 'dim')`；脚本里仍不能出现 `<` 字符

QQ 桥只看 `finished` / `awaiting`，本任务不改 bridge。`wait` 进事件日志即可。

- [ ] **Step 1: Write the failing tests**

Append to `tests/web/test_runner.py`:

```python
from spoolkit.web.protocol import AWAIT, WAIT, Event


def test_wait事件不改变awaiting(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner._absorb(Event(WAIT, {"elapsed": 15, "limit": 491}))
    assert runner.snapshot()["awaiting"] == 0
    runner._absorb(Event(AWAIT, {"count": 1, "command": "whoami"}))
    runner._absorb(Event(WAIT, {"elapsed": 30}))
    assert runner.snapshot()["awaiting"] == 1
```

Append to `tests/web/test_page.py`:

```python
def test_页面显示模型等待心跳() -> None:
    assert "data.type === 'wait'" in HTML
    assert "模型生成中，已等待" in HTML
    assert "上限" in HTML


def test_等待心跳不占用探活横幅() -> None:
    """红色 #health 是连不上，不是还在生成。"""
    start = HTML.find("<script>")
    end = HTML.rfind("</script>")
    script = HTML[start + len("<script>") : end]
    wait_at = script.find("data.type === 'wait'")
    assert wait_at != -1
    wait_block = script[wait_at : wait_at + 280]
    assert "showHealth" not in wait_block
    assert "health" not in wait_block
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/web/test_runner.py::test_wait事件不改变awaiting tests/web/test_page.py::test_页面显示模型等待心跳 tests/web/test_page.py::test_等待心跳不占用探活横幅 -q -p no:cacheprovider`

Expected: 页面两项 FAIL。absorb 测试在改 runner 前就应 PASS（未知类型本来不改 `awaiting`）——保留当回归。

- [ ] **Step 3: Write minimal implementation**

In `src/spoolkit/web/page.py` 的 `onEvent`，`note` 分支之后、`diff` 之前插入（脚本内禁止写 `<`）：

```javascript
  if (data.type === 'wait') {
    var text = '模型生成中，已等待 ' + data.elapsed + 's';
    if (data.limit) { text += ' / 上限 ' + data.limit + 's'; }
    add(text, 'dim');
    return;
  }
```

`runner._absorb` **不要**为 `WAIT` 加分支。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/web/test_page.py tests/web/test_runner.py tests/web/test_protocol.py -q -p no:cacheprovider`

Expected: PASS。`test_脚本里没有会被浏览器当成标签的小于号` 仍过。

- [ ] **Step 5: Commit**

```bash
git add src/spoolkit/web/page.py tests/web/test_page.py tests/web/test_runner.py
git commit -m "$(cat <<'EOF'
feat(web): 事件流显示模型等待心跳

灰字报已等待秒数，不占用模型连不上的红横幅。
EOF
)"
```

---

### Task 5: 文档与 spec 状态

**Files:**
- Modify: `docs/getting-started.md` 故障表「终端几分钟没有新行」那一行
- Modify: `README.md` 已知限制 llama.cpp 那条
- Modify: `docs/dogfooding.md` 追加一条
- Modify: `docs/superpowers/specs/2026-09-21-model-wait-heartbeat-design.md` 状态改为「已定，已实施」

**Interfaces:**
- Consumes: 已落地行为（满 15 秒报还在等；总超时公式不变）
- Produces: 对外文档与 dogfood 记录一致

- [ ] **Step 1: 改上手与 README**

`docs/getting-started.md` 那一行改为：

| 终端几分钟没有新行 | llama.cpp 非流式，整段生成完才返回 | 满 15 秒会报「模型生成中，已等待…」；仍等到现有上限。假模型缺脚本会立刻退出，不会替你写代码 |

`README.md` 已知限制改为：

- **llama.cpp 等待期会报心跳**：生成仍是非流式的；满 15 秒打一行「还在等」（CLI stderr / 网页事件）。总超时公式不变。假模型缺脚本会立刻退出，不会替你写代码。

- [ ] **Step 2: 追加 dogfooding 与 spec 状态**

`docs/dogfooding.md` 文末：

```markdown
## 2026-09-22 · 模型等待心跳

**任务**：llama.cpp `stream: false` 时三端长时间无输出，人以为挂死。

**做了**：`WaitingGateway` 满 15 秒心跳；散文 stderr、`--events` 发 `wait`；网页灰字。不改超时、不接流式。

**没做**：SSE / 空闲杀请求 / T8。
```

Spec 文首 `状态：待评审` 改为 `状态：已定，已实施`。

- [ ] **Step 3: 跑相关测试**

Run: `uv run pytest tests/llm/test_waiting.py tests/cli/test_waiting_bind.py tests/web/test_protocol.py tests/web/test_page.py tests/web/test_runner.py tests/llm/test_llamacpp.py -q -p no:cacheprovider`

Expected: PASS；`test_请求打到OpenAI兼容路径` 仍断言 `stream is False`。

- [ ] **Step 4: Commit**

```bash
git add docs/getting-started.md README.md docs/dogfooding.md docs/superpowers/specs/2026-09-21-model-wait-heartbeat-design.md
git commit -m "$(cat <<'EOF'
docs: 回写模型等待心跳的对外说明

故障表不再写成「几分钟完全没新行」。
EOF
)"
```

---

## 建议落地顺序

```
Task 1 → 2 → 3 → 4 → 5
```

1 不依赖协议；3 依赖 1 和 2；4 依赖 2；5 最后。

## 验收（计划级）

- `HEARTBEAT_SECONDS == 15`；立刻返回零心跳；堵住后有心跳；inner 抛错后不再跳。
- `parse_line` 认 `wait`；无 `limit` 也认。
- `spool run --events` 子进程 stdout 出现 `wait` JSON 时，网页 runner 的 `awaiting` 仍为 0。
- 页面灰字含「模型生成中，已等待」；`#health` 仍只服务于连不上。
- `tests/llm/test_llamacpp.py` 里 `stream is False` 仍过。
- 全套 pytest 绿（允许已知 service flaky 重跑）。

## Self-review（对照 spec）

| Spec 条款 | 任务 |
|---|---|
| 满 15 秒心跳、可中断 | Task 1 |
| llama.cpp 带 `_timeout_for` 上限 | Task 1 |
| 散文 stderr / `--events` JSON，同一进程一条路 | Task 3 |
| `wait` 进协议白名单 | Task 2 |
| 网页灰字、不占红横幅 | Task 4 |
| QQ 不推手机、`awaiting` 不变 | Task 4（不改 bridge） |
| 不改超时、不接流式、不进 limits | Global Constraints |
| 上手 / README | Task 5 |
| 假模型秒回零心跳 | Task 1 + Task 3 包一层但不触发 |
