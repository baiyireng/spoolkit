# 短上下文主循环骨架 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在无 GPU 的前提下，用确定性假模型跑通完整的 Agent 闭环，并让「上下文分层预算」「任务状态外置」「工具调用协议」三套核心机制通过自动化测试。

**Architecture:** 模型网关是纯粹的可替换接口（真实实现走 llama.cpp HTTP，测试与当前开发用 `FakeModel`）。上下文装配器按配额从各区段拼装并裁剪；工具层用 JSON Schema 子集校验调用；主循环单步编排，把任务状态外置到检查点文件，使对话历史可以被随时丢弃。

**Tech Stack:** Python 3.14（要求 >=3.12）、标准库为主、pytest。运行时依赖仅 `httpx`，但本计划所有任务都不需要它。

## Global Constraints

以下约束来自设计文档 `docs/superpowers/specs/2026-09-11-local-coding-agent-design.md`，对每个任务都生效。

- Python 版本下限 `>=3.12`；虚拟环境为 `.venv`（Python 3.14.3）。
- 运行时依赖只允许 `httpx`；测试依赖只允许 `pytest`。本计划不新增任何依赖。
- 所有文件路径解析后必须落在项目根目录内，越界一律拒绝。
- token 计数必须可插拔：真实实现调用服务端 `/tokenize`，离线实现用估算。
- 有效预算 = 上下文窗口 − 输出预留；输出预留比例 `0.15`，硬性不可占用。
- 结构化输出一律经校验，校验失败返回结构化错误而非抛异常。
- 代码与文档注释使用中文；标识符使用英文。
- 每个任务结束时必须 pytest 全绿并提交一次。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/agents_dev/errors.py` | 项目异常类型 |
| `src/agents_dev/paths.py` | 项目内路径安全解析 |
| `src/agents_dev/config.py` | 运行配置 |
| `src/agents_dev/llm/types.py` | `Message` / `ChatRequest` / `ChatResponse` |
| `src/agents_dev/llm/tokenizer.py` | `TokenCounter` 协议与离线估算实现 |
| `src/agents_dev/llm/gateway.py` | `ModelGateway` 协议 |
| `src/agents_dev/llm/fake.py` | 确定性假模型 |
| `src/agents_dev/context/sections.py` | 上下文区段类型 |
| `src/agents_dev/context/budget.py` | 分层配额与阈值 |
| `src/agents_dev/context/assembler.py` | 上下文装配与裁剪 |
| `src/agents_dev/tools/types.py` | `ToolSpec` / `ToolCall` / `ToolResult` |
| `src/agents_dev/tools/registry.py` | 工具注册表与参数校验 |
| `src/agents_dev/tools/fs.py` | 读文件、列目录、按行区间取片段 |
| `src/agents_dev/tools/search.py` | 基于 `rg` 的代码搜索 |
| `src/agents_dev/agent/state.py` | 任务状态与检查点读写 |
| `src/agents_dev/agent/protocol.py` | 模型输出的结构化解析 |
| `src/agents_dev/agent/loop.py` | 主循环与预算守卫 |
| `src/agents_dev/cli/app.py` | 最小命令行入口 |

测试文件与源码镜像，全部放在 `tests/` 下。

---

## Task 1: 路径安全与异常类型

**Files:**
- Create: `src/agents_dev/errors.py`
- Create: `src/agents_dev/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `agents_dev.errors.AgentError`、`PathOutsideProjectError`、`ToolArgumentError`
  - `agents_dev.paths.resolve_within(root: Path, candidate: str) -> Path`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_paths.py
from pathlib import Path

import pytest

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_within


def test_相对路径解析到项目根之下(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    got = resolve_within(tmp_path, "pkg/a.py")
    assert got == (tmp_path / "pkg" / "a.py").resolve()


def test_绝对路径若在项目内则接受(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    assert resolve_within(tmp_path, str(target)) == target.resolve()


def test_越界路径被拒绝(tmp_path: Path) -> None:
    with pytest.raises(PathOutsideProjectError):
        resolve_within(tmp_path, "../outside.py")


def test_用绝对路径越界同样被拒绝(tmp_path: Path) -> None:
    with pytest.raises(PathOutsideProjectError):
        resolve_within(tmp_path, str(tmp_path.parent / "outside.py"))


def test_空路径被拒绝(tmp_path: Path) -> None:
    with pytest.raises(PathOutsideProjectError):
        resolve_within(tmp_path, "")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_paths.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.errors'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/errors.py
"""项目内共用的异常类型。"""


class AgentError(Exception):
    """本项目所有自定义异常的基类。"""


class PathOutsideProjectError(AgentError):
    """请求的路径落在项目根目录之外。"""


class ToolArgumentError(AgentError):
    """工具调用参数不符合其 JSON Schema。"""
```

```python
# src/agents_dev/paths.py
"""项目内路径的安全解析。

所有涉及文件系统的工具都必须经由 resolve_within 取得路径，
以保证模型无法通过相对路径、绝对路径或符号链接逃逸出项目根目录。
"""

from pathlib import Path

from agents_dev.errors import PathOutsideProjectError


def resolve_within(root: Path, candidate: str) -> Path:
    """把 candidate 解析为 root 之下的绝对路径。

    越界、空路径或无法解析的路径一律抛出 PathOutsideProjectError。
    """
    if not candidate or not candidate.strip():
        raise PathOutsideProjectError("路径为空")

    base = root.resolve()
    raw = Path(candidate)
    target = raw if raw.is_absolute() else base / raw
    resolved = target.resolve()

    if resolved != base and base not in resolved.parents:
        raise PathOutsideProjectError(f"路径越出项目根目录: {candidate}")
    return resolved
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_paths.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/errors.py src/agents_dev/paths.py tests/test_paths.py
git commit -m "feat: 项目内路径安全解析与异常类型"
```

---

## Task 2: token 计数

**Files:**
- Create: `src/agents_dev/llm/tokenizer.py`
- Test: `tests/llm/test_tokenizer.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `agents_dev.llm.tokenizer.TokenCounter`（协议，方法 `count(text: str) -> int`）
  - `agents_dev.llm.tokenizer.OfflineTokenCounter`

- [ ] **Step 1: 写失败测试**

```python
# tests/llm/test_tokenizer.py
from agents_dev.llm.tokenizer import OfflineTokenCounter


def test_空字符串为零() -> None:
    assert OfflineTokenCounter().count("") == 0


def test_计数随长度单调增长() -> None:
    counter = OfflineTokenCounter()
    assert counter.count("a" * 100) > counter.count("a" * 10)


def test_中文比等长英文消耗更多() -> None:
    counter = OfflineTokenCounter()
    assert counter.count("中" * 50) > counter.count("z" * 50)


def test_估算不低估量级() -> None:
    counter = OfflineTokenCounter()
    assert counter.count("hello world " * 20) >= 40


def test_同一输入结果稳定() -> None:
    counter = OfflineTokenCounter()
    text = "def f():\n    return 1\n"
    assert counter.count(text) == counter.count(text)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/llm/test_tokenizer.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.llm.tokenizer'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/llm/tokenizer.py
"""token 计数。

真实实现应向 llama.cpp 服务端的 /tokenize 端点查询以获得精确值；
离线实现用于测试与无 GPU 环境，采用保守估算。
两种实现都满足 TokenCounter 协议，上层无需区分。
"""

from typing import Protocol


class TokenCounter(Protocol):
    """token 计数接口。"""

    def count(self, text: str) -> int:
        """返回 text 的 token 数估算或精确值。"""
        ...


class OfflineTokenCounter:
    """不依赖模型的估算实现。

    规则：ASCII 字符按 4 字符 1 token，非 ASCII 按 1 字符 1 token。
    刻意偏保守（宁多不少），以免低估导致上下文溢出。
    """

    def count(self, text: str) -> int:
        if not text:
            return 0
        ascii_chars = sum(1 for ch in text if ord(ch) < 128)
        wide_chars = len(text) - ascii_chars
        return max(1, -(-ascii_chars // 4) + wide_chars)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/llm/test_tokenizer.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/llm/tokenizer.py tests/llm/test_tokenizer.py
git commit -m "feat: 可插拔的 token 计数接口与离线估算实现"
```

---

## Task 3: 消息类型、网关协议与假模型

**Files:**
- Create: `src/agents_dev/llm/types.py`
- Create: `src/agents_dev/llm/gateway.py`
- Create: `src/agents_dev/llm/fake.py`
- Test: `tests/llm/test_fake.py`

**Interfaces:**
- Consumes: `agents_dev.llm.tokenizer.TokenCounter`
- Produces:
  - `agents_dev.llm.types.Message(role: str, content: str, name: str | None = None)`
  - `agents_dev.llm.types.ChatRequest(messages: tuple[Message, ...], max_tokens: int, grammar: str | None = None)`
  - `agents_dev.llm.types.ChatResponse(text: str, prompt_tokens: int, completion_tokens: int)`
  - `agents_dev.llm.gateway.ModelGateway`（协议，方法 `chat(request: ChatRequest) -> ChatResponse`）
  - `agents_dev.llm.fake.FakeModel(script: Sequence[str], tokenizer: TokenCounter)`，含 `.chat()`、`.requests`、`.remaining`

- [ ] **Step 1: 写失败测试**

```python
# tests/llm/test_fake.py
import pytest

from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.llm.types import ChatRequest, Message


def _req(text: str) -> ChatRequest:
    return ChatRequest(
        messages=(Message(role="user", content=text),),
        max_tokens=256,
    )


def test_按脚本顺序返回响应() -> None:
    model = FakeModel(script=["第一步", "第二步"], tokenizer=OfflineTokenCounter())
    assert model.chat(_req("a")).text == "第一步"
    assert model.chat(_req("b")).text == "第二步"


def test_脚本耗尽后抛错() -> None:
    model = FakeModel(script=["只有一条"], tokenizer=OfflineTokenCounter())
    model.chat(_req("a"))
    with pytest.raises(RuntimeError):
        model.chat(_req("b"))


def test_记录收到的请求便于断言() -> None:
    model = FakeModel(script=["ok"], tokenizer=OfflineTokenCounter())
    model.chat(_req("请读文件"))
    assert len(model.requests) == 1
    assert model.requests[0].messages[0].content == "请读文件"


def test_统计token数() -> None:
    model = FakeModel(script=["返回内容"], tokenizer=OfflineTokenCounter())
    resp = model.chat(_req("提示"))
    assert resp.prompt_tokens > 0
    assert resp.completion_tokens > 0


def test_剩余条数可查() -> None:
    model = FakeModel(script=["a", "b"], tokenizer=OfflineTokenCounter())
    assert model.remaining == 2
    model.chat(_req("x"))
    assert model.remaining == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/llm/test_fake.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.llm.fake'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/llm/types.py
"""模型交互的基础数据类型。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    """一条对话消息。role 取 system / user / assistant / tool。"""

    role: str
    content: str
    name: str | None = None


@dataclass(frozen=True)
class ChatRequest:
    """一次模型请求。grammar 非空时表示要求语法约束解码。"""

    messages: tuple[Message, ...]
    max_tokens: int
    grammar: str | None = None


@dataclass(frozen=True)
class ChatResponse:
    """一次模型响应，携带用量统计用于预算核算。"""

    text: str
    prompt_tokens: int
    completion_tokens: int
```

```python
# src/agents_dev/llm/gateway.py
"""模型网关协议。"""

from typing import Protocol

from agents_dev.llm.types import ChatRequest, ChatResponse


class ModelGateway(Protocol):
    """所有模型供应商必须满足的接口。

    真实实现走 llama.cpp HTTP；测试与离线开发使用 FakeModel。
    上层代码只依赖本协议，不依赖具体实现。
    """

    def chat(self, request: ChatRequest) -> ChatResponse:
        """发送请求并返回完整响应。"""
        ...
```

```python
# src/agents_dev/llm/fake.py
"""确定性假模型。

用途有二：一是在无 GPU 时驱动开发与测试；二是在自动化测试中精确
控制模型输出，从而验证循环、预算、协议等机制本身是否正确。
它不模拟任何智能，只按预设脚本顺序应答。
"""

from typing import Sequence

from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.llm.types import ChatRequest, ChatResponse


class FakeModel:
    """按脚本应答的假模型。"""

    def __init__(self, script: Sequence[str], tokenizer: TokenCounter) -> None:
        self._script = list(script)
        self._tokenizer = tokenizer
        self.requests: list[ChatRequest] = []
        self._cursor = 0

    @property
    def remaining(self) -> int:
        """脚本中尚未被消费的条数。"""
        return len(self._script) - self._cursor

    def chat(self, request: ChatRequest) -> ChatResponse:
        if self._cursor >= len(self._script):
            raise RuntimeError("假模型脚本已耗尽，请补充足够的应答条目")

        self.requests.append(request)
        text = self._script[self._cursor]
        self._cursor += 1

        prompt_tokens = sum(self._tokenizer.count(m.content) for m in request.messages)
        return ChatResponse(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=self._tokenizer.count(text),
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/llm/test_fake.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/llm/types.py src/agents_dev/llm/gateway.py src/agents_dev/llm/fake.py tests/llm/test_fake.py
git commit -m "feat: 模型网关协议、消息类型与确定性假模型"
```

---

## Task 4: 上下文区段与分层配额

**Files:**
- Create: `src/agents_dev/context/sections.py`
- Create: `src/agents_dev/context/budget.py`
- Test: `tests/context/test_budget.py`

**Interfaces:**
- Consumes: `agents_dev.llm.tokenizer.TokenCounter`
- Produces:
  - `agents_dev.context.sections.Section(name: str, text: str, priority: int, mandatory: bool = False)`
  - `agents_dev.context.budget.OUTPUT_RESERVE_RATIO = 0.15`
  - `agents_dev.context.budget.FLEX_QUOTAS: dict[str, float]`
  - `agents_dev.context.budget.FIXED_QUOTAS: dict[str, int]`
  - `agents_dev.context.budget.Budget(window: int)`，含 `.effective()`、`.quota(name)`、`.output_reserve()`、`.soft_limit()`、`.hard_limit()`

配额分两类：固定配额（与窗口无关，如系统提示）与比例配额（按有效预算折算）。

- [ ] **Step 1: 写失败测试**

```python
# tests/context/test_budget.py
from agents_dev.context.budget import OUTPUT_RESERVE_RATIO, Budget
from agents_dev.context.sections import Section


def test_输出预留被扣除() -> None:
    b = Budget(window=1000)
    assert b.output_reserve() == 150
    assert b.effective() == 850


def test_比例配额按有效预算计算() -> None:
    b = Budget(window=1000)
    assert b.quota("hot_memory") == int(850 * 0.15)
    assert b.quota("code") == int(850 * 0.35)


def test_系统区段使用固定配额() -> None:
    assert Budget(window=1000).quota("system") == 900
    assert Budget(window=32000).quota("system") == 900


def test_未知区段配额为零() -> None:
    assert Budget(window=1000).quota("不存在") == 0


def test_软硬两条触发线() -> None:
    b = Budget(window=1000)
    assert b.soft_limit() == int(1000 * 0.70)
    assert b.hard_limit() == int(1000 * 0.90)


def test_区段默认可裁剪() -> None:
    assert Section(name="code", text="x", priority=50).mandatory is False


def test_输出预留比例符合设计() -> None:
    assert OUTPUT_RESERVE_RATIO == 0.15
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/context/test_budget.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.context.budget'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/context/sections.py
"""上下文区段。

装配器把上下文切成若干区段分别核算配额。priority 越小越先被保留；
mandatory 为 True 的区段永不裁剪，超限时直接报错（说明配置本身不合理）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    """上下文中的一个区段。"""

    name: str
    text: str
    priority: int
    mandatory: bool = False
```

```python
# src/agents_dev/context/budget.py
"""上下文预算与分层配额。

术语：有效预算 = 上下文窗口 − 输出预留。
输出预留比例硬性为 0.15，不可被任何区段占用——上下文接近满载时
模型质量显著下降，且必须留有空间生成响应。
"""

OUTPUT_RESERVE_RATIO = 0.15

# 固定配额：与窗口大小无关。系统提示与工具描述长度稳定，按绝对量给定。
FIXED_QUOTAS: dict[str, int] = {
    "system": 900,
}

# 比例配额：占有效预算的百分比。未列出的区段配额为 0。
# recent_turns 不设配额，它取所有配额之外的余量。
FLEX_QUOTAS: dict[str, float] = {
    "hot_memory": 0.15,
    "task_state": 0.05,
    "retrieval": 0.15,
    "code": 0.35,
}

SOFT_TRIGGER_RATIO = 0.70
HARD_TRIGGER_RATIO = 0.90


class Budget:
    """一次请求的上下文预算。"""

    def __init__(self, window: int) -> None:
        if window <= 0:
            raise ValueError("上下文窗口必须为正数")
        self.window = window

    def output_reserve(self) -> int:
        """为模型输出保留的 token 数。"""
        return int(self.window * OUTPUT_RESERVE_RATIO)

    def effective(self) -> int:
        """可供上下文装配使用的有效预算。"""
        return self.window - self.output_reserve()

    def quota(self, name: str) -> int:
        """区段 name 的配额。固定配额优先，其次比例配额，未定义则为 0。"""
        if name in FIXED_QUOTAS:
            return FIXED_QUOTAS[name]
        ratio = FLEX_QUOTAS.get(name)
        if ratio is None:
            return 0
        return int(self.effective() * ratio)

    def soft_limit(self) -> int:
        """软触发线：达到后整理上下文，不中断任务。"""
        return int(self.window * SOFT_TRIGGER_RATIO)

    def hard_limit(self) -> int:
        """硬触发线：达到后重置上下文，保留状态。"""
        return int(self.window * HARD_TRIGGER_RATIO)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/context/test_budget.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/context/sections.py src/agents_dev/context/budget.py tests/context/test_budget.py
git commit -m "feat: 上下文区段与分层配额"
```

---

## Task 5: 上下文装配器

**Files:**
- Create: `src/agents_dev/context/assembler.py`
- Test: `tests/context/test_assembler.py`

**Interfaces:**
- Consumes: `TokenCounter`、`Budget`、`Section`、`Message`
- Produces:
  - `agents_dev.context.assembler.Assembler(tokenizer, budget)`
    - `.assemble(sections: Sequence[Section], recent_turns: Sequence[Message] = ()) -> AssembleResult`
  - `agents_dev.context.assembler.AssembleResult(messages, total_tokens, demand_tokens, dropped, window)`
    - `.ratio` 属性返回 `total_tokens / window`

**关键概念：`total_tokens` 是实际装进去的量，`demand_tokens` 是未裁剪前想要的量。**

装配器最多只能装到有效预算（窗口的 85%），而硬触发线是窗口的 90%，因此**用 `total_tokens` 判断触发线永远不会触发**。触发线必须依据 `demand_tokens`——它反映的是任务真实的上下文压力。

- [ ] **Step 1: 写失败测试**

```python
# tests/context/test_assembler.py
import pytest

from agents_dev.context.assembler import Assembler
from agents_dev.context.budget import Budget
from agents_dev.context.sections import Section
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.llm.types import Message


def _asm(window: int = 1000) -> Assembler:
    return Assembler(tokenizer=OfflineTokenCounter(), budget=Budget(window=window))


def test_按优先级顺序拼接区段() -> None:
    result = _asm().assemble(
        [
            Section(name="task_state", text="状态", priority=20),
            Section(name="system", text="系统提示", priority=10),
        ]
    )
    assert result.messages[0].content == "系统提示"
    assert result.messages[1].content == "状态"


def test_普通区段超配额被裁剪并记录() -> None:
    result = _asm(window=1000).assemble(
        [Section(name="code", text="x" * 4000, priority=50)]
    )
    assert "code" in result.dropped
    assert result.total_tokens <= 1000


def test_需求体积反映裁剪前的量() -> None:
    result = _asm(window=1000).assemble(
        [Section(name="code", text="x" * 4000, priority=50)]
    )
    assert result.demand_tokens > result.total_tokens


def test_需求体积计入全部历史轮次() -> None:
    turns = [Message(role="user", content="z" * 4000) for _ in range(10)]
    result = _asm(window=1000).assemble([], recent_turns=turns)
    assert result.demand_tokens == 10000


def test_必留区段超限时报错() -> None:
    with pytest.raises(ValueError):
        _asm(window=100).assemble(
            [Section(name="system", text="y" * 5000, priority=1, mandatory=True)]
        )


def test_最近轮次从旧到新排列() -> None:
    turns = [Message(role="user", content=f"第{i}轮") for i in range(1, 4)]
    result = _asm(window=1000).assemble(
        [Section(name="system", text="S", priority=1)], recent_turns=turns
    )
    contents = [m.content for m in result.messages if m.role == "user"]
    assert contents == ["第1轮", "第2轮", "第3轮"]


def test_余量不足时丢弃更旧的轮次() -> None:
    turns = [Message(role="user", content="z" * 400) for _ in range(10)]
    result = _asm(window=1000).assemble([], recent_turns=turns)
    assert 0 < len(result.messages) < 10
    assert result.messages[-1].content == "z" * 400


def test_装配结果报告占用比例() -> None:
    result = _asm(window=1000).assemble(
        [Section(name="system", text="abc", priority=1)]
    )
    assert 0 < result.ratio < 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/context/test_assembler.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.context.assembler'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/context/assembler.py
"""上下文装配器。

职责：把若干区段按优先级与配额拼装成最终消息序列，超限时从最低
优先级开始裁剪。裁剪只发生在区段内部，不改变区段之间的顺序。

同时报告 demand_tokens（未裁剪前的需求总量）与 total_tokens（实际装入量），
前者用于判断上下文压力，后者用于核算真实占用。
"""

from dataclasses import dataclass
from typing import Sequence

from agents_dev.context.budget import Budget
from agents_dev.context.sections import Section
from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.llm.types import Message

TRUNCATION_MARKER = "\n…（内容已裁剪）"


@dataclass(frozen=True)
class AssembleResult:
    """装配结果。

    demand_tokens: 裁剪前的需求总量，反映上下文压力
    total_tokens:  实际装入的 token 数
    dropped:       被裁剪过的区段名
    """

    messages: tuple[Message, ...]
    total_tokens: int
    demand_tokens: int
    dropped: tuple[str, ...]
    window: int

    @property
    def ratio(self) -> float:
        """实际占用占窗口的比例。"""
        return self.total_tokens / self.window

    @property
    def pressure(self) -> float:
        """需求占窗口的比例，用于 70%/90% 触发线判断。"""
        return self.demand_tokens / self.window


class Assembler:
    """按配额装配上下文。"""

    def __init__(self, tokenizer: TokenCounter, budget: Budget) -> None:
        self._tokenizer = tokenizer
        self._budget = budget

    def _fit(self, text: str, limit: int) -> str:
        """把 text 裁剪到 limit 个 token 以内。"""
        if self._tokenizer.count(text) <= limit:
            return text
        marker_cost = self._tokenizer.count(TRUNCATION_MARKER)
        room = max(0, limit - marker_cost)
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._tokenizer.count(text[:mid]) <= room:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + TRUNCATION_MARKER

    def assemble(
        self,
        sections: Sequence[Section],
        recent_turns: Sequence[Message] = (),
    ) -> AssembleResult:
        ordered = sorted(sections, key=lambda s: s.priority)
        demand = sum(self._tokenizer.count(s.text) for s in ordered)
        demand += sum(self._tokenizer.count(m.content) for m in recent_turns)

        dropped: list[str] = []
        rendered: list[str] = []

        for section in ordered:
            if section.mandatory:
                if self._tokenizer.count(section.text) > self._budget.effective():
                    raise ValueError(
                        f"必留区段 {section.name} 超出有效预算，请检查配置"
                    )
                rendered.append(section.text)
                continue

            fitted = self._fit(section.text, self._budget.quota(section.name))
            if fitted != section.text:
                dropped.append(section.name)
            rendered.append(fitted)

        used = sum(self._tokenizer.count(r) for r in rendered)
        messages = [Message(role="system", content=r) for r in rendered]

        # 最近轮次取余量。从最新往回填决定保留哪些，再还原为时间顺序。
        kept: list[Message] = []
        for message in reversed(recent_turns):
            cost = self._tokenizer.count(message.content)
            if used + cost > self._budget.effective():
                break
            used += cost
            kept.append(message)
        kept.reverse()

        return AssembleResult(
            messages=tuple(messages + kept),
            total_tokens=used,
            demand_tokens=demand,
            dropped=tuple(dropped),
            window=self._budget.window,
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/context/test_assembler.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/context/assembler.py tests/context/test_assembler.py
git commit -m "feat: 上下文装配器与分级裁剪"
```

---

## Task 6: 工具类型与注册表

**Files:**
- Create: `src/agents_dev/tools/types.py`
- Create: `src/agents_dev/tools/registry.py`
- Test: `tests/tools/test_registry.py`

**Interfaces:**
- Consumes: 无（仅标准库）
- Produces:
  - `agents_dev.tools.types.ToolSpec(name, description, parameters, handler)`
  - `agents_dev.tools.types.ToolCall(name, arguments)`
  - `agents_dev.tools.types.ToolResult(ok, content)`
  - `agents_dev.tools.registry.ToolRegistry()`，含 `.register()`、`.get()`、`.names()`、`.invoke()`、`.describe()`

- [ ] **Step 1: 写失败测试**

```python
# tests/tools/test_registry.py
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.types import ToolCall, ToolResult, ToolSpec

SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="read_file",
            description="读取文件",
            parameters=SCHEMA,
            handler=lambda args: ToolResult(ok=True, content=f"读到 {args['path']}"),
        )
    )
    return reg


def test_注册后可按名查询() -> None:
    reg = _registry()
    assert reg.get("read_file") is not None
    assert reg.names() == ("read_file",)


def test_正常调用返回成功() -> None:
    result = _registry().invoke(ToolCall(name="read_file", arguments={"path": "a.py"}))
    assert result.ok is True
    assert result.content == "读到 a.py"


def test_未知工具返回失败而非抛错() -> None:
    result = _registry().invoke(ToolCall(name="nope", arguments={}))
    assert result.ok is False
    assert "nope" in result.content


def test_缺少必填参数被拦截() -> None:
    result = _registry().invoke(ToolCall(name="read_file", arguments={}))
    assert result.ok is False
    assert "path" in result.content


def test_参数类型错误被拦截() -> None:
    result = _registry().invoke(ToolCall(name="read_file", arguments={"path": 123}))
    assert result.ok is False


def test_未知参数被拒绝() -> None:
    result = _registry().invoke(
        ToolCall(name="read_file", arguments={"path": "a.py", "x": 1})
    )
    assert result.ok is False


def test_处理器抛异常被转为失败结果() -> None:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="boom",
            description="会炸",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: (_ for _ in ()).throw(RuntimeError("炸了")),
        )
    )
    result = reg.invoke(ToolCall(name="boom", arguments={}))
    assert result.ok is False
    assert "炸了" in result.content


def test_描述文本包含工具名与参数名() -> None:
    text = _registry().describe()
    assert "read_file" in text
    assert "path" in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/tools/test_registry.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.tools.registry'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/tools/types.py
"""工具层数据类型。"""

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。失败也是正常返回值，不通过异常表达。"""

    ok: bool
    content: str


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整定义。parameters 为 JSON Schema 子集。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult] = field(repr=False)


@dataclass(frozen=True)
class ToolCall:
    """模型发出的一次工具调用请求。"""

    name: str
    arguments: dict[str, Any]
```

```python
# src/agents_dev/tools/registry.py
"""工具注册表与参数校验。

所有工具调用都必须经 invoke 进入，参数校验失败会被拦在这里，
避免模型编造的参数直接进入真实文件系统或命令行。
校验覆盖本项目实际使用的 JSON Schema 子集：type / required /
properties / additionalProperties / enum。不使用完整实现，以免引入依赖。
"""

from typing import Any

from agents_dev.tools.types import ToolCall, ToolResult, ToolSpec

_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _validate(args: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """校验通过返回 None，否则返回中文错误说明。"""
    if not isinstance(args, dict):
        return "参数必须是 JSON 对象"

    properties = schema.get("properties", {})

    if schema.get("additionalProperties", True) is False:
        unknown = set(args) - set(properties)
        if unknown:
            return f"存在未知参数: {', '.join(sorted(unknown))}"

    for key in schema.get("required", []):
        if key not in args:
            return f"缺少必填参数: {key}"

    for key, value in args.items():
        rule = properties.get(key)
        if rule is None:
            continue
        expected = _TYPE_MAP.get(rule.get("type", ""))
        if expected is not None and not isinstance(value, expected):
            return f"参数 {key} 类型应为 {rule['type']}"
        if "enum" in rule and value not in rule["enum"]:
            return f"参数 {key} 取值不在允许范围内"

    return None


class ToolRegistry:
    """工具注册表。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具，名称重复会直接报错（属配置错误）。"""
        if spec.name in self._specs:
            raise ValueError(f"工具名重复: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行工具调用。任何失败都转成 ok=False 的结果。"""
        spec = self._specs.get(call.name)
        if spec is None:
            return ToolResult(ok=False, content=f"未知工具: {call.name}")

        error = _validate(call.arguments, spec.parameters)
        if error is not None:
            return ToolResult(ok=False, content=f"参数校验失败: {error}")

        try:
            return spec.handler(dict(call.arguments))
        except Exception as exc:  # 工具内部异常不应中断主循环
            return ToolResult(ok=False, content=f"工具执行异常: {exc}")

    def describe(self) -> str:
        """生成紧凑的工具说明，供提示词使用。"""
        lines: list[str] = []
        for name in self.names():
            spec = self._specs[name]
            params = ", ".join(spec.parameters.get("properties", {}))
            lines.append(f"- {name}({params}): {spec.description}")
        return "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/tools/test_registry.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/tools/types.py src/agents_dev/tools/registry.py tests/tools/test_registry.py
git commit -m "feat: 工具注册表与参数校验"
```

---

## Task 7: 文件与搜索工具

**Files:**
- Create: `src/agents_dev/tools/fs.py`
- Create: `src/agents_dev/tools/search.py`
- Test: `tests/tools/test_fs.py`
- Test: `tests/tools/test_search.py`

**Interfaces:**
- Consumes: `resolve_within`、`ToolSpec`、`ToolResult`
- Produces:
  - `agents_dev.tools.fs.read_file_spec(root: Path) -> ToolSpec`（参数 `path`，可选 `start_line`、`end_line`）
  - `agents_dev.tools.fs.list_dir_spec(root: Path) -> ToolSpec`（参数 `path`）
  - `agents_dev.tools.search.search_code_spec(root: Path) -> ToolSpec`（参数 `pattern`，可选 `path`、`max_results`）

- [ ] **Step 1: 写失败测试**

```python
# tests/tools/test_fs.py
from pathlib import Path

from agents_dev.tools.fs import list_dir_spec, read_file_spec


def test_读取整个文件(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    result = read_file_spec(tmp_path).handler({"path": "a.txt"})
    assert result.ok is True
    assert "第一行" in result.content
    assert "第二行" in result.content


def test_按行区间读取(tmp_path: Path) -> None:
    body = "".join(f"line{i}\n" for i in range(1, 11))
    (tmp_path / "a.txt").write_text(body, encoding="utf-8")
    result = read_file_spec(tmp_path).handler(
        {"path": "a.txt", "start_line": 3, "end_line": 4}
    )
    assert result.ok is True
    assert "line3" in result.content
    assert "line4" in result.content
    assert "line5" not in result.content


def test_读取不存在的文件返回失败(tmp_path: Path) -> None:
    result = read_file_spec(tmp_path).handler({"path": "nope.txt"})
    assert result.ok is False
    assert "不存在" in result.content


def test_读取越界路径失败(tmp_path: Path) -> None:
    assert read_file_spec(tmp_path).handler({"path": "../secret.txt"}).ok is False


def test_读取目录返回失败(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    assert read_file_spec(tmp_path).handler({"path": "d"}).ok is False


def test_行号区间非法时失败(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    result = read_file_spec(tmp_path).handler(
        {"path": "a.txt", "start_line": 5, "end_line": 2}
    )
    assert result.ok is False


def test_列目录返回文件名(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    result = list_dir_spec(tmp_path).handler({"path": "pkg"})
    assert result.ok is True
    assert "m.py" in result.content


def test_列目录遇到非目录返回失败(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    assert list_dir_spec(tmp_path).handler({"path": "a.txt"}).ok is False
```

```python
# tests/tools/test_search.py
from pathlib import Path

from agents_dev.tools.search import search_code_spec


def test_搜索命中并返回文件名(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "def target"})
    assert result.ok is True
    assert "a.py" in result.content


def test_无命中时返回空结果而非失败(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "绝不存在的标识符xyzzy"})
    assert result.ok is True
    assert result.content.strip() == ""


def test_搜索不逃出项目根(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_probe.py"
    outside.write_text("SECRET_MARKER = 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "SECRET_MARKER"})
    assert "SECRET_MARKER" not in result.content
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/tools/test_fs.py tests/tools/test_search.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.tools.fs'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/tools/fs.py
"""文件系统工具。

所有路径都经 resolve_within 处理，模型无法逃出项目根目录。
工具失败一律返回 ok=False，不抛异常。
"""

from pathlib import Path

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_within
from agents_dev.tools.types import ToolResult, ToolSpec


def _read_file(root: Path, args: dict) -> ToolResult:
    try:
        target = resolve_within(root, args["path"])
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    if not target.exists():
        return ToolResult(ok=False, content=f"文件不存在: {args['path']}")
    if target.is_dir():
        return ToolResult(ok=False, content=f"目标是目录而非文件: {args['path']}")

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult(ok=False, content=f"文件不是 UTF-8 文本: {args['path']}")

    start = args.get("start_line")
    end = args.get("end_line")
    if start is None and end is None:
        return ToolResult(ok=True, content=text)

    start = 1 if start is None else start
    if start < 1:
        return ToolResult(ok=False, content="start_line 必须大于等于 1")

    lines = text.splitlines()
    end = len(lines) if end is None else end
    if end < start:
        return ToolResult(ok=False, content="end_line 不能小于 start_line")

    chunk = lines[start - 1 : end]
    numbered = "\n".join(f"{start + i}\t{line}" for i, line in enumerate(chunk))
    return ToolResult(ok=True, content=numbered)


def _list_dir(root: Path, args: dict) -> ToolResult:
    try:
        target = resolve_within(root, args["path"])
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    if not target.exists():
        return ToolResult(ok=False, content=f"目录不存在: {args['path']}")
    if not target.is_dir():
        return ToolResult(ok=False, content=f"目标不是目录: {args['path']}")

    entries = [f"{c.name}/" if c.is_dir() else c.name for c in sorted(target.iterdir())]
    return ToolResult(ok=True, content="\n".join(entries))


def read_file_spec(root: Path) -> ToolSpec:
    """构造读文件工具的规格。"""
    return ToolSpec(
        name="read_file",
        description="读取项目内文件；可用 start_line/end_line 只取需要的行区间",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args: _read_file(root, args),
    )


def list_dir_spec(root: Path) -> ToolSpec:
    """构造列目录工具的规格。"""
    return ToolSpec(
        name="list_dir",
        description="列出项目内某个目录的直接子项",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args: _list_dir(root, args),
    )
```

```python
# src/agents_dev/tools/search.py
"""代码搜索工具，基于 ripgrep。

未安装 rg 时返回失败结果并给出明确提示，不静默降级。
"""

import shutil
import subprocess
from pathlib import Path

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_within
from agents_dev.tools.types import ToolResult, ToolSpec

DEFAULT_MAX_RESULTS = 50
TIMEOUT_SECONDS = 20


def _search_code(root: Path, args: dict) -> ToolResult:
    pattern = args["pattern"]
    max_results = args.get("max_results", DEFAULT_MAX_RESULTS)
    if max_results < 1:
        return ToolResult(ok=False, content="max_results 必须大于等于 1")

    if shutil.which("rg") is None:
        return ToolResult(ok=False, content="未找到 rg（ripgrep），无法执行搜索")

    try:
        base = resolve_within(root, args.get("path", "."))
    except PathOutsideProjectError as exc:
        return ToolResult(ok=False, content=str(exc))

    proc = subprocess.run(
        [
            "rg",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            str(max_results),
            "--",
            pattern,
            str(base),
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=str(root),
    )

    if proc.returncode not in (0, 1):
        return ToolResult(ok=False, content=f"搜索失败: {proc.stderr.strip()}")

    root_prefix = str(root)
    lines = proc.stdout.splitlines()[:max_results]
    cleaned = [
        line.replace(root_prefix + "\\", "").replace(root_prefix + "/", "")
        for line in lines
    ]
    return ToolResult(ok=True, content="\n".join(cleaned))


def search_code_spec(root: Path) -> ToolSpec:
    """构造代码搜索工具的规格。"""
    return ToolSpec(
        name="search_code",
        description="在项目内按正则搜索代码，返回 文件:行号:内容",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        handler=lambda args: _search_code(root, args),
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/tools/test_fs.py tests/tools/test_search.py -v`
Expected: PASS（11 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/tools/fs.py src/agents_dev/tools/search.py tests/tools/test_fs.py tests/tools/test_search.py
git commit -m "feat: 文件读取、目录列举与代码搜索工具"
```

---

## Task 8: 任务状态与检查点

**Files:**
- Create: `src/agents_dev/agent/state.py`
- Test: `tests/agent/test_state.py`

**Interfaces:**
- Consumes: `TokenCounter`
- Produces:
  - `agents_dev.agent.state.StateDelta(done_added, current, verify, excluded_added, hypothesis)`
  - `agents_dev.agent.state.TaskState(task_id, goal, done, current, verify, excluded, hypothesis, step)`
    - `.render() -> str`、`.token_cost(counter) -> int`、`.apply(delta)`、`.step_forward()`
  - `agents_dev.agent.state.save_state(state, path)`、`load_state(path) -> TaskState | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/agent/test_state.py
from pathlib import Path

from agents_dev.agent.state import StateDelta, TaskState, load_state, save_state
from agents_dev.llm.tokenizer import OfflineTokenCounter


def _state() -> TaskState:
    return TaskState(
        task_id="t1",
        goal="给 parser 加增量更新",
        done=["读 parser.py"],
        current="正在改 _parse_file",
        verify="跑 pytest tests/test_parser.py",
        excluded=["整文件重解析"],
        hypothesis="比较 mtime",
        step=1,
    )


def test_渲染包含全部字段() -> None:
    text = _state().render()
    for fragment in (
        "给 parser 加增量更新",
        "读 parser.py",
        "_parse_file",
        "pytest",
        "整文件重解析",
        "mtime",
    ):
        assert fragment in text


def test_渲染控制在很小体积() -> None:
    assert _state().token_cost(OfflineTokenCounter()) < 120


def test_应用增量更新字段() -> None:
    state = _state()
    state.apply(StateDelta(done_added=["确认索引表结构"], current="写测试"))
    assert "确认索引表结构" in state.done
    assert state.current == "写测试"


def test_未提供的字段保持不变() -> None:
    state = _state()
    state.apply(StateDelta(hypothesis="改用哈希比对"))
    assert state.current == "正在改 _parse_file"
    assert state.hypothesis == "改用哈希比对"


def test_保存后可原样读回(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(_state(), path)
    restored = load_state(path)
    assert restored is not None
    assert restored.render() == _state().render()


def test_读取不存在的检查点返回空(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nope.json") is None


def test_步数可递增() -> None:
    state = _state()
    state.step_forward()
    assert state.step == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/agent/test_state.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.agent.state'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/agent/state.py
"""任务状态与检查点。

任务状态是「我做到哪了」的结构化表达，每轮注入上下文，
从而让对话历史变成可以随时丢弃的东西。这是短上下文设计的支点。
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agents_dev.llm.tokenizer import TokenCounter


@dataclass
class StateDelta:
    """模型对任务状态提出的增量修改。未提供的字段保持不变。"""

    done_added: list[str] = field(default_factory=list)
    current: str | None = None
    verify: str | None = None
    excluded_added: list[str] = field(default_factory=list)
    hypothesis: str | None = None


@dataclass
class TaskState:
    """一次任务的状态。"""

    task_id: str
    goal: str
    done: list[str] = field(default_factory=list)
    current: str = ""
    verify: str = ""
    excluded: list[str] = field(default_factory=list)
    hypothesis: str = ""
    step: int = 0

    def render(self) -> str:
        """渲染成紧凑文本供注入。刻意省略空字段以节省 token。"""
        lines = [f"目标: {self.goal}"]
        if self.done:
            lines.append("已完成: " + " | ".join(self.done))
        if self.current:
            lines.append(f"当前: {self.current}")
        if self.verify:
            lines.append(f"待验证: {self.verify}")
        if self.excluded:
            lines.append("已排除: " + " | ".join(self.excluded))
        if self.hypothesis:
            lines.append(f"下一步假设: {self.hypothesis}")
        return "\n".join(lines)

    def token_cost(self, counter: TokenCounter) -> int:
        return counter.count(self.render())

    def apply(self, delta: StateDelta) -> None:
        """应用增量修改。"""
        self.done.extend(delta.done_added)
        self.excluded.extend(delta.excluded_added)
        if delta.current is not None:
            self.current = delta.current
        if delta.verify is not None:
            self.verify = delta.verify
        if delta.hypothesis is not None:
            self.hypothesis = delta.hypothesis

    def step_forward(self) -> None:
        self.step += 1


def save_state(state: TaskState, path: Path) -> None:
    """把状态写入检查点文件，父目录不存在则创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_state(path: Path) -> TaskState | None:
    """读取检查点，文件不存在则返回 None。"""
    if not path.exists():
        return None
    return TaskState(**json.loads(path.read_text(encoding="utf-8")))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/agent/test_state.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/agent/state.py tests/agent/test_state.py
git commit -m "feat: 任务状态与检查点读写"
```

---

## Task 9: 结构化输出协议

**Files:**
- Create: `src/agents_dev/agent/protocol.py`
- Test: `tests/agent/test_protocol.py`

**Interfaces:**
- Consumes: `ToolCall`、`StateDelta`
- Produces:
  - `agents_dev.agent.protocol.TURN_SCHEMA: dict`
  - `agents_dev.agent.protocol.AgentTurn(thought, tool_calls, state_delta, final)`
  - `agents_dev.agent.protocol.ParseFailure(reason)`
  - `agents_dev.agent.protocol.parse_turn(text: str) -> AgentTurn | ParseFailure`

模型每轮必须输出一个 JSON 对象：`{"thought": str, "tool_calls": [...], "state": {...}|null, "final": str|null}`。解析失败返回 `ParseFailure`，由主循环作为反馈回灌，不抛异常。

- [ ] **Step 1: 写失败测试**

```python
# tests/agent/test_protocol.py
import json

from agents_dev.agent.protocol import AgentTurn, ParseFailure, parse_turn


def _payload(**overrides) -> str:
    base = {
        "thought": "先读文件",
        "tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}],
        "state": {"current": "读 a.py"},
        "final": None,
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_解析正常输出() -> None:
    turn = parse_turn(_payload())
    assert isinstance(turn, AgentTurn)
    assert turn.thought == "先读文件"
    assert turn.tool_calls[0].name == "read_file"
    assert turn.state_delta is not None
    assert turn.state_delta.current == "读 a.py"
    assert turn.final is None


def test_解析最终答复() -> None:
    turn = parse_turn(_payload(tool_calls=[], final="完成了"))
    assert isinstance(turn, AgentTurn)
    assert turn.final == "完成了"
    assert turn.tool_calls == ()


def test_无状态块时状态增量为空() -> None:
    raw = json.dumps({"thought": "t", "tool_calls": [], "final": "ok"}, ensure_ascii=False)
    turn = parse_turn(raw)
    assert isinstance(turn, AgentTurn)
    assert turn.state_delta is None


def test_非JSON返回解析失败而非抛错() -> None:
    assert isinstance(parse_turn("不是 JSON"), ParseFailure)


def test_缺少thought字段返回解析失败() -> None:
    raw = json.dumps({"tool_calls": [], "final": None}, ensure_ascii=False)
    assert isinstance(parse_turn(raw), ParseFailure)


def test_工具调用缺名字返回解析失败() -> None:
    assert isinstance(parse_turn(_payload(tool_calls=[{"arguments": {}}])), ParseFailure)


def test_工具调用参数不是对象返回解析失败() -> None:
    raw = _payload(tool_calls=[{"name": "read_file", "arguments": "a.py"}])
    assert isinstance(parse_turn(raw), ParseFailure)


def test_既无工具调用也无最终答复返回失败() -> None:
    assert isinstance(parse_turn(_payload(tool_calls=[], final=None)), ParseFailure)


def test_失败信息说明原因() -> None:
    failure = parse_turn("不是 JSON")
    assert isinstance(failure, ParseFailure)
    assert failure.reason


def test_状态块字段被正确传入增量() -> None:
    raw = _payload(state={"done_added": ["第一步"], "excluded_added": ["方案A"]})
    turn = parse_turn(raw)
    assert isinstance(turn, AgentTurn)
    assert turn.state_delta is not None
    assert turn.state_delta.done_added == ["第一步"]
    assert turn.state_delta.excluded_added == ["方案A"]


def test_状态块含未知字段返回失败() -> None:
    assert isinstance(parse_turn(_payload(state={"不存在": 1})), ParseFailure)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/agent/test_protocol.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.agent.protocol'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/agent/protocol.py
"""模型输出的结构化协议。

每轮输出必须是一个 JSON 对象，字段固定。解析失败不抛异常，而是返回
ParseFailure，由主循环作为反馈回灌给模型重试——格式错误在小模型上
是常态而非异常，把它当异常处理会让主循环变得难以推理。

TURN_SCHEMA 同时用于生成 GBNF 语法约束，从采样层面消灭格式错误。
"""

import json
from dataclasses import dataclass
from typing import Any

from agents_dev.agent.state import StateDelta
from agents_dev.tools.types import ToolCall

TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
            },
        },
        "state": {"type": "object"},
        "final": {"type": ["string", "null"]},
    },
    "required": ["thought", "tool_calls"],
}

_STATE_FIELDS = {"done_added", "current", "verify", "excluded_added", "hypothesis"}


@dataclass(frozen=True)
class AgentTurn:
    """一轮输出的结构化表示。"""

    thought: str
    tool_calls: tuple[ToolCall, ...]
    state_delta: StateDelta | None
    final: str | None


@dataclass(frozen=True)
class ParseFailure:
    """解析失败，reason 会作为反馈回灌给模型。"""

    reason: str


def _parse_tool_calls(raw: Any) -> tuple[ToolCall, ...] | str:
    """返回工具调用元组，或返回错误说明字符串。"""
    if not isinstance(raw, list):
        return "tool_calls 必须是数组"

    calls: list[ToolCall] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return f"tool_calls[{index}] 必须是对象"
        name = item.get("name")
        if not isinstance(name, str) or not name:
            return f"tool_calls[{index}] 缺少合法的 name"
        arguments = item.get("arguments", {})
        if not isinstance(arguments, dict):
            return f"tool_calls[{index}].arguments 必须是对象"
        calls.append(ToolCall(name=name, arguments=arguments))
    return tuple(calls)


def _parse_state(raw: Any) -> StateDelta | None | str:
    """返回状态增量、None，或错误说明字符串。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "state 必须是对象"

    unknown = set(raw) - _STATE_FIELDS
    if unknown:
        return f"state 存在未知字段: {', '.join(sorted(unknown))}"

    return StateDelta(
        done_added=list(raw.get("done_added", [])),
        current=raw.get("current"),
        verify=raw.get("verify"),
        excluded_added=list(raw.get("excluded_added", [])),
        hypothesis=raw.get("hypothesis"),
    )


def parse_turn(text: str) -> AgentTurn | ParseFailure:
    """解析模型一轮输出。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return ParseFailure(reason=f"输出不是合法 JSON: {exc.msg}")

    if not isinstance(payload, dict):
        return ParseFailure(reason="输出必须是 JSON 对象")

    thought = payload.get("thought")
    if not isinstance(thought, str):
        return ParseFailure(reason="缺少字符串字段 thought")

    calls = _parse_tool_calls(payload.get("tool_calls", []))
    if isinstance(calls, str):
        return ParseFailure(reason=calls)

    state_delta = _parse_state(payload.get("state"))
    if isinstance(state_delta, str):
        return ParseFailure(reason=state_delta)

    final = payload.get("final")
    if final is not None and not isinstance(final, str):
        return ParseFailure(reason="final 必须是字符串或 null")

    if not calls and final is None:
        return ParseFailure(reason="既没有 tool_calls 也没有 final，本轮没有产出")

    return AgentTurn(
        thought=thought,
        tool_calls=calls,
        state_delta=state_delta,
        final=final,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/agent/test_protocol.py -v`
Expected: PASS（11 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/agent/protocol.py tests/agent/test_protocol.py
git commit -m "feat: 模型输出的结构化协议与解析"
```

---

## Task 10: 主循环与预算守卫

**Files:**
- Create: `src/agents_dev/config.py`
- Create: `src/agents_dev/agent/loop.py`
- Test: `tests/agent/test_loop.py`

**Interfaces:**
- Consumes: `ModelGateway`、`TokenCounter`、`Assembler`、`Budget`、`ToolRegistry`、`TaskState`、`parse_turn`
- Produces:
  - `agents_dev.config.Config(project_root, context_window=8192, max_steps=10, state_dir_name=".agent")`，含 `.state_dir`、`.task_path(task_id)`
  - `agents_dev.agent.loop.AgentLoop(gateway, tokenizer, registry, config)`，含 `.run(goal, task_id="task") -> LoopResult`
  - `agents_dev.agent.loop.LoopResult(finished, final, state, steps, resets, trace)`

每轮流程：装配 → 预算守卫（软触发整理、硬触发重置）→ 请求模型 → 解析 → 执行工具 → 更新状态 → 保存检查点。

- [ ] **Step 1: 写失败测试**

```python
# tests/agent/test_loop.py
import json
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.fs import read_file_spec
from agents_dev.tools.registry import ToolRegistry


def _turn(thought: str, calls=None, state=None, final=None) -> str:
    return json.dumps(
        {"thought": thought, "tool_calls": calls or [], "state": state, "final": final},
        ensure_ascii=False,
    )


def _build(tmp_path: Path, script: list, **config_kwargs) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    settings = {"context_window": 4096, **config_kwargs}
    config = Config(project_root=tmp_path, **settings)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=config,
    )


def test_调用工具后给出最终答复(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("内容ABC\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读文件", [{"name": "read_file", "arguments": {"path": "a.txt"}}]),
            _turn("看到了内容", [], final="文件里有 内容ABC"),
        ],
    )
    result = loop.run("看看 a.txt 里有什么")
    assert result.finished is True
    assert "内容ABC" in result.final
    assert result.steps == 2


def test_工具结果被喂回模型(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("标记内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读文件", [{"name": "read_file", "arguments": {"path": "a.txt"}}]),
            _turn("完成", [], final="好"),
        ],
    )
    loop.run("读文件")
    second = loop.gateway.requests[1]
    assert any("标记内容" in m.content for m in second.messages)


def test_解析失败时把原因回灌并继续(tmp_path: Path) -> None:
    loop = _build(tmp_path, ["这不是 JSON", _turn("改正了", [], final="好了")])
    result = loop.run("做点什么")
    assert result.finished is True
    second = loop.gateway.requests[1]
    assert any("JSON" in m.content for m in second.messages)


def test_状态块被应用并落盘(tmp_path: Path) -> None:
    loop = _build(
        tmp_path,
        [_turn("开始", [], state={"current": "正在分析"}), _turn("结束", [], final="完成")],
    )
    result = loop.run("分析一下")
    assert result.state.current == "正在分析"
    assert (tmp_path / ".agent" / "tasks" / "task.json").exists()


def test_达到步数上限会停止(tmp_path: Path) -> None:
    loop = _build(tmp_path, [_turn(f"第{i}步") for i in range(20)], max_steps=3)
    result = loop.run("没完没了")
    assert result.finished is False
    assert result.steps == 3


def test_上下文需求过大时触发重置(tmp_path: Path) -> None:
    # 每轮都返回体积很大的状态块，历史累积后需求迅速超过硬触发线
    long_state = {"current": "很长的当前状态" * 200}
    script = [_turn(f"步骤{i}", [], state=long_state) for i in range(6)]
    loop = _build(tmp_path, script, context_window=2000)
    result = loop.run("把上下文撑爆")
    assert result.resets >= 1


def test_轨迹记录每一步(tmp_path: Path) -> None:
    result = _build(tmp_path, [_turn("结束", [], final="好")]).run("简单任务")
    assert len(result.trace) >= 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/agent/test_loop.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.agent.loop'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/config.py
"""运行配置。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """一次运行的配置。context_window 为模型上下文窗口总长。"""

    project_root: Path
    context_window: int = 8192
    max_steps: int = 10
    state_dir_name: str = ".agent"

    @property
    def state_dir(self) -> Path:
        return self.project_root / self.state_dir_name

    def task_path(self, task_id: str) -> Path:
        return self.state_dir / "tasks" / f"{task_id}.json"
```

```python
# src/agents_dev/agent/loop.py
"""Agent 主循环。

设计要点：任务状态每轮注入，对话历史只保留最近若干轮，
因此上下文可以被安全地重置——状态不丢，历史可弃。

预算守卫依据 demand_tokens（裁剪前的需求）而非实际装入量来判断：
装配器最多只能装到有效预算，用实际装入量判断触发线永远不会触发。
"""

from dataclasses import dataclass, field

from agents_dev.agent.protocol import ParseFailure, parse_turn
from agents_dev.agent.state import TaskState, save_state
from agents_dev.config import Config
from agents_dev.context.assembler import Assembler
from agents_dev.context.budget import Budget
from agents_dev.context.sections import Section
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.llm.types import ChatRequest, Message
from agents_dev.tools.registry import ToolRegistry

SYSTEM_PROMPT = """你是本地运行的编程助手。每轮只做一件事。
必须输出一个 JSON 对象，字段为 thought、tool_calls、state、final。
不要输出 JSON 以外的任何内容。
可用工具：
{tools}"""

MAX_RECENT_TURNS = 6


@dataclass
class LoopResult:
    """一次运行的结果。"""

    finished: bool
    final: str
    state: TaskState
    steps: int
    resets: int
    trace: list[str] = field(default_factory=list)


class AgentLoop:
    """单步编排的主循环。"""

    def __init__(
        self,
        gateway: ModelGateway,
        tokenizer: TokenCounter,
        registry: ToolRegistry,
        config: Config,
    ) -> None:
        self.gateway = gateway
        self.tokenizer = tokenizer
        self.registry = registry
        self.config = config
        self._budget = Budget(window=config.context_window)

    def _assemble(self, state: TaskState, history: list[Message], feedback: str | None):
        """按当前状态与历史装配本轮上下文。"""
        assembler = Assembler(tokenizer=self.tokenizer, budget=self._budget)
        system_text = SYSTEM_PROMPT.format(tools=self.registry.describe())
        sections = [
            Section(name="system", text=system_text, priority=10, mandatory=True),
            Section(name="task_state", text=state.render(), priority=30),
        ]
        if feedback:
            sections.append(Section(name="retrieval", text=feedback, priority=40))
        return assembler.assemble(sections, recent_turns=history[-MAX_RECENT_TURNS:])

    def run(self, goal: str, task_id: str = "task") -> LoopResult:
        """运行任务直到给出最终答复或达到步数上限。"""
        state = TaskState(task_id=task_id, goal=goal)
        history: list[Message] = [Message(role="user", content=goal)]
        feedback: str | None = None
        resets = 0
        trace: list[str] = []

        while state.step < self.config.max_steps:
            assembled = self._assemble(state, history, feedback)

            # 预算守卫：软触发整理，硬触发重置。依据需求体积而非装入量。
            if assembled.demand_tokens >= self._budget.hard_limit():
                history = [Message(role="user", content=goal)]
                feedback = None
                resets += 1
                trace.append(f"step{state.step}: 上下文重置（第 {resets} 次）")
                assembled = self._assemble(state, history, feedback)
            elif assembled.demand_tokens >= self._budget.soft_limit():
                history = history[-(MAX_RECENT_TURNS // 2):]
                trace.append(f"step{state.step}: 上下文整理")

            response = self.gateway.chat(
                ChatRequest(
                    messages=assembled.messages,
                    max_tokens=self._budget.output_reserve(),
                )
            )
            turn = parse_turn(response.text)

            if isinstance(turn, ParseFailure):
                feedback = f"上一轮输出无法解析：{turn.reason}。请只输出规定的 JSON 对象。"
                history.append(Message(role="assistant", content=response.text))
                history.append(Message(role="user", content=feedback))
                state.step_forward()
                trace.append(f"step{state.step}: 解析失败 - {turn.reason}")
                continue

            if turn.state_delta is not None:
                state.apply(turn.state_delta)
            history.append(Message(role="assistant", content=response.text))

            if turn.tool_calls:
                outputs = []
                for call in turn.tool_calls:
                    result = self.registry.invoke(call)
                    status = "成功" if result.ok else "失败"
                    outputs.append(f"[{call.name}] {status}: {result.content}")
                    trace.append(f"step{state.step}: 工具 {call.name} -> {status}")
                history.append(Message(role="tool", content="\n".join(outputs)))

            state.step_forward()
            save_state(state, self.config.task_path(task_id))

            if turn.final is not None:
                trace.append(f"step{state.step}: 完成")
                return LoopResult(True, turn.final, state, state.step, resets, trace)

        return LoopResult(
            False, "已达步数上限，任务未完成", state, state.step, resets, trace
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/agent/test_loop.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/config.py src/agents_dev/agent/loop.py tests/agent/test_loop.py
git commit -m "feat: Agent 主循环与上下文预算守卫"
```

---

## Task 11: 最小命令行与端到端验证

**Files:**
- Create: `src/agents_dev/cli/app.py`
- Test: `tests/test_end_to_end.py`

**Interfaces:**
- Consumes: `AgentLoop`、`Config`、`ToolRegistry`、`FakeModel`
- Produces:
  - `agents_dev.cli.app.build_loop(project_root: Path, script: list[str], window: int = 4096) -> AgentLoop`
  - `agents_dev.cli.app.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_end_to_end.py
import json
from pathlib import Path

from agents_dev.cli.app import build_loop, main


def test_端到端完成一次读文件并回答(tmp_path: Path) -> None:
    (tmp_path / "config.py").write_text("TIMEOUT = 30\n", encoding="utf-8")
    script = [
        json.dumps(
            {
                "thought": "读取配置",
                "tool_calls": [{"name": "read_file", "arguments": {"path": "config.py"}}],
                "state": {"current": "读 config.py"},
                "final": None,
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "thought": "已获得答案",
                "tool_calls": [],
                "state": {"done_added": ["读 config.py"]},
                "final": "超时配置是 30",
            },
            ensure_ascii=False,
        ),
    ]
    result = build_loop(tmp_path, script=script, window=4096).run("config.py 里的超时是多少")
    assert result.finished is True
    assert "30" in result.final
    assert (tmp_path / ".agent" / "tasks" / "task.json").exists()


def test_命令行读取脚本文件并运行(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("答案42\n", encoding="utf-8")
    script_path = tmp_path / "script.json"
    script_path.write_text(
        json.dumps(
            [
                {
                    "thought": "读文件",
                    "tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}],
                    "state": None,
                    "final": None,
                },
                {"thought": "完成", "tool_calls": [], "state": None, "final": "答案是 42"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "run",
            "--goal",
            "a.txt 里是什么",
            "--script",
            str(script_path),
            "--root",
            str(tmp_path),
            "--window",
            "4096",
        ]
    )
    assert exit_code == 0


def test_脚本文件不存在时返回非零(tmp_path: Path) -> None:
    exit_code = main(
        [
            "run",
            "--goal",
            "x",
            "--script",
            str(tmp_path / "nope.json"),
            "--root",
            str(tmp_path),
        ]
    )
    assert exit_code == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_end_to_end.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'agents_dev.cli.app'`

- [ ] **Step 3: 写最小实现**

```python
# src/agents_dev/cli/app.py
"""最小命令行入口。

当前阶段用脚本化假模型驱动，因此整个闭环在无 GPU 环境下即可运行。
接入 llama.cpp 后，只需把 build_loop 中的 FakeModel 换成真实网关。
"""

import argparse
import json
import sys
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.fs import list_dir_spec, read_file_spec
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.search import search_code_spec


def build_loop(project_root: Path, script: list[str], window: int = 4096) -> AgentLoop:
    """装配一个由假模型驱动的完整循环。"""
    registry = ToolRegistry()
    registry.register(read_file_spec(project_root))
    registry.register(list_dir_spec(project_root))
    registry.register(search_code_spec(project_root))

    tokenizer = OfflineTokenCounter()
    config = Config(project_root=project_root, context_window=window)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=registry,
        config=config,
    )


def _run(args: argparse.Namespace) -> int:
    script_path = Path(args.script)
    if not script_path.exists():
        print(f"脚本文件不存在: {script_path}", file=sys.stderr)
        return 2

    raw = json.loads(script_path.read_text(encoding="utf-8"))
    script = [
        item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for item in raw
    ]

    loop = build_loop(Path(args.root).resolve(), script=script, window=args.window)
    result = loop.run(args.goal)

    for line in result.trace:
        print(line)
    print("---")
    print(result.final)
    return 0 if result.finished else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents-dev")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="运行一次任务")
    run_parser.add_argument("--goal", required=True)
    run_parser.add_argument("--script", required=True, help="假模型脚本 JSON")
    run_parser.add_argument("--root", default=".")
    run_parser.add_argument("--window", type=int, default=4096)
    run_parser.set_defaults(func=_run)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 运行全部测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add src/agents_dev/cli/app.py tests/test_end_to_end.py
git commit -m "feat: 最小命令行入口与端到端验证"
```

---

## 完成标准

全部任务完成后应满足：

1. `pytest` 全绿，覆盖路径安全、token 计数、假模型、预算配额、装配裁剪、工具校验、文件与搜索工具、任务状态、协议解析、主循环、端到端。
2. `.venv\Scripts\python.exe -m agents_dev.cli.app run --goal "..." --script s.json` 能在无 GPU 环境下跑通完整闭环。
3. 主循环在上下文**需求**触达硬触发线时能重置且不丢失任务状态，此行为有测试覆盖。
4. 不引入 `httpx` 之外的运行时依赖。

## 后续计划（不在本计划范围内）

- 计划二：代码索引层（`ast` 符号提取、L0/L1 分层加载、引用图、增量更新）。
- 计划三：记忆系统（热记忆文件、SQLite 冷记忆、FTS5 检索、归档与提炼、教训机制）。
- 计划四：真实模型接入（llama.cpp HTTP 网关、`/tokenize` 精确计数、GBNF 语法约束生成、提示词调优）。
- 计划五：约束检查器与子智能体（实现者/审查者、分派协议、分级授权）。

