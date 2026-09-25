# 工作区与会话内核（批次一）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把"工作在哪个工作区、哪条会话"从启动参数变成内核状态，四个入口（CLI / 聊天通道 / 网页壳 / MCP）改成它的客户端，放行从工作区级升到身份级。

**Architecture:** 新增 `spoolkit/kernel/` 四模块（工作区登记、会话登记、客户端指向、放行），机器级状态落在 `workspace.state_dir()` 下；`spoolkit/workspace.py` 保留为转出壳，`resolve_root` 逐步变成 `resolve_target` 的薄包装。入口面只调用内核，不再各自解析 `--root`。网页壳的换脸不在本批（批次二）。

**Tech Stack:** Python ≥ 3.12，标准库 + 现有依赖（零新依赖）；测试用 pytest，命令行入口 `.\.venv\Scripts\python.exe`。

**设计文档：** [`docs/superpowers/specs/2026-09-24-workspace-session-kernel-design.md`](../specs/2026-09-24-workspace-session-kernel-design.md)

## Global Constraints

- Python ≥ 3.12；**不加任何新依赖**（内核只用标准库）。
- 注释与文档用中文，标识符用英文。
- 上限、名单、阈值**不写死**：由调用方给参数，或按工作区放 `.agent/limits.json`。
- **每个改动都带测试**。提交前跑全套：
  `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`
  基线是 **1394 passed / 3 failed**；那 3 条（`tests/cli/test_service_command.py` 两条、
  `tests/tools/test_js_verify.py` 一条）是本机沙箱**禁止起子进程**导致的，与代码无关。
  除此之外出现任何失败都算回归。
- **只 stage 本次改的文件，禁止 `git add -A`**（工作区里还有别人的未跟踪文件）。
- 机器级状态目录一律用 `workspace.state_dir()`（`SPOOLKIT_STATE` 可覆盖，测试靠它隔离）。
- 兼容是**验收条件**：旧登记表格式、旧配对文件、旧会话名（`cli` / `chat` / `bridge` / `mcp`）
  都不许坏，且旧文件**不删**。
- `--events` 的既有事件语义（`start/step/tool/diff/await/confirm/usage/final/error/note/wait`）
  只增不改；agent 的工具表不动。
- 每个任务改完在 `docs/dogfooding.md` 补一条（做了什么、量到什么、哪条被证伪），
  由最后两个任务统一回写其余文档。

---

## 文件结构

```
新增
  src/spoolkit/kernel/__init__.py       内核对外出口（只导出 Target / resolve_target / 四个模块）
  src/spoolkit/kernel/jsonstore.py      机器级小文件的读-改-写（进程内锁 + 原子替换）
  src/spoolkit/kernel/workspaces.py     工作区：登记表、别名、显示名、按名字解析
  src/spoolkit/kernel/pointers.py       客户端指向：clients.json
  src/spoolkit/kernel/sessions.py       会话：列表、新建、解析、改名、归档、标题
  src/spoolkit/kernel/target.py         Target 与 resolve_target（唯一的解析入口）
  src/spoolkit/kernel/trust.py          放行：谁 + 能碰哪些工作区；配对码；认领旧记录
  src/spoolkit/cli/commands/ws.py       `spool ws`
  tests/kernel/…                        内核用例
  tests/cli/test_ws_command.py          `spool ws` 用例

改造
  src/spoolkit/workspace.py             变成转出壳（保住 workspace.resolve_root 等老导入路径）
  src/spoolkit/cli/commands/session.py  扩成有生命周期的会话命令
  src/spoolkit/cli/app.py               注册 `ws`；main 用 resolve_target 解析
  src/spoolkit/cli/commands/chat.py     开局那一眼 + /ws /new /sessions /use
  src/spoolkit/cli/commands/bridge.py   `--root` 变可选；认领旧配对
  src/spoolkit/bridge/commands.py       斜杠命令接内核（按目标工作区）
  src/spoolkit/bridge/core.py           准入两步（身份 → 工作区）；runner 按客户端键取目标
  src/spoolkit/bridge/agent_runner.py   按 (工作区, 会话) 复用一个 Runner
  src/spoolkit/mcp/server.py            delegate_task 加 workspace/session；两个只读工具

文档（最后一题统一回写）
  README.md / docs/manual.md / docs/cli.md / docs/bridge.md / docs/mcp.md
  docs/getting-started.md / docs/roadmap.md / docs/dogfooding.md
```

---

### Task 1: 内核 · 机器级小文件 + 工作区登记

**Files:**
- Create: `src/spoolkit/kernel/__init__.py`
- Create: `src/spoolkit/kernel/jsonstore.py`
- Create: `src/spoolkit/kernel/workspaces.py`
- Modify: `src/spoolkit/workspace.py`（改成转出壳，`resolve_root` 暂留在这里）
- Test: `tests/kernel/__init__.py`、`tests/kernel/test_jsonstore.py`、`tests/kernel/test_workspaces.py`

**Interfaces:**
- Consumes: 无（本任务是底座）
- Produces:
  - `kernel.jsonstore.read_json(path, default) -> Any`
  - `kernel.jsonstore.write_json(path, payload) -> None`（原子替换，失败抛 `OSError`）
  - `kernel.jsonstore.update_json(path, mutate, default) -> Any`
  - `kernel.workspaces`：`MARK=".agent"`、`REGISTRY="workspaces.json"`、`ALIASES="workspace-aliases.json"`、
    `STATE_ENV="SPOOLKIT_STATE"`、`WorkspaceError`、`Workspace(root, name, alias)`、
    `state_dir()`、`registry_path()`、`aliases_path()`、`find_root(start=None)`、`known()`、
    `register(root)`、`forget(name_or_path) -> Path`、`set_alias(root_or_name, alias) -> None`、
    `load_aliases() -> dict[str, Path]`、`list_workspaces() -> list[Workspace]`、
    `resolve_workspace(name_or_path) -> Path`

- [ ] **Step 1: 写失败的测试（jsonstore）**

```python
# tests/kernel/test_jsonstore.py
"""机器级小文件：坏了当空、写不进去要能看出来。"""
from pathlib import Path

import pytest

from spoolkit.kernel import jsonstore


def test_读不到就是默认值(tmp_path: Path) -> None:
    assert jsonstore.read_json(tmp_path / "没有这个.json", {"a": 1}) == {"a": 1}


def test_内容坏了也当默认值(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    assert jsonstore.read_json(path, {"a": 1}) == {"a": 1}


def test_改一次会原样落盘(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    jsonstore.update_json(path, lambda data: {**data, "n": data.get("n", 0) + 1}, {})
    jsonstore.update_json(path, lambda data: {**data, "n": data.get("n", 0) + 1}, {})
    assert jsonstore.read_json(path, {}) == {"n": 2}
    assert not path.with_name(path.name + ".tmp").exists()
```

- [ ] **Step 2: 跑一次，确认它失败**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel/test_jsonstore.py`
Expected: FAIL，`ModuleNotFoundError: No module named 'spoolkit.kernel'`

- [ ] **Step 3: 写 jsonstore 与内核出口**

```python
# src/spoolkit/kernel/jsonstore.py
"""机器级小文件的读写。

为什么单独一层：登记表、客户端指向、放行都是"读-改-写"的小文件，且可能被多个
进程同时碰（CLI 一个、桥一个、hub 一个）。规则收在一处，别让每个模块自己写一遍。

**不做跨进程文件锁**：改这些文件的动作（切工作区、批准放行）都是人在按，本来
就不是并发写，为它引入一套锁协议不划算。只做两件便宜的事——进程内加锁（挡住
同一进程的多个线程，网页壳就有），以及"写临时文件再原子替换"（挡住读到半份）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def read_json(path: Path, default: Any) -> Any:
    """读不到、坏了、不是 JSON 对象，一律给默认值——它们是便利，不是数据。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return payload


def write_json(path: Path, payload: Any) -> None:
    """写失败会抛 OSError（调用方决定是吞掉还是报给人）。"""
    path = Path(path)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # 临时文件留下会被人当成"状态文件"翻，清掉再抛。
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def update_json(path: Path, mutate: Callable[[Any], Any], default: Any) -> Any:
    """读-改-写，整段在进程内锁里完成。"""
    path = Path(path)
    with _lock_for(path):
        updated = mutate(read_json(path, default))
        write_json(path, updated)
        return updated
```

```python
# src/spoolkit/kernel/__init__.py
"""工作区与会话内核。

四个入口（CLI / 聊天通道 / 网页壳 / MCP）都从这里拿"现在该在哪儿干活"，不再各自
解析 `--root`。实现细节在各子模块里；这里只做出口，免得调用方到处 import 路径。
"""

from spoolkit.kernel.workspaces import WorkspaceError

__all__ = ["WorkspaceError"]
```

- [ ] **Step 4: 跑 jsonstore 用例，确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel/test_jsonstore.py`
Expected: PASS（3 passed）

- [ ] **Step 5: 写失败的测试（工作区登记）**

```python
# tests/kernel/test_workspaces.py
"""工作区登记：列、按名字/别名/序号/路径解析、忘掉。

登记表本身的四条规则（向上找、显式就是显式、MRU、坏了不挡活）钉在
`tests/test_workspace_root.py` 里，这里只管**新增的那一层命名**。
"""
from pathlib import Path

import pytest

from spoolkit.kernel import workspaces as kws


def _ws(base: Path, name: str) -> Path:
    root = base / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def test_列出登记表带上显示名(tmp_path: Path) -> None:
    first = _ws(tmp_path, "alpha")
    kws.register(first)
    listed = kws.list_workspaces()
    assert [item.root for item in listed] == [first]
    assert listed[0].name == "alpha"


def test_重名时显示名带上一级(tmp_path: Path) -> None:
    left = _ws(tmp_path / "left", "app")
    right = _ws(tmp_path / "right", "app")
    kws.register(left)
    kws.register(right)
    names = {item.root: item.name for item in kws.list_workspaces()}
    assert names[left] == "left/app"
    assert names[right] == "right/app"


def test_按目录名解析(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    assert kws.resolve_workspace("alpha") == root


def test_按别名解析(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    kws.set_alias(root, "我的项目")
    assert kws.resolve_workspace("我的项目") == root


def test_按序号解析且从1起(tmp_path: Path) -> None:
    first = _ws(tmp_path, "alpha")
    second = _ws(tmp_path, "beta")
    kws.register(first)
    kws.register(second)
    assert kws.resolve_workspace("1") == second  # 最近登记的在前
    assert kws.resolve_workspace("2") == first


def test_按路径解析会往上找(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    deep = root / "src" / "pkg"
    deep.mkdir(parents=True)
    assert kws.resolve_workspace(deep) == root


def test_路径不是工作区时报清楚(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(kws.WorkspaceError) as raised:
        kws.resolve_workspace(plain)
    assert kws.MARK in str(raised.value)


def test_没找到时列出候选(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    with pytest.raises(kws.WorkspaceError) as raised:
        kws.resolve_workspace("查无此区")
    assert str(root) in str(raised.value)


def test_同名多个时报清楚并给出出路(tmp_path: Path) -> None:
    left = _ws(tmp_path / "left", "app")
    right = _ws(tmp_path / "right", "app")
    kws.register(left)
    kws.register(right)
    with pytest.raises(kws.WorkspaceError) as raised:
        kws.resolve_workspace("app")
    message = str(raised.value)
    assert str(left) in message and str(right) in message
    assert "alias" in message


def test_忘掉会摘掉登记与别名但不删目录(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    kws.set_alias(root, "我的项目")
    assert kws.forget("我的项目") == root
    assert kws.list_workspaces() == []
    assert (root / kws.MARK).is_dir()


def test_登记表格式不变还是纯路径数组(tmp_path: Path) -> None:
    """兼容是验收条件：别的工具可能按老格式读它。"""
    import json

    root = _ws(tmp_path, "alpha")
    kws.register(root)
    payload = json.loads(kws.registry_path().read_text(encoding="utf-8"))
    assert payload == {"workspaces": [str(root)]}
```

同一批要建的 `tests/kernel/__init__.py` 是空文件（这个仓库的 `tests/` 子目录都有）。

- [ ] **Step 6: 跑一次，确认工作区用例失败**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel/test_workspaces.py`
Expected: FAIL，`ImportError: cannot import name 'workspaces' from 'spoolkit.kernel'`

- [ ] **Step 7: 写工作区登记**

把 `src/spoolkit/workspace.py` 原有的实现**原样搬进** `kernel/workspaces.py`：
`state_dir` / `registry_path` / `MARK` / `REGISTRY` / `STATE_ENV` / `WorkspaceError` /
`find_root` / `known` / `register` 的代码一字不改（连那些解释"为什么"的注释一起搬），
再在这基础上加命名那一层：

```python
# src/spoolkit/kernel/workspaces.py
"""工作区：在哪、叫什么、怎么找到它。

搬过来的是原来 `spoolkit/workspace.py` 的规则（向上找 `.agent/`、显式就是显式、
登记表是最近使用序），新增的是**命名**：目录名、别名、序号。

别名单独放一个文件（`workspace-aliases.json`），**不动登记表的格式**——别的工具
可能按 `{"workspaces": ["D:\\..."]}` 这个老格式读它，往里塞对象会把它们弄瞎。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from spoolkit.kernel import jsonstore

MARK = ".agent"
REGISTRY = "workspaces.json"
ALIASES = "workspace-aliases.json"
STATE_ENV = "SPOOLKIT_STATE"


class WorkspaceError(RuntimeError):
    """工作区找不到、或者有好几个说不清是哪个。"""


@dataclass(frozen=True)
class Workspace:
    """登记表里的一个工作区。`name` 是显示名（重名时带上一级）。"""

    root: Path
    name: str
    alias: str = ""


def state_dir() -> Path:
    """这台机器上放用户级状态的地方（登记表、指向、放行）。"""
    override = os.environ.get(STATE_ENV)
    if override:
        return Path(override)
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "spoolkit"


def registry_path() -> Path:
    return state_dir() / REGISTRY


def aliases_path() -> Path:
    return state_dir() / ALIASES


def find_root(start: Path | None = None) -> Path | None:
    """从 `start`（默认当前目录）往上找带 `.agent/` 的那一层。"""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / MARK).is_dir():
            return candidate
    return None


def known() -> list[Path]:
    """登记表里的工作区（只留现在还在的，顺序是最近使用在前）。"""
    payload = jsonstore.read_json(registry_path(), {})
    items = payload.get("workspaces") if isinstance(payload, dict) else payload
    found: list[Path] = []
    for item in items or []:
        try:
            candidate = Path(str(item)).resolve()
        except (OSError, ValueError):
            continue
        if candidate.is_dir() and candidate not in found:
            found.append(candidate)
    return found


def register(root: Path | str) -> None:
    """把工作区登记下来（幂等，最近用的排最前）。写不进去不挡干活。"""
    target = Path(root).resolve()
    order = [item for item in known() if item != target]
    order.insert(0, target)
    try:
        jsonstore.write_json(
            registry_path(), {"workspaces": [str(item) for item in order]}
        )
    except OSError:
        # 登记不上不该挡住干活：最坏是下次还得写 --root。
        pass


def load_aliases() -> dict[str, Path]:
    """别名 → 工作区。坏了、指向不存在的工作区，都当没设过。"""
    payload = jsonstore.read_json(aliases_path(), {})
    raw = payload.get("aliases") if isinstance(payload, dict) else None
    found: dict[str, Path] = {}
    for alias, target in (raw or {}).items():
        try:
            path = Path(str(target)).resolve()
        except (OSError, ValueError):
            continue
        if path.is_dir():
            found[str(alias)] = path
    return found


def set_alias(root: Path | str, alias: str) -> None:
    """给工作区起个别名。同一个工作区的旧别名会被新别名顶掉。"""
    target = Path(root).resolve()
    name = str(alias).strip()
    if not name:
        raise WorkspaceError("别名不能是空的")
    if name.isdigit():
        # 序号是解析顺序里的一个位置，被别名抢走之后那人就再也点不动第 3 个了。
        raise WorkspaceError("别名不能用纯数字（那是按序号选的写法）")
    current = {key: value for key, value in load_aliases().items() if value != target}
    current[name] = target
    jsonstore.write_json(
        aliases_path(), {"aliases": {k: str(v) for k, v in current.items()}}
    )


def _display_names(roots: list[Path]) -> dict[Path, str]:
    """重名时带上父目录名：两个 `app` 在列表里要分得开。"""
    counts: dict[str, int] = {}
    for root in roots:
        counts[root.name] = counts.get(root.name, 0) + 1
    return {
        root: (root.name if counts[root.name] == 1 else f"{root.parent.name}/{root.name}")
        for root in roots
    }


def list_workspaces() -> list[Workspace]:
    """登记表 + 别名 + 显示名，最近使用在前。"""
    roots = known()
    names = _display_names(roots)
    by_root = {value: key for key, value in load_aliases().items()}
    return [
        Workspace(root=root, name=names[root], alias=by_root.get(root, ""))
        for root in roots
    ]


def forget(name_or_path: str | Path) -> Path:
    """从登记表摘掉（**不删目录、不删 `.agent/`**）。"""
    root = resolve_workspace(name_or_path)
    order = [item for item in known() if item != root]
    jsonstore.write_json(registry_path(), {"workspaces": [str(item) for item in order]})
    rest = {key: value for key, value in load_aliases().items() if value != root}
    jsonstore.write_json(
        aliases_path(), {"aliases": {k: str(v) for k, v in rest.items()}}
    )
    return root


def resolve_workspace(name_or_path: str | Path) -> Path:
    """名字、别名、序号（1 起）或路径。找不到就抛 `WorkspaceError`，**不猜**。"""
    raw = str(name_or_path).strip()
    if not raw:
        raise WorkspaceError("没有给工作区名字或路径")

    aliases = load_aliases()
    if raw in aliases:
        return aliases[raw]

    candidate = Path(raw)
    if candidate.exists():
        found = find_root(candidate)
        if found is not None:
            return found
        raise WorkspaceError(
            f"{candidate.resolve()} 不是工作区（没有 {MARK}/）。"
            f"要在那里干活先 `spool ws add {candidate.resolve()}`。"
        )

    items = list_workspaces()

    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(items):
            return items[index - 1].root
        raise WorkspaceError(f"只有 {len(items)} 个登记过的工作区，没有第 {index} 个。")

    # 按**目录名**匹配，不是显示名：重名时显示名会带上一级（left/app），
    # 于是敲 `app` 永远匹配不上、「同名多个」那条分支根本走不到。
    exact = [item for item in items if item.root.name == raw or item.alias == raw]
    if len(exact) == 1:
        return exact[0].root
    if len(exact) > 1:
        listed = "\n".join(f"  {item.root}" for item in exact)
        raise WorkspaceError(
            f"有多个工作区都叫 {raw}：\n{listed}\n"
            f"用完整路径，或起个别名：`spool ws alias <路径> {raw}`"
        )
    if not items:
        raise WorkspaceError(
            f"没有叫 {raw} 的工作区，登记表也是空的。先 `spool ws add <路径>`。"
        )
    listed = "\n".join(
        f"  {index}) {item.root}" for index, item in enumerate(items, start=1)
    )
    raise WorkspaceError(f"没有叫 {raw} 的工作区。现在登记的有：\n{listed}")
```

- [ ] **Step 8: 把 `workspace.py` 改成转出壳**

`src/spoolkit/workspace.py` 只留 `resolve_root` 那一套（下一个任务会把它接到
`resolve_target`），其余从内核转出。docstring 要写清"为什么搬家"，否则下一个人会
以为这文件是死的：

```python
"""工作区：从哪来、怎么找、记在哪。

**这个文件现在是一层壳。** 实现在 `spoolkit.kernel.workspaces`：那里多了命名
（目录名、别名、序号）与客户端指向，四个入口都要用。这里保留同名导出，是因为
外部与测试一直按 `workspace.resolve_root` / `workspace.MARK` 这些路径导入——
改名那次已经吃过一次"旧导入路径断掉"的亏，不再吃第二次。

`resolve_root` 仍然住在这里，因为它的签名（`choose` / `interactive`）是给"人在
终端前、登记表里有好几个"那个交互场景的，纯内核不该知道终端怎么问人。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Iterable

from spoolkit.kernel.workspaces import (
    ALIASES,
    MARK,
    REGISTRY,
    STATE_ENV,
    Workspace,
    WorkspaceError,
    aliases_path,
    find_root,
    forget,
    known,
    list_workspaces,
    load_aliases,
    register,
    registry_path,
    resolve_workspace,
    set_alias,
    state_dir,
)

__all__ = [
    "ALIASES",
    "MARK",
    "REGISTRY",
    "STATE_ENV",
    "Workspace",
    "WorkspaceError",
    "aliases_path",
    "find_root",
    "forget",
    "known",
    "list_workspaces",
    "load_aliases",
    "register",
    "registry_path",
    "resolve_root",
    "resolve_workspace",
    "set_alias",
    "state_dir",
]


def resolve_root(
    explicit: str | Path | None = None,
    *,
    choose: Callable[[list[Path]], Path] | None = None,
    interactive: Callable[[], bool] | None = None,
) -> Path:
    """按四条规则定出工作区（这一段本任务只搬家，不改行为）。"""
    if explicit not in (None, ""):
        return Path(str(explicit)).resolve()

    found = find_root()
    if found is not None:
        register(found)
        return found

    candidates = known()
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        chooser = choose or _ask_which
        is_interactive = interactive or _interactive
        if is_interactive():
            picked = chooser(candidates)
            register(picked)
            return picked
        listed = "\n".join(f"  {item}" for item in candidates)
        raise WorkspaceError(
            "当前目录不是工作区，而登记表里有好几个，说不清是哪一个：\n"
            f"{listed}\n"
            "用 `--root <路径>` 指定一个。"
        )
    return Path.cwd().resolve()


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ValueError, AttributeError):  # pragma: no cover - 少数被包装过的流
        return False


def _ask_which(candidates: Iterable[Path]) -> Path:
    items = list(candidates)
    print("当前目录不是工作区，登记表里有这几个：")
    for index, item in enumerate(items, start=1):
        print(f"  {index}) {item}")
    raw = input("选哪个 [1]：").strip() or "1"
    try:
        return items[int(raw) - 1]
    except (ValueError, IndexError):
        return items[0]
```

- [ ] **Step 9: 跑内核用例与老的工作区用例**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel tests/test_workspace_root.py tests/test_rename_compat.py tests/test_paths.py tests/test_end_to_end.py`
Expected: PASS。`tests/test_workspace_root.py` **一条都不许改**——它是"老行为没变"的凭据。

- [ ] **Step 10: 提交**

```bash
git add src/spoolkit/kernel src/spoolkit/workspace.py tests/kernel
git commit -m "feat(kernel): 工作区登记与命名（别名/序号/显示名）" -m "为什么：工作区解析规则只认得路径，而四个入口要能在运行时按名字选；登记表本来就存着最近使用的顺序，不用白不用。" -m "怎么做：新增 spoolkit/kernel（jsonstore 做机器级小文件的原子读写；workspaces 搬来原有四条规则并加命名层）。别名单独一个文件，登记表格式不动，避免把别的工具弄瞎。workspace.py 变转出壳，老导入路径与 tests/test_workspace_root.py 一条不改。" -m "测试：tests/kernel/test_jsonstore.py、tests/kernel/test_workspaces.py"
```

---

### Task 2: 内核 · 客户端指向与唯一解析入口

**Files:**
- Create: `src/spoolkit/kernel/pointers.py`
- Create: `src/spoolkit/kernel/target.py`
- Modify: `src/spoolkit/kernel/__init__.py`（出口加 `Target` / `resolve_target`）
- Modify: `src/spoolkit/workspace.py`（`resolve_root` 改成 `resolve_target` 的薄包装）
- Test: `tests/kernel/test_pointers.py`、`tests/kernel/test_target.py`

**Interfaces:**
- Consumes: `kernel.workspaces.{find_root,known,register,list_workspaces}`、`kernel.jsonstore`
- Produces:
  - `kernel.pointers`：`POINTERS="clients.json"`、`Pointer(workspace: Path, session: str)`、
    `pointers_path()`、`all_pointers() -> dict[str, Pointer]`、`get(client_key) -> Pointer | None`、
    `set(client_key, root, session) -> None`、`clear(client_key) -> None`
  - `kernel.target`：`Target(root, session, workspace_source, session_source)`、
    `resolve_target(*, workspace=None, session=None, client="cli", session_fallback="cli", on_ambiguous=None) -> Target`
  - `workspace.resolve_root(explicit=None, *, choose=None, interactive=None) -> Path`（**老签名不变**）

- [ ] **Step 1: 写失败的测试（指向）**

```python
# tests/kernel/test_pointers.py
"""客户端指向：某个入口"现在指着哪儿"。"""
from pathlib import Path

from spoolkit.kernel import pointers


def test_没设过就是没有(tmp_path: Path) -> None:
    assert pointers.get("cli") is None


def test_设了能读回来(tmp_path: Path) -> None:
    pointers.set("cli", tmp_path, "alpha")
    got = pointers.get("cli")
    assert got is not None
    assert got.workspace == tmp_path.resolve()
    assert got.session == "alpha"


def test_每个键互不影响(tmp_path: Path) -> None:
    pointers.set("cli", tmp_path / "a", "one")
    pointers.set("chat:qqbot:u1", tmp_path / "b", "two")
    assert pointers.get("cli").session == "one"
    assert pointers.get("chat:qqbot:u1").session == "two"


def test_清掉之后就没有了(tmp_path: Path) -> None:
    pointers.set("cli", tmp_path, "alpha")
    pointers.clear("cli")
    assert pointers.get("cli") is None


def test_文件坏了当没设过(tmp_path: Path) -> None:
    path = pointers.pointers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 json", encoding="utf-8")
    assert pointers.get("cli") is None
    assert pointers.all_pointers() == {}
```

- [ ] **Step 2: 跑一次，确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel/test_pointers.py`
Expected: FAIL，`ImportError: cannot import name 'pointers' from 'spoolkit.kernel'`

- [ ] **Step 3: 写指向**

```python
# src/spoolkit/kernel/pointers.py
"""客户端指向：某个入口"现在指着哪儿"，即一对 (工作区, 会话)。

为什么需要它：四个入口要么每次让人敲 `--root`（记不住），要么在启动时钉死
（换不了）。指向把"上次选的那一对"记下来，作为解析的**一层兜底**——它永远排在
"当前目录向上找"后面（在项目子目录里敲命令，指定性明显强于"我上次在别处选过谁"）。

聊天通道按**每个聊天**分键（`chat:<通道>:<用户>`），所以两个群可以各自指着不同的
工作区，互不影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from spoolkit.kernel import jsonstore
from spoolkit.kernel.workspaces import state_dir

POINTERS = "clients.json"


@dataclass(frozen=True)
class Pointer:
    workspace: Path
    session: str


def pointers_path() -> Path:
    return state_dir() / POINTERS


def all_pointers() -> dict[str, Pointer]:
    """坏条目直接跳过：指向是便利，不是数据。"""
    payload = jsonstore.read_json(pointers_path(), {})
    raw = payload.get("clients") if isinstance(payload, dict) else None
    found: dict[str, Pointer] = {}
    for key, value in (raw or {}).items():
        if not isinstance(value, dict):
            continue
        text = str(value.get("workspace") or "").strip()
        if not text:
            continue
        try:
            workspace = Path(text).resolve()
        except (OSError, ValueError):
            continue
        found[str(key)] = Pointer(
            workspace=workspace, session=str(value.get("session") or "")
        )
    return found


def get(client_key: str) -> Pointer | None:
    return all_pointers().get(str(client_key))


def set(client_key: str, root: Path | str, session: str) -> None:
    """写指针。工作区存不存在由调用方先判（这里不做工作区解析）。"""
    root_text = str(Path(root).resolve())
    session_text = str(session)

    def mutate(payload: object) -> dict:
        data = payload if isinstance(payload, dict) else {}
        clients = dict(data.get("clients") or {})
        clients[str(client_key)] = {"workspace": root_text, "session": session_text}
        return {**data, "clients": clients}

    jsonstore.update_json(pointers_path(), mutate, {})


def clear(client_key: str) -> None:
    def mutate(payload: object) -> dict:
        data = payload if isinstance(payload, dict) else {}
        clients = dict(data.get("clients") or {})
        clients.pop(str(client_key), None)
        return {**data, "clients": clients}

    jsonstore.update_json(pointers_path(), mutate, {})
```

- [ ] **Step 4: 跑指向用例，确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel/test_pointers.py`
Expected: PASS（5 passed）

- [ ] **Step 5: 写失败的测试（解析入口）**

```python
# tests/kernel/test_target.py
"""唯一解析入口：显式 > 向上找 > 指针 > 登记表 > 当前目录。"""
from pathlib import Path

import pytest

from spoolkit.kernel import pointers, workspaces as kws
from spoolkit.kernel.target import resolve_target


def _ws(base: Path, name: str) -> Path:
    root = base / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def test_显式工作区最优先(tmp_path: Path, monkeypatch) -> None:
    here = _ws(tmp_path, "here")
    other = _ws(tmp_path, "other")
    monkeypatch.chdir(here)
    pointers.set("cli", other, "s1")
    target = resolve_target(workspace=other)
    assert target.root == other
    assert target.workspace_source == "explicit"


def test_当前目录向上找压过指针(tmp_path: Path, monkeypatch) -> None:
    here = _ws(tmp_path, "here")
    other = _ws(tmp_path, "other")
    pointers.set("cli", other, "s1")
    monkeypatch.chdir(here)
    target = resolve_target()
    assert target.root == here
    assert target.workspace_source == "cwd"


def test_不在工作区里时用指针(tmp_path: Path, monkeypatch) -> None:
    other = _ws(tmp_path, "other")
    plain = tmp_path / "plain"
    plain.mkdir()
    pointers.set("cli", other, "s1")
    monkeypatch.chdir(plain)
    target = resolve_target()
    assert target.root == other
    assert target.workspace_source == "pointer"


def test_指针指向的工作区没了就接着往下退(tmp_path: Path, monkeypatch) -> None:
    gone = _ws(tmp_path, "gone")
    keeper = _ws(tmp_path, "keeper")
    kws.register(keeper)
    pointers.set("cli", gone, "s1")
    (gone / kws.MARK).rmdir()
    gone.rmdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_target().root == keeper


def test_登记表多个又不交互就报清楚(tmp_path: Path, monkeypatch) -> None:
    first = _ws(tmp_path, "a")
    second = _ws(tmp_path, "b")
    kws.register(first)
    kws.register(second)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    with pytest.raises(kws.WorkspaceError) as raised:
        resolve_target()
    assert "--root" in str(raised.value)


def test_说不清时可以交给调用方挑(tmp_path: Path, monkeypatch) -> None:
    first = _ws(tmp_path, "a")
    second = _ws(tmp_path, "b")
    kws.register(first)
    kws.register(second)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    # `known()` 是"最近使用在前"：先 register(first) 再 register(second) 得
    # `[second, first]`，所以 `items[1]` 是 first（这一行原稿写成 second，是笔误）。
    assert resolve_target(on_ambiguous=lambda items: items[1]).root == first


def test_一个都没有就退回当前目录(tmp_path: Path, monkeypatch) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    target = resolve_target()
    assert target.root == plain.resolve()
    assert target.workspace_source == "cwd-fallback"


def test_会话显式最优先(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path, "here")
    monkeypatch.chdir(root)
    pointers.set("cli", root, "from-pointer")
    assert resolve_target(session="asked").session == "asked"


def test_指针里的会话只在同一个工作区时生效(tmp_path: Path, monkeypatch) -> None:
    here = _ws(tmp_path, "here")
    other = _ws(tmp_path, "other")
    pointers.set("cli", other, "from-pointer")
    monkeypatch.chdir(here)
    target = resolve_target()
    assert target.session == "cli"
    assert target.session_source == "default"


def test_会话默认值由入口给(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path, "here")
    monkeypatch.chdir(root)
    assert resolve_target(session_fallback="bridge").session == "bridge"
```

- [ ] **Step 6: 跑一次，确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel/test_target.py`
Expected: FAIL，`ModuleNotFoundError: No module named 'spoolkit.kernel.target'`

- [ ] **Step 7: 写解析入口**

```python
# src/spoolkit/kernel/target.py
"""唯一的解析入口：这次到底在哪个工作区、哪条会话里干活。

顺序（见设计文档 3.4 / 3.5）：

  工作区：显式 --root > 当前目录向上找 .agent/ > 客户端指向 > 登记表 > 当前目录
  会话：  显式 --session > 客户端指向（仅当指向的是同一个工作区）> 入口的默认名

"向上找"排在指针前面是有意的：在项目子目录里敲命令，指定性比"我上次在别处选过谁"
强得多。登记表里有多个又没人可问时**报错而不是猜**——猜错的症状是"这个命令的记忆
跟别的命令不是同一份"，那是最难查的一类问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from spoolkit.kernel import pointers, workspaces
from spoolkit.kernel.workspaces import WorkspaceError


@dataclass(frozen=True)
class Target:
    """这次要干活的地方。`*_source` 是给"开局那一眼"交代来源用的。"""

    root: Path
    session: str
    workspace_source: str  # explicit / cwd / pointer / registry / cwd-fallback
    session_source: str    # explicit / pointer / default


def resolve_target(
    *,
    workspace: str | Path | None = None,
    session: str | None = None,
    client: str = "cli",
    session_fallback: str = "cli",
    on_ambiguous: Callable[[list[Path]], Path] | None = None,
) -> Target:
    root, workspace_source = _resolve_workspace(workspace, client, on_ambiguous)
    name, session_source = _resolve_session(session, client, root, session_fallback)
    return Target(
        root=root,
        session=name,
        workspace_source=workspace_source,
        session_source=session_source,
    )


def _resolve_workspace(
    explicit: str | Path | None,
    client: str,
    on_ambiguous: Callable[[list[Path]], Path] | None,
) -> tuple[Path, str]:
    if explicit not in (None, ""):
        root = Path(str(explicit)).resolve()
        workspaces.register(root)  # 显式给的值不写指针，但登记表照旧更新
        return root, "explicit"

    found = workspaces.find_root()
    if found is not None:
        workspaces.register(found)
        return found, "cwd"

    pointer = pointers.get(client)
    if pointer is not None and (pointer.workspace / workspaces.MARK).is_dir():
        workspaces.register(pointer.workspace)
        return pointer.workspace, "pointer"

    candidates = workspaces.known()
    if len(candidates) == 1:
        return candidates[0], "registry"
    if len(candidates) > 1:
        if on_ambiguous is not None:
            picked = on_ambiguous(candidates)
            workspaces.register(picked)
            return picked, "registry"
        listed = "\n".join(f"  {item}" for item in candidates)
        raise WorkspaceError(
            "当前目录不是工作区，而登记表里有好几个，说不清是哪一个：\n"
            f"{listed}\n"
            "用 `--root <路径>` 指定一个。"
        )
    return Path.cwd().resolve(), "cwd-fallback"


def _resolve_session(
    explicit: str | None,
    client: str,
    root: Path,
    fallback: str,
) -> tuple[str, str]:
    if explicit not in (None, ""):
        return str(explicit), "explicit"
    pointer = pointers.get(client)
    # 会话属于工作区：指向的工作区跟这次要干的不是同一个时，那个会话名不能搬过来。
    if pointer is not None and pointer.session and pointer.workspace == root:
        return pointer.session, "pointer"
    return fallback, "default"
```

顺带把内核出口与兼容壳接上：

```python
# src/spoolkit/kernel/__init__.py
from spoolkit.kernel.target import Target, resolve_target
from spoolkit.kernel.workspaces import WorkspaceError

__all__ = ["Target", "WorkspaceError", "resolve_target"]
```

```python
# src/spoolkit/workspace.py（只换 resolve_root，导出那一段不动）
def _ambiguous_error(candidates: list[Path]) -> Path:
    """说不清是哪一个。文案一字不改：tests/test_workspace_root.py 钉着它。"""
    listed = "\n".join(f"  {item}" for item in candidates)
    raise WorkspaceError(
        "当前目录不是工作区，而登记表里有好几个，说不清是哪一个：\n"
        f"{listed}\n"
        "用 `--root <路径>` 指定一个。"
    )


def resolve_root(
    explicit: str | Path | None = None,
    *,
    choose: Callable[[list[Path]], Path] | None = None,
    interactive: Callable[[], bool] | None = None,
) -> Path:
    """老签名、老行为。多出来的只是"指针"这一层兜底。"""
    from spoolkit.kernel.target import resolve_target

    on_ambiguous: Callable[[list[Path]], Path] | None = None
    if choose is not None or interactive is not None:
        def on_ambiguous(candidates: list[Path]) -> Path:
            asks = interactive or _interactive
            if not asks():
                return _ambiguous_error(candidates)
            return (choose or _ask_which)(candidates)

    return resolve_target(
        workspace=explicit, client="cli", on_ambiguous=on_ambiguous
    ).root
```

- [ ] **Step 8: 跑内核与工作区用例**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel tests/test_workspace_root.py tests/test_paths.py tests/test_session_isolation.py`
Expected: PASS

- [ ] **Step 9: 提交**

```bash
git add src/spoolkit/kernel src/spoolkit/workspace.py tests/kernel
git commit -m "feat(kernel): 客户端指向与唯一解析入口 resolve_target" -m "为什么：四个入口都要知道这次在哪个工作区哪条会话，这件事不能继续各自解析 --root。指向把上次选过谁记下来当兜底，排在当前目录向上找后面。" -m "怎么做：pointers 按客户端键存一对 (工作区, 会话)，聊天通道按每个聊天分键；target.resolve_target 实现 spec 3.4/3.5 的顺序，登记表说不清时报错而不是猜，交互挑人那条由调用方通过 on_ambiguous 注入（内核不碰终端）。workspace.resolve_root 变薄包装，老签名与报错文案一字不改。" -m "测试：tests/kernel/test_pointers.py、tests/kernel/test_target.py"
```

---

### Task 3: 内核 · 会话登记（列表 / 新建 / 解析 / 改名 / 归档 / 标题）

**Files:**
- Create: `src/spoolkit/kernel/sessions.py`
- Test: `tests/kernel/test_sessions.py`

**Interfaces:**
- Consumes: `spoolkit.session_state.safe/dir_for`、`spoolkit.memory.store.{init_memory_schema,list_sessions,record_session,get_session}`、`spoolkit.store.db.open_db`、`kernel.jsonstore`
- Produces:
  - `META_NAME = "meta.json"`、`TITLE_LIMIT = 40`
  - `SessionError(RuntimeError)`
  - `Session(id, title, model, context_limit, created_at, last_active, archived)`
  - `meta_path(root, session_id) -> Path`、`read_meta(root, session_id) -> dict`、`write_meta(root, session_id, **changes) -> dict`
  - `list_sessions(root, include_archived=False) -> list[Session]`
  - `create_session(root, title="") -> str`
  - `resolve_session(root, name) -> str`（id / 序号 / `latest`；不存在抛 `SessionError`）
  - `rename_session(root, session_id, title) -> None`
  - `archive_session(root, session_id, archived=True) -> None`
  - `title_from_goal(goal) -> str`、`ensure_title(root, session_id, first_goal) -> None`

**设计要点（不要自由发挥）：**

- 标题与归档标记走 `.agent/sessions/<id>/meta.json`，**`memory.db` 表结构一个字不改**。
- 新建时同时 `record_session(...)` 落一行到 `memory.db`，这样新会话立刻出现在列表里、
  也占住名字（`memory.db` 是真实数据，不进它就会出现"建了但看不见"）。
- 列表 = `memory.db` 的行（有最近活跃）+ `meta.json`（有标题、归档标记）。
  两边都以 `safe(session_id)` 归一，`meta.json` 里存原始 id。
- 归档 = 从默认列表隐藏。计划、进度、检查点、记忆都不动，`resolve_session` 仍然认得它。

- [ ] **Step 1: 写失败的测试**

```python
# tests/kernel/test_sessions.py
"""会话：有生命周期，才有"一个工作区多个会话"这件事。"""
from pathlib import Path

import pytest

from spoolkit.kernel import sessions


def _ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    return root.resolve()


def test_新工作区还没有会话(tmp_path: Path) -> None:
    assert sessions.list_sessions(_ws(tmp_path)) == []


def test_建一条就能列出来(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="修 calc.py")
    listed = sessions.list_sessions(root)
    assert [item.id for item in listed] == [made]
    assert listed[0].title == "修 calc.py"


def test_标题从第一句目标截出来(tmp_path: Path) -> None:
    assert sessions.title_from_goal("  ") == ""
    assert sessions.title_from_goal("修好 sum_to") == "修好 sum_to"
    long_goal = "把" + "很长的目标" * 20
    assert len(sessions.title_from_goal(long_goal)) == sessions.TITLE_LIMIT


def test_第一句目标填标题之后不再覆盖(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root)
    sessions.ensure_title(root, made, "第一句")
    sessions.ensure_title(root, made, "第二句")
    assert sessions.read_meta(root, made)["title"] == "第一句"


def test_改名(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="旧")
    sessions.rename_session(root, made, "新")
    assert sessions.list_sessions(root)[0].title == "新"


def test_归档只是从默认列表里藏起来(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="归档我")
    sessions.archive_session(root, made)
    assert sessions.list_sessions(root) == []
    assert [item.id for item in sessions.list_sessions(root, include_archived=True)] == [made]
    assert sessions.resolve_session(root, made) == made


def test_取消归档(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="来回")
    sessions.archive_session(root, made)
    sessions.archive_session(root, made, archived=False)
    assert [item.id for item in sessions.list_sessions(root)] == [made]


def test_按序号和latest解析(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    first = sessions.create_session(root, title="A")
    second = sessions.create_session(root, title="B")
    assert sessions.resolve_session(root, "latest") == second
    assert sessions.resolve_session(root, "1") == second  # 最近活跃在前
    assert sessions.resolve_session(root, "2") == first


def test_不认识的会话名要报错不要偷偷新建(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    first = sessions.create_session(root, title="A")
    with pytest.raises(sessions.SessionError) as raised:
        sessions.resolve_session(root, "查无此会话")
    assert first in str(raised.value)
    assert len(sessions.list_sessions(root)) == 1


def test_记忆库坏了也不炸(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / ".agent" / "memory.db").write_text("这不是 sqlite", encoding="utf-8")
    assert sessions.list_sessions(root) == []
```

- [ ] **Step 2: 跑一次，确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel/test_sessions.py`
Expected: FAIL，`ModuleNotFoundError: No module named 'spoolkit.kernel.sessions'`

- [ ] **Step 3: 写实现**

```python
# src/spoolkit/kernel/sessions.py
"""会话：属于某个工作区的一条活。

为什么要有它：`.agent/sessions/<会话>/`（计划、进度、检查点）与 `memory.db` 的
会话表都已经在了，但会话只是在**启动时**用 `--session` 选一次，还被各入口硬编码了
不同的默认名。于是"一个工作区多个会话"只停留在文件结构上。

状态落在两处，各有理由：

- `memory.db` 的会话表：最近活跃、模型、窗口——真实数据，进库；
- `.agent/sessions/<id>/meta.json`：标题、归档标记——给人看的旁路信息，
  **不动 `memory.db` 的表结构**（那库里装着记忆，不值得为标题冒迁移的险）。

归档 = 从默认列表里隐藏：计划、进度、检查点、记忆都不动，也仍然能直接进去。
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from spoolkit.kernel import jsonstore
from spoolkit.memory.store import init_memory_schema, list_sessions as db_sessions
from spoolkit.memory.store import record_session
from spoolkit.session_state import dir_for, safe
from spoolkit.store.db import open_db

META_NAME = "meta.json"
TITLE_LIMIT = 40


class SessionError(RuntimeError):
    """会话名对不上、或者这里有别的问题让人没法继续。"""


@dataclass(frozen=True)
class Session:
    id: str
    title: str
    model: str
    context_limit: int
    created_at: float
    last_active: float
    archived: bool = False


def meta_path(root: Path | str, session_id: str) -> Path:
    return Path(root) / ".agent" / "sessions" / safe(session_id) / META_NAME


def read_meta(root: Path | str, session_id: str) -> dict:
    payload = jsonstore.read_json(meta_path(root, session_id), {})
    return payload if isinstance(payload, dict) else {}


def write_meta(root: Path | str, session_id: str, **changes) -> dict:
    """合并写：调用方只需要给要改的键。"""

    def mutate(payload: object) -> dict:
        data = dict(payload) if isinstance(payload, dict) else {}
        data.setdefault("id", str(session_id))
        data.update(changes)
        return data

    return jsonstore.update_json(meta_path(root, session_id), mutate, {})


def _legacy_dir(root: Path | str, session_id: str) -> Path:
    """给旧的会话目录写一份 meta 用（老会话没有 meta.json）。"""
    return dir_for(root, session_id)


def _db(root: Path | str):
    """只读打开记忆库；坏了、没有，就返回 None（列表少几行，不炸）。"""
    path = Path(root) / ".agent" / "memory.db"
    if not path.is_file():
        return None
    try:
        conn = open_db(path)
        init_memory_schema(conn)
    except sqlite3.Error:
        return None
    return conn


def list_sessions(root: Path | str, include_archived: bool = False) -> list[Session]:
    """最近活跃在前。`memory.db` 给活跃与模型，`meta.json` 给标题与归档。"""
    conn = _db(root)
    rows = []
    if conn is not None:
        try:
            rows = db_sessions(conn)
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()

    found: list[Session] = []
    seen: set[str] = set()
    for row in rows:
        session_id = str(row["id"])
        seen.add(safe(session_id))
        meta = read_meta(root, session_id)
        archived = bool(meta.get("archived"))
        if archived and not include_archived:
            continue
        found.append(
            Session(
                id=session_id,
                title=str(meta.get("title") or ""),
                model=str(row["model"] or ""),
                context_limit=int(row["context_limit"] or 0),
                created_at=float(row["created_at"] or 0),
                last_active=float(row["last_active"] or 0),
                archived=archived,
            )
        )

    # 只有 meta.json、还没跑过一轮的会话也要看得见（`create_session` 建的就是这种）。
    base = Path(root) / ".agent" / "sessions"
    if base.is_dir():
        for item in sorted(base.iterdir()):
            if not item.is_dir() or item.name in seen or item.name.startswith("."):
                continue
            meta = jsonstore.read_json(item / META_NAME, {})
            if not isinstance(meta, dict):
                continue
            archived = bool(meta.get("archived"))
            if archived and not include_archived:
                continue
            found.append(
                Session(
                    id=str(meta.get("id") or item.name),
                    title=str(meta.get("title") or ""),
                    model="",
                    context_limit=0,
                    created_at=float(meta.get("created_at") or 0),
                    last_active=float(meta.get("last_active") or 0),
                    archived=archived,
                )
            )
    found.sort(key=lambda item: item.last_active, reverse=True)
    return found


def create_session(root: Path | str, title: str = "") -> str:
    """新建一条会话：id 带时间戳（可排序）+ 两位随机（不撞车）。"""
    session_id = time.strftime("%y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    now = time.time()
    write_meta(root, session_id, created_at=now, last_active=now, title=str(title or ""))
    conn = _db(root)
    if conn is not None:
        try:
            record_session(conn, session_id, str(Path(root).resolve()))
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return session_id


def resolve_session(root: Path | str, name: str) -> str:
    """id / 序号（1 起）/ `latest`。**不认识就报错，不新建**。"""
    raw = str(name).strip()
    if not raw:
        raise SessionError("没有给会话名")
    items = list_sessions(root, include_archived=True)
    if raw == "latest":
        if not items:
            raise SessionError("这个工作区还没有任何会话。用 `spool session new` 建一条。")
        return items[0].id
    for item in items:
        if item.id == raw:
            return item.id
    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(items):
            return items[index - 1].id
        raise SessionError(f"只有 {len(items)} 条会话，没有第 {index} 条。")
    listed = "\n".join(f"  {index}) {item.id}  {item.title or ''}" for index, item in enumerate(items, start=1))
    if not items:
        raise SessionError(f"没有叫 {raw} 的会话，这个工作区还没有任何会话。")
    raise SessionError(f"没有叫 {raw} 的会话。现在有：\n{listed}")


def rename_session(root: Path | str, session_id: str, title: str) -> None:
    resolve_session(root, session_id)  # 先在，才给改
    write_meta(root, session_id, title=str(title).strip())


def archive_session(root: Path | str, session_id: str, archived: bool = True) -> None:
    resolve_session(root, session_id)
    write_meta(root, session_id, archived=bool(archived))


def title_from_goal(goal: str) -> str:
    """标题取第一句目标，截断到 TITLE_LIMIT。空白目标给空串。"""
    text = " ".join(str(goal).split())
    return text[:TITLE_LIMIT]


def ensure_title(root: Path | str, session_id: str, first_goal: str) -> None:
    """没标题才填。已有标题不动——人是按标题认这条会话的。"""
    meta = read_meta(root, session_id)
    if str(meta.get("title") or "").strip():
        return
    title = title_from_goal(first_goal)
    if not title:
        return
    write_meta(root, session_id, title=title)
```

- [ ] **Step 4: 跑会话用例，确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel/test_sessions.py`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交**

```bash
git add src/spoolkit/kernel/sessions.py tests/kernel/test_sessions.py
git commit -m "feat(kernel): 会话登记（列表/新建/解析/改名/归档/标题）" -m "为什么：计划、进度、检查点早就按会话隔离了，但会话本身只会在启动时选一次，还被各入口硬编码默认名——一个工作区多个会话这件事只停在文件结构上。" -m "怎么做：复用 memory.db 的会话表与 .agent/sessions/，标题与归档标记写 meta.json（不动表结构）。解析支持 id/序号/latest，不认识的会话名报错而不是新建（偷偷新建会让'我的计划去哪了'变成最难查的问题）。" -m "测试：tests/kernel/test_sessions.py"
```

---

### Task 4: 内核 · 放行（身份级）与旧配对记录认领

**Files:**
- Create: `src/spoolkit/kernel/trust.py`
- Test: `tests/kernel/test_trust.py`

**Interfaces:**
- Consumes: `kernel.jsonstore`、`kernel.workspaces.{state_dir,find_root}`、`spoolkit.bridge.pairing.Pairings`（**只读**，用于认领旧记录）
- Produces:
  - `TRUST = "channel-trust.json"`
  - `Grant(channel, user, roots: tuple[Path, ...], label: str = "", source: str = "manual")`
  - `PendingCode(code, user, channel, root)`
  - `trust_path() -> Path`、`list_grants() -> list[Grant]`
  - `grant(channel, user, roots) -> None`（`roots` 必填且非空，空就抛 `ValueError`）
  - `revoke(channel, user) -> bool`、`is_allowed(channel, user) -> bool`
  - `allowed_roots(channel, user) -> tuple[Path, ...]`、`may_use(channel, user, root) -> bool`
  - `ensure_code(channel, user, root) -> str`、`pending() -> list[PendingCode]`
  - `find_code(code) -> PendingCode | None`（先机器级，再旧文件）、`approve_code(code) -> PendingCode | None`、`forget_code(code) -> bool`
  - `adopt_legacy(root, channel) -> int`（把旧 `.agent/bridge-pairings.json` 的已批准用户
    合成本通道 + 那个工作区的放行，**幂等、不删原文件**）

**设计要点：**

- 一条放行 = **(通道, 用户, 工作区列表)**。通道必须进主键：QQ 的 openid 与 Telegram 的
  user id 不是一套命名空间。批准时**必须点名工作区**（调用方给），默认只给一个。
- 旧文件没有通道维度，所以认领**必须由那条通道自己做**（`adopt_legacy(root, channel)`），
  每条通道各自认领自己工作区里的记录，互不串。
- 旧文件里的待批准码一并读进机器级（key 里带上它请求的工作区与通道）。

- [ ] **Step 1: 写失败的测试**

```python
# tests/kernel/test_trust.py
"""放行：谁、能碰哪几个工作区。换工作区不该再重新配对。"""
from pathlib import Path

import pytest

from spoolkit.bridge.pairing import Pairings
from spoolkit.kernel import trust


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".agent").mkdir(parents=True)
    return root.resolve()


def test_没放行过就是没放行() -> None:
    assert trust.is_allowed("qqbot", "u1") is False
    assert trust.allowed_roots("qqbot", "u1") == ()


def test_放行要给工作区且默认只给一个(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    trust.grant("qqbot", "u1", [root])
    assert trust.is_allowed("qqbot", "u1") is True
    assert trust.may_use("qqbot", "u1", root) is True


def test_放行不能让调用方漏给工作区(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        trust.grant("qqbot", "u1", [])


def test_两个工作区可以分别放行(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    trust.grant("qqbot", "u1", [a])
    trust.grant("qqbot", "u1", [b])
    assert trust.may_use("qqbot", "u1", a) is True
    assert trust.may_use("qqbot", "u1", b) is True


def test_没被放行到的工作区够不到(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    trust.grant("qqbot", "u1", [a])
    assert trust.may_use("qqbot", "u1", b) is False


def test_通道之间不串(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    trust.grant("qqbot", "u1", [a])
    assert trust.is_allowed("telegram", "u1") is False


def test_撤销(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    trust.grant("qqbot", "u1", [a])
    assert trust.revoke("qqbot", "u1") is True
    assert trust.is_allowed("qqbot", "u1") is False
    assert trust.revoke("qqbot", "u1") is False


def test_同一个人的码复用不刷屏(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    first = trust.ensure_code("qqbot", "u1", a)
    assert trust.ensure_code("qqbot", "u1", a) == first


def test_按码批准带上工作区(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    code = trust.ensure_code("qqbot", "u1", a)
    got = trust.approve_code(code)
    assert got is not None and got.root == a and got.user == "u1"
    assert trust.may_use("qqbot", "u1", a) is True
    # 批准之后那个码就没了
    assert trust.find_code(code) is None


def test_丢掉没用的码(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    code = trust.ensure_code("qqbot", "u1", a)
    assert trust.forget_code(code) is True
    assert trust.is_allowed("qqbot", "u1") is False


def test_认领旧配对记录且不删原文件(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    legacy = Pairings(root / ".agent" / "bridge-pairings.json")
    legacy.approve("老用户")
    assert trust.adopt_legacy(root, "qqbot") == 1
    assert trust.may_use("qqbot", "老用户", root) is True
    assert (root / ".agent" / "bridge-pairings.json").is_file()
    # 幂等：再认领一次不会变成两条
    assert trust.adopt_legacy(root, "qqbot") == 1
    assert len(trust.list_grants()) == 1


def test_认领之后旧的待批准码也能用(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    legacy = Pairings(root / ".agent" / "bridge-pairings.json")
    code = legacy.ensure_code("还在等的人")
    trust.adopt_legacy(root, "qqbot")
    got = trust.find_code(code)
    assert got is not None and got.root == root and got.channel == "qqbot"
```

- [ ] **Step 2: 跑一次，确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel/test_trust.py`
Expected: FAIL，`ModuleNotFoundError: No module named 'spoolkit.kernel.trust'`

- [ ] **Step 3: 写实现**

```python
# src/spoolkit/kernel/trust.py
"""放行：谁能驱动、能碰哪几个工作区。

为什么从工作区级搬到身份级：原先放行记录写在 `.agent/bridge-pairings.json`——
那是**工作区里**的文件，于是"信任"和"地点"绑死：换工作区就得重新配对。而人还是
同一个人。

一条放行 = (通道, 用户, 能碰的工作区列表)。通道必须进主键：QQ 的 openid 与
Telegram 的 user id 不是一套命名空间，同名会串。

旧文件不删也不改：`adopt_legacy` 只是把它读进来，什么时候真的不要了由人决定。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from spoolkit.bridge.pairing import Pairings
from spoolkit.kernel import jsonstore
from spoolkit.kernel.workspaces import state_dir

TRUST = "channel-trust.json"
LEGACY_NAME = "bridge-pairings.json"

# 配对码的字母表去掉 0/O、1/I/L：这串码要靠人在聊天里念、在终端里敲。
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6


@dataclass(frozen=True)
class Grant:
    channel: str
    user: str
    roots: tuple[Path, ...]
    label: str = ""
    source: str = "manual"  # manual / legacy


@dataclass(frozen=True)
class PendingCode:
    code: str
    user: str
    channel: str
    root: Path


def trust_path() -> Path:
    return state_dir() / TRUST


def _load() -> dict:
    payload = jsonstore.read_json(trust_path(), {})
    return payload if isinstance(payload, dict) else {}


def _grant_key(channel: str, user: str) -> str:
    # 通道与用户都可能带冒号（openid 见过），所以用不可见分隔符拼一个稳定键。
    return f"{channel}\u0000{user}"


def list_grants() -> list[Grant]:
    """坏条目跳过：放行有问题应该是"够不到"，不是"整个通道起不来"。"""
    data = _load()
    found: list[Grant] = []
    for key, item in (data.get("grants") or {}).items():
        if not isinstance(item, dict):
            continue
        channel, _, user = str(key).partition("\u0000")
        roots = tuple(
            Path(str(text)).resolve()
            for text in (item.get("roots") or [])
            if str(text).strip()
        )
        if not roots:
            continue
        found.append(
            Grant(
                channel=channel,
                user=user,
                roots=roots,
                label=str(item.get("label") or ""),
                source=str(item.get("source") or "manual"),
            )
        )
    return found


def grant(
    channel: str, user: str, roots: list[Path | str] | tuple[Path | str, ...], label: str = ""
) -> None:
    """放行这个人到这几个工作区。`roots` 必须显式给非空值。"""
    channel = str(channel).strip()
    user = str(user).strip()
    picked = tuple(
        dict.fromkeys(Path(str(item)).resolve() for item in roots if str(item).strip())
    )
    if not channel or not user:
        raise ValueError("放行要给通道和用户")
    if not picked:
        raise ValueError(
            "放行必须点名工作区——不给就等于「谁都能碰这台机器上的一切」，"
            "那是这个通道最容易出的事。"
        )

    def mutate(payload: object) -> dict:
        data = dict(payload) if isinstance(payload, dict) else {}
        grants = dict(data.get("grants") or {})
        existing = grants.get(_grant_key(channel, user)) or {}
        previous = [str(item) for item in existing.get("roots") or []]
        merged = list(dict.fromkeys(previous + [str(item) for item in picked]))
        grants[_grant_key(channel, user)] = {
            "channel": channel,
            "user": user,
            "roots": merged,
            "label": label or str(existing.get("label") or ""),
            "source": "manual",
        }
        return {**data, "grants": grants}

    jsonstore.update_json(trust_path(), mutate, {})
    _drop_codes_for(channel, user)


def _drop_codes_for(channel: str, user: str) -> None:
    """批准之后那个码就没用了：留着只会让旧消息永远有效。"""
    def mutate(payload: object) -> dict:
        data = dict(payload) if isinstance(payload, dict) else {}
        codes = {
            code: item
            for code, item in (data.get("codes") or {}).items()
            if not (
                isinstance(item, dict)
                and str(item.get("channel")) == channel
                and str(item.get("user")) == user
            )
        }
        return {**data, "codes": codes}

    jsonstore.update_json(trust_path(), mutate, {})


def revoke(channel: str, user: str) -> bool:
    """撤销放行。返回这次是不是真的撤掉了。"""
    removed = False

    def mutate(payload: object) -> dict:
        nonlocal removed
        data = dict(payload) if isinstance(payload, dict) else {}
        grants = dict(data.get("grants") or {})
        removed = grants.pop(_grant_key(str(channel), str(user)), None) is not None
        return {**data, "grants": grants}

    jsonstore.update_json(trust_path(), mutate, {})
    _drop_codes_for(str(channel), str(user))
    return removed


def is_allowed(channel: str, user: str) -> bool:
    return any(
        item.channel == str(channel) and item.user == str(user) for item in list_grants()
    )


def allowed_roots(channel: str, user: str) -> tuple[Path, ...]:
    for item in list_grants():
        if item.channel == str(channel) and item.user == str(user):
            return item.roots
    return ()


def may_use(channel: str, user: str, root: Path | str) -> bool:
    """这个人被放行到**这个**工作区了吗。没有就够不到，且要说清为什么。"""
    target = Path(root).resolve()
    return target in allowed_roots(channel, user)


def _codes() -> dict[str, dict]:
    data = _load()
    raw = data.get("codes") or {}
    return {
        str(code).upper(): dict(item)
        for code, item in raw.items()
        if isinstance(item, dict)
    }


def pending() -> list[PendingCode]:
    found: list[PendingCode] = []
    for code, item in sorted(_codes().items()):
        root_text = str(item.get("root") or "").strip()
        if not root_text:
            continue
        found.append(
            PendingCode(
                code=code,
                user=str(item.get("user") or ""),
                channel=str(item.get("channel") or ""),
                root=Path(root_text).resolve(),
            )
        )
    return found


def ensure_code(channel: str, user: str, root: Path | str) -> str:
    """给这个人生成一个码（已经有就复用，别刷屏）。"""
    channel, user = str(channel), str(user)
    target = str(Path(root).resolve())
    for item in pending():
        if item.channel == channel and item.user == user and str(item.root) == target:
            return item.code
    code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))

    def mutate(payload: object) -> dict:
        data = dict(payload) if isinstance(payload, dict) else {}
        codes = dict(data.get("codes") or {})
        codes[code] = {"user": user, "channel": channel, "root": target}
        return {**data, "codes": codes}

    jsonstore.update_json(trust_path(), mutate, {})
    return code


def forget_code(code: str) -> bool:
    """丢掉一个还没批准的码（不给理由，也不放行谁）。"""
    dropped = False

    def mutate(payload: object) -> dict:
        nonlocal dropped
        data = dict(payload) if isinstance(payload, dict) else {}
        codes = dict(data.get("codes") or {})
        dropped = codes.pop(str(code).strip().upper(), None) is not None
        return {**data, "codes": codes}

    jsonstore.update_json(trust_path(), mutate, {})
    return dropped


def find_code(code: str) -> PendingCode | None:
    """先找机器级的，再找旧文件里的（认领过之后旧码也能批）。"""
    wanted = str(code).strip().upper()
    for item in pending():
        if item.code == wanted:
            return item
    for item in _legacy_pending():
        if item.code == wanted:
            return item
    return None


def approve_code(code: str) -> PendingCode | None:
    got = find_code(code)
    if got is None:
        return None
    grant(got.channel, got.user, [got.root])
    forget_code(code)
    _approve_in_legacy(got)
    return got


def _legacy_pairings(root: Path) -> Pairings | None:
    path = root / ".agent" / LEGACY_NAME
    if not path.is_file():
        return None
    return Pairings(path)


def _legacy_pending() -> list[PendingCode]:
    """旧文件里的码：没有通道维度，所以只能标成"(旧)"，批准时按调用方给的通道算。"""
    from spoolkit.kernel.workspaces import known

    found: list[PendingCode] = []
    for root in known():
        pairings = _legacy_pairings(root)
        if pairings is None:
            continue
        for code, user in pairings.pending:
            found.append(PendingCode(code=code, user=user, channel="", root=root))
    return found


def _approve_in_legacy(target: PendingCode) -> None:
    """旧文件里也有这个码的话，一并标成已批准（原文件不改结构、也不删）。"""
    pairings = _legacy_pairings(target.root)
    if pairings is None:
        return
    if pairings.approve_code(target.code):
        return
    pairings.approve(target.user)


def adopt_legacy(root: Path | str, channel: str) -> int:
    """把旧配对文件里已批准的人认领成"这条通道 + 这个工作区"的放行。

    由通道自己调用（旧文件没有通道维度，只有当前那条通道知道该认成谁的）。
    幂等；返回现在一共放行了多少条（含以前认领的）。
    """
    pairings = _legacy_pairings(Path(root).resolve())
    if pairings is None:
        return 0
    for user in pairings.approved:
        grant(str(channel), user, [Path(root).resolve()], label="从旧配对记录认领")
    return sum(1 for item in list_grants() if item.channel == str(channel))
```

> 注：`adopt_legacy` 的返回值按"这条通道下现在有多少条放行"算。别写成一个累加
> 计数器——幂等那条用例断言的正是"再认领一次不会变成两条"。

- [ ] **Step 4: 跑放行用例，确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/kernel/test_trust.py`
Expected: PASS（12 passed）

- [ ] **Step 5: 提交**

```bash
git add src/spoolkit/kernel/trust.py tests/kernel/test_trust.py
git commit -m "feat(kernel): 放行升到身份级（通道, 用户, 工作区列表）" -m "为什么：放行原先写在工作区里的 bridge-pairings.json，于是信任和地点绑死——换工作区就得重新配对，而人还是同一个人。这正是'换绑流程模糊'的根。" -m "怎么做：机器级 channel-trust.json，一条放行点名一个或多个工作区，通道进主键（QQ openid 与 TG user id 不是一套命名空间）。旧文件不删不改，由当前通道在启动时 adopt_legacy 认领，幂等；旧的待批准码也能继续批。" -m "测试：tests/kernel/test_trust.py"
```

---

### Task 5: CLI · `spool ws`、会话生命周期、main 与 chat 接入内核

**Files:**
- Create: `src/spoolkit/cli/commands/ws.py`
- Modify: `src/spoolkit/cli/app.py`（注册 `ws`；`main` 用 `resolve_target`；各命令的 `--session` 默认值改成空串）
- Modify: `src/spoolkit/cli/commands/session.py`（扩成 `list|new|use|rename|archive`）
- Modify: `src/spoolkit/cli/commands/chat.py`（开局那一眼 + `/ws` `/ws use` `/new` `/sessions` `/use`）
- Modify: `src/spoolkit/cli/commands/run.py`（`plan` 路径也要用内核解析；`--session` 默认改成空串）
- Test: `tests/cli/test_ws_command.py`、`tests/cli/test_session_command.py`、`tests/cli/test_chat_command.py`、`tests/cli/test_cli_entry.py`

**Interfaces:**
- Consumes: `kernel.workspaces.{list_workspaces,resolve_workspace,forget,set_alias}`、`kernel.sessions.*`、`kernel.pointers.{set as set_pointer}`、`kernel.target.resolve_target`
- Produces: `ws_command(args) -> int`（`args.action ∈ {list,use,add,forget,alias}`，`args.name`、`args.value`）

**⚠ 一个必须改的坑**：`run` / `chat` / `bridge` / `mcp` 的 `--session` 现在带着
默认值（`cli` / `chat` / `bridge` / `mcp`）。只要默认值还在，`args.session` 就永远
是"显式给的"，指针永远轮不到。所以**argparse 的默认值一律改成空串**，把老默认名
当作 `session_fallback` 传给 `resolve_target`；这样老会话接得上，指针也生效。

- [ ] **Step 1: 写失败的测试**

```python
# tests/cli/test_ws_command.py
"""spool ws：列、切、加、忘、起别名。"""
from pathlib import Path

from spoolkit.cli.app import main
from spoolkit.kernel import pointers, workspaces as kws


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def test_列表标出当前(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    pointers.set("cli", root, "cli")
    assert main(["ws"]) == 0
    out = capsys.readouterr().out
    assert str(root) in out
    assert "当前" in out


def test_用名字切会写指针(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    assert main(["ws", "use", "alpha"]) == 0
    got = pointers.get("cli")
    assert got is not None and got.workspace == root


def test_加一个目录会建_agent并登记(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    target.mkdir()
    assert main(["ws", "add", str(target)]) == 0
    assert (target / kws.MARK).is_dir()
    assert target.resolve() in kws.known()


def test_起别名之后能按别名解析(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    assert main(["ws", "alias", "alpha", "我的项目"]) == 0
    assert main(["ws", "use", "我的项目"]) == 0


def test_忘掉不会删目录(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    assert main(["ws", "forget", "alpha"]) == 0
    assert kws.known() == []
    assert (root / kws.MARK).is_dir()


def test_没有叫这个名字的工作区时报错并列出候选(tmp_path: Path, capsys) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    assert main(["ws", "use", "查无此区"]) == 2
    assert str(root) in capsys.readouterr().err
```

```python
# tests/cli/test_session_command.py（追加用例，原有那条保留）
"""会话：建、列、切、改名、归档。"""
from pathlib import Path

from spoolkit.cli.app import main
from spoolkit.kernel import pointers, sessions


def _ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    return root.resolve()


def test_建一条并写指针(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path)
    monkeypatch.chdir(root)
    assert main(["session", "new", "修 calc.py"]) == 0
    got = pointers.get("cli")
    assert got is not None and got.session != ""
    assert sessions.list_sessions(root)[0].title == "修 calc.py"


def test_切到另一条会话(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path)
    monkeypatch.chdir(root)
    first = sessions.create_session(root, title="A")
    second = sessions.create_session(root, title="B")
    assert main(["session", "use", first]) == 0
    assert pointers.get("cli").session == first
    assert main(["session", "use", "latest"]) == 0
    assert pointers.get("cli").session == second


def test_改名与归档(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path)
    monkeypatch.chdir(root)
    made = sessions.create_session(root, title="旧名")
    assert main(["session", "rename", made, "新名"]) == 0
    assert sessions.list_sessions(root)[0].title == "新名"
    assert main(["session", "archive", made]) == 0
    assert sessions.list_sessions(root) == []
```

- [ ] **Step 2: 跑一次，确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/cli/test_ws_command.py tests/cli/test_session_command.py`
Expected: FAIL（`ws` 不是已知命令；`session new` 不存在）

- [ ] **Step 3: 写 `spool ws`**

```python
# src/spoolkit/cli/commands/ws.py
"""`spool ws`：工作区管理。列、切、加、忘、起别名。

为什么要有它：工作区原先只能在启动参数里给（`--root <长路径>`），而且在别处就
只能靠"登记表里只有一个"碰运气。列出来 + 起个短名字之后，切换是一句话的事。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from spoolkit.kernel import pointers, workspaces
from spoolkit.kernel.workspaces import WorkspaceError
from spoolkit.cli.commands.init import init_command

CLIENT = "cli"


def ws_command(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "list") or "list")
    name = str(getattr(args, "name", "") or "").strip()
    try:
        if action == "list":
            return _list()
        if action == "use":
            return _use(name)
        if action == "add":
            # 建工作区这件事只能由人做（`.agent/` 里装着授权策略），
            # 这里直接复用 `spool init`，不另写一套。
            return init_command(argparse.Namespace(path=name or "."))
        if action == "forget":
            root = workspaces.forget(name)
            print(f"已从登记表摘掉：{root}（目录和 .agent/ 都没动）")
            return 0
        if action == "alias":
            alias = str(getattr(args, "value", "") or "").strip()
            root = workspaces.resolve_workspace(name)
            workspaces.set_alias(root, alias)
            print(f"{root} 现在也叫「{alias}」")
            return 0
    except WorkspaceError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"写状态文件失败：{exc}", file=sys.stderr)
        return 2
    print(f"不认识的用法：ws {action}", file=sys.stderr)
    return 2


def _current() -> Path | None:
    pointer = pointers.get(CLIENT)
    if pointer is not None:
        return pointer.workspace
    return workspaces.find_root()


def _list() -> int:
    items = workspaces.list_workspaces()
    if not items:
        print("还没有登记过任何工作区。在这儿敲 `spool ws add` 把当前目录做成工作区。")
        return 0
    here = _current()
    for index, item in enumerate(items, start=1):
        marks = []
        if here is not None and item.root == here:
            marks.append("当前")
        if item.alias:
            marks.append(f"别名 {item.alias}")
        tail = f"  ← {'、'.join(marks)}" if marks else ""
        print(f"  {index}) {item.name}  {item.root}{tail}")
    print("\n用 `spool ws use <名字|序号|路径>` 切；`spool ws alias <名字> <别名>` 起短名。")
    return 0


def _use(name: str) -> int:
    if not name:
        print("要切到哪个？例如 `spool ws use 2` 或 `spool ws use 我的项目`", file=sys.stderr)
        return 2
    root = workspaces.resolve_workspace(name)  # 找不到会抛 WorkspaceError
    session = ""
    existing = pointers.get(CLIENT)
    if existing is not None and existing.workspace == root:
        session = existing.session
    pointers.set(CLIENT, root, session)
    print(f"现在指向：{root}")
    if not session:
        print("（还没选会话；第一句话会开一条新的，或用 `spool session use` 挑一条）")
    return 0
```

- [ ] **Step 4: 注册命令并把 `--session` 默认值改成空串**

```python
# src/spoolkit/cli/app.py（新增）
def _add_ws_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("ws", aliases=["workspace"], help="工作区管理：列 / 切 / 加 / 忘 / 别名")
    parser.add_argument("action", nargs="?", default="list",
                        choices=["list", "use", "add", "forget", "alias"])
    parser.add_argument("name", nargs="?", default="", help="名字、序号、路径或别名")
    parser.add_argument("value", nargs="?", default="", help="`alias` 时给别名")
    parser.set_defaults(func=ws_command)
    # 注意：`ws` 自己**不带** --root，它是用来选工作区的，不是被解析的对象。
```

在 `main()` 里：`_add_ws_command(sub)` 放在 `_add_init_command(sub)` 后面；并改一处：

```python
    if hasattr(args, "root"):
        try:
            args.root = str(workspace.resolve_root(args.root))
        except workspace.WorkspaceError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if hasattr(args, "session"):
        # `--session` 现在留空表示"交给内核定"，老默认名在各自的命令里当兜底。
        args.session = str(getattr(args, "session", "") or "")
```

各命令的 `--session` 默认值改成 `""`，兜底名接上：

| 命令 | `session_fallback` |
|---|---|
| `run` / `plan` | `"cli"` |
| `chat` | `"chat"` |
| `bridge` | `"bridge"` |
| `mcp` | `"mcp"` |

`run` / `chat` / `plan` 里拿会话的地方，统一换成：

```python
    from spoolkit.kernel.target import resolve_target

    target = resolve_target(
        workspace=args.root, session=args.session, client="cli", session_fallback="cli"
    )
    args.session = target.session
```

（`chat` 传 `session_fallback="chat"`，`bridge` 传 `"bridge"`，`mcp` 传 `"mcp"`。）

- [ ] **Step 5: `spool session` 扩成有生命周期**

`src/spoolkit/cli/commands/session.py` 改成 `session_command(args)`：
`args.action ∈ {list,new,use,rename,archive}`，`args.name`、`args.title`。要点：

```python
def session_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    action = str(getattr(args, "action", "list") or "list")
    if action == "new":
        made = sessions.create_session(root, title=str(getattr(args, "title", "") or ""))
        pointers.set("cli", root, made)
        print(f"新会话：{made}" + (f"（{args.title}）" if args.title else ""))
        return 0
    if action == "use":
        picked = sessions.resolve_session(root, str(args.name))
        pointers.set("cli", root, picked)
        print(f"现在用会话：{picked}")
        return 0
    if action == "rename":
        sessions.rename_session(root, str(args.name), str(args.title))
        return 0
    if action == "archive":
        sessions.archive_session(root, str(args.name))
        print("已归档（数据还在，只是默认不列出；`spool session --all` 看得到）")
        return 0
    return _list(root, include_archived=bool(getattr(args, "all", False)))
```

```python
# argparse：位置参数 action + name + title，外加 --all
parser.add_argument("action", nargs="?", default="list",
                    choices=["list", "new", "use", "rename", "archive"])
parser.add_argument("name", nargs="?", default="")
parser.add_argument("title", nargs="?", default="")
parser.add_argument("--all", action="store_true", help="连归档的一起列")
```

`_list` 用 `kernel.sessions.list_sessions` 渲染：`会话 id · 标题 · 模型 · 最近活跃`，
当前那条（指针里的）标出来。表头与旧版保持同形，免得既有用例全红。

- [ ] **Step 6: `spool chat` 开局那一眼与斜杠命令**

`_status_line` 增加会话与来源：

```python
def _status_line(args, project_root, window, policy, scope, target=None) -> str:
    ...
    if target is not None:
        title = sessions.read_meta(project_root, target.session).get("title") or "（还没标题）"
        head = (
            f"工作区：{project_root}（{target.workspace_source}）\n"
            f"会话：{target.session} ｜ {title}（{target.session_source}）\n"
        )
    ...
```

chat 的循环里加四条（都走内核，不碰工作区状态）：

```python
        if text in ("/ws", "/workspace"):
            print(_ws_listing(project_root))
            continue
        if text.startswith("/ws use "):
            print(_ws_switch(text[len("/ws use "):]))
            continue
        if text in ("/sessions", "/session"):
            print(_sessions_listing(project_root))
            continue
        if text.startswith("/use "):
            print(_session_switch(project_root, text[len("/use "):]))
            continue
        if text == "/new" or text.startswith("/new "):
            target = _new_session(args, project_root, text[len("/new"):].strip())
            continue
```

`/new` 与 `/use` 会**改变这次对话接着往哪个会话写**：实现上把新的 `Target` 写进
指针，并更新 `args.session`、重建 `memory`（`open_memory(project_root, window, session)`），
因为 `MemorySession` 的会话 id 是构造时固定的。切换后打印一行"现在用会话 X（标题）"。

- [ ] **Step 7: 跑 CLI 用例**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/cli tests/kernel`
Expected: PASS。会红的旧用例都属于"认默认会话名"那类，按新语义改（默认名改成兜底名，
断言从 `args.session == "chat"` 改成"指针或兜底"）。

- [ ] **Step 8: 提交**

```bash
git add src/spoolkit/cli tests/cli
git commit -m "feat(cli): spool ws 与会话生命周期，main 改用 resolve_target" -m "为什么：工作区原先只能在启动参数里给，在别处只能靠登记表碰运气；会话则只会在启动时选一次。列出来 + 短名字 + 指针之后，切换是一句话的事。" -m "怎么做：新增 spool ws（list/use/add/forget/alias，add 直接复用 init）；spool session 扩成 list/new/use/rename/archive；各命令 --session 默认值改成空串，老默认名降级成 session_fallback（不改的话指针永远轮不到）；chat 加 /ws /ws use /new /sessions /use。" -m "测试：tests/cli/test_ws_command.py、tests/cli/test_session_command.py"
```

---

### Task 6: 聊天通道 · 一条通道服务整台机器

**Files:**
- Modify: `src/spoolkit/bridge/core.py`（准入两步、runner 按客户端键取目标、斜杠命令带目标）
- Modify: `src/spoolkit/bridge/commands.py`（`/ws` `/ws use` `/new` `/sessions` `/use` `/status`）
- Modify: `src/spoolkit/bridge/agent_runner.py`（按 (工作区, 会话) 各复用一个 Runner）
- Modify: `src/spoolkit/cli/commands/bridge.py`（`--root` 变可选；启动时认领旧配对；`spool approve` 支持点名工作区）
- Test: `tests/bridge/test_commands.py`、`tests/bridge/test_core.py`、`tests/bridge/test_agent_runner.py`、`tests/cli/test_approve_command.py`

**Interfaces:**
- Consumes: `kernel.target.resolve_target`、`kernel.trust.*`、`kernel.sessions.*`、`kernel.pointers.*`、`kernel.workspaces.*`
- Produces:
  - `Bridge(channel_obj, runner, *, channel: str, root: Path | None = None, default_session: str = "bridge", trust_mod=trust, ...)`
  - `AgentRunner(resolve, *, default_session="bridge", extra_args=(), timeout=..., clock=..., sleep=..., notify=None)`，`for_client(client_key) -> Callable[[str], str]`
  - `commands.handle(text, target, *, trust_mod=trust, sessions_mod=sessions) -> str | None`

**设计要点（照做，别自由发挥）：**

1. **准入两步**：先 `trust.is_allowed(channel, user)`（不认识 → 给码，走旧的 `access`
   三档语义），再按这条消息解析出的目标 `trust.may_use(channel, user, target.root)`
   ——不在列表里就回一句说明，**不进 agent**。
2. **每聊天一个指针**：`client_key = f"chat:{channel}:{user}"`。所以两个群可以指着
   两个工作区，互不影响。
3. **首次消息开会话**：指针里没有会话时，`sessions.create_session(root, title=
   title_from_goal(text))` 并写指针。这是入口面的显式动作，不是解析器偷偷新建。
4. **runner 协议**：`Bridge` 先看 `runner.for_client` 在不在（仿现有
   `set_notify` 那种"只有 AgentRunner 认这个接口，测试里的假货不受影响"的写法）：
   `call = runner.for_client(key) if callable(getattr(runner, "for_client", None)) else runner`，
   然后 `answer = call(text)`。**假 runner 一行不用改。**
5. **未绑定时谁来认领码**：桥不给 `--root` 时不知道新来的人该碰哪个工作区，所以
   这条消息只在**已经指向某个工作区**时才发码（指向那个工作区）；不然回复
   "先让主人给你指定一个工作区"。`spool approve <码>` 在只有一个工作区时按它批准，
   多个时要求 `spool approve <码> --ws <名字>`——**批准必须点名工作区**。

- [ ] **Step 1: 写失败的测试**

```python
# tests/bridge/test_bridge_targets.py
"""一条通道服务多个工作区：每个聊天各指一个，换绑不用重新配对。"""
from pathlib import Path

from spoolkit.bridge.channel import Incoming
from spoolkit.bridge.core import Bridge
from spoolkit.kernel import pointers, sessions, trust, workspaces as kws


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, conversation: str, text: str) -> None:
        self.sent.append((conversation, text))

    def close(self) -> None:
        pass


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def _bridge(channel_obj, runner, channel="fake") -> Bridge:
    return Bridge(channel_obj, runner, channel=channel)


def test_没被放行的人拿码但进不了_agent(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    kws.register(root)
    pointers.set("chat:fake:u1", root, "cli")
    ran: list[str] = []
    channel = _FakeChannel()
    bridge = _bridge(channel, lambda text: ran.append(text) or "答")
    reply = bridge.handle(Incoming(user="u1", text="改点什么", conversation="c1"))
    assert reply.accepted is False
    assert ran == []
    assert any(pair[0] and "配对码" in pair[1] for pair in channel.sent)


def test_放行之后能干活并且写进那个工作区(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    kws.register(root)
    pointers.set("chat:fake:u1", root, "cli")
    trust.grant("fake", "u1", [root])
    seen: list[str] = []
    bridge = _bridge(_FakeChannel(), lambda text: seen.append(text) or "答")
    reply = bridge.handle(Incoming(user="u1", text="改点什么", conversation="c1"))
    assert reply.accepted is True
    assert seen == ["改点什么"]


def test_两个聊天各指一个工作区(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kws.register(a)
    kws.register(b)
    trust.grant("fake", "u1", [a, b])
    pointers.set("chat:fake:u1", a, "cli")
    pointers.set("chat:fake:u2", b, "cli")
    bridge = _bridge(_FakeChannel(), lambda text: "答")
    assert bridge.target_for("u1").root == a
    assert bridge.target_for("u2").root == b


def test_没被放行到的工作区够不到(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kws.register(a)
    kws.register(b)
    trust.grant("fake", "u1", [a])
    pointers.set("chat:fake:u1", b, "cli")  # 指针指着一个没放行的地方
    ran: list[str] = []
    channel = _FakeChannel()
    bridge = _bridge(channel, lambda text: ran.append(text) or "答")
    reply = bridge.handle(Incoming(user="u1", text="干活", conversation="c1"))
    assert reply.accepted is False
    assert ran == []
    assert "没被放行" in "".join(text for _, text in channel.sent)


def test_第一次说话会给这个聊天开一条会话(tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    kws.register(root)
    trust.grant("fake", "u1", [root])
    pointers.set("chat:fake:u1", root, "")
    bridge = _bridge(_FakeChannel(), lambda text: "答")
    bridge.handle(Incoming(user="u1", text="修好 calc.py 里 sum_to", conversation="c1"))
    made = pointers.get("chat:fake:u1").session
    assert made
    assert sessions.list_sessions(root)[0].title == "修好 calc.py 里 sum_to"


def test_ws_use_切工作区不用重新配对(tmp_path: Path) -> None:
    a = _ws(tmp_path, "a")
    b = _ws(tmp_path, "b")
    kws.register(a)
    kws.register(b)
    trust.grant("fake", "u1", [a, b])
    pointers.set("chat:fake:u1", a, "cli")
    channel = _FakeChannel()
    bridge = _bridge(channel, lambda text: "答")
    bridge.handle(Incoming(user="u1", text="/ws use b", conversation="c1"))
    assert pointers.get("chat:fake:u1").workspace == b
    reply = bridge.handle(Incoming(user="u1", text="干活", conversation="c1"))
    assert reply.accepted is True  # 换工作区没有重新配对这一步
```

- [ ] **Step 2: 跑一次，确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/bridge/test_bridge_targets.py`
Expected: FAIL（`Bridge` 不接受 `channel=`；`target_for` 不存在）

- [ ] **Step 3: 改 `Bridge`**

`core.py` 的改动集中在四处（其余逻辑不动）：

```python
class Bridge:
    def __init__(
        self,
        channel: Channel,
        runner: Callable[[str], str],
        allowed_users=frozenset(),
        max_chars: int = DEFAULT_MAX_CHARS,
        on_note=None,
        pairings: Pairings | None = None,
        access: str = PAIRING,
        root: Path | str | None = None,
        notify_disconnect: bool = False,
        disconnect_cooldown: float = 300.0,
        *,
        channel_name: str = "fake",
        default_session: str = "bridge",
    ) -> None:
        ...
        # `root` 给了 = 这条通道**只**服务那一个工作区（收紧）；不给 = 服务整台机器。
        self.root = Path(root).resolve() if root not in (None, "") else None
        self.channel_name = channel_name
        self.default_session = default_session

    def _client_key(self, user: str) -> str:
        return f"chat:{self.channel_name}:{user}"

    def target_for(self, user: str) -> Target:
        """这条聊天现在指向哪儿。测试与桥都用它。"""
        return resolve_target(
            workspace=self.root,
            client=self._client_key(user),
            session_fallback=self.default_session,
        )

    def _gate(self, user: str, text: str, conversation: str) -> Reply | None:
        """准入第二步：身份过了，还要看他能不能碰**这个**工作区。"""
        target = self.target_for(user)
        if self._trust.may_use(self.channel_name, user, target.root):
            return None
        allowed = "、".join(str(item) for item in self._trust.allowed_roots(self.channel_name, user))
        reply = (
            f"你还没被放行到 {target.root}。"
            + (f"你能碰的是：{allowed}。" if allowed else "现在哪儿都没放行。")
            + "\n`/ws` 看能切的地方。"
        )
        self._send(reply, conversation)
        return Reply(user=user, text=reply, accepted=False, reason="没被放行到这个工作区")
```

`handle()` 的顺序变成：`_admit`（身份，沿用现有三档）→ `_gate`（工作区）→
斜杠命令（带 `target`）→ 给会话补标题（`ensure_title(target.root, target.session,
text)`，会话为空时先 `create_session` 并写指针）→ `runner`。

`commands.handle()` 的签名与内容：

```python
def handle(text: str, target, *, sessions_mod=None, workspaces_mod=None) -> str | None:
    """是命令就返回答复；不是命令返回 None（交给 agent）。

    命令只改"这条通道自己的状态"（指向、会话），**不碰工作区里的内容**：
    授权、关联目录、回滚仍然是人的动作，聊天里不开放。
    """
    if not text.startswith("/"):
        return None
    parts = text.split()
    name = parts[0].lower()
    if name in ("/help", "/?"):
        return HELP
    if name == "/status":
        return _status(target)
    if name == "/ws":
        return _ws_list(target) if len(parts) == 1 else _ws_use(target, parts[1])
    if name == "/sessions":
        return _sessions(target)
    if name == "/use":
        return _use(target, parts[1] if len(parts) > 1 else "")
    if name == "/new":
        return _new(target, " ".join(parts[1:]))
    if name == "/allow-read":
        return workspace_scope_notice(target.root)
    return f"不认识的命令：{parts[0]}（/help 看有哪些）"
```

`HELP` 文案要重写：先说"直接说要做什么"，再列 `/ws` `/ws use <名>` `/new [标题]`
`/sessions` `/use <id|序号|latest>` `/status` `/help`，并保留"回 y 应用、n 丢弃"
那句（它现在真的有效）。

- [ ] **Step 4: 改 `AgentRunner`（按目标复用 Runner）**

```python
class AgentRunner:
    def __init__(self, resolve, *, default_session="bridge", extra_args=(), timeout=900.0,
                 clock=time.monotonic, sleep=time.sleep, notify=None) -> None:
        self._resolve = resolve          # Callable[[str], Target]
        self.default_session = default_session
        ...
        self._runners: dict[str, Runner] = {}
        self._current: str = ""

    def for_client(self, client_key: str) -> Callable[[str], str]:
        """桥按"每条聊天"取一个可调用对象。"""
        return lambda text: self.run(text, client_key)

    def _runner_for(self, client_key: str) -> Runner:
        """一个 (工作区, 会话) 一个 Runner：`start()` 那条"已有运行就拒绝"
        是按实例算的，共用一个实例会让两个聊天互相挡住。"""
        target = self._resolve(client_key)
        key = f"{target.root}\u0000{target.session}"
        runner = self._runners.get(key)
        if runner is None:
            runner = Runner(target.root, session=target.session, extra_args=self.extra_args)
            self._runners[key] = runner
        self._current = key
        return runner
```

`y/n` 确认的回答改成"在**所有** runner 里找一个正在等的"：

```python
    def _awaiting_runner(self) -> Runner | None:
        for key in [self._current, *self._runners]:
            runner = self._runners.get(key)
            if runner is not None and runner.snapshot().get("awaiting"):
                return runner
        return None
```

- [ ] **Step 5: 改 `spool bridge` 与 `spool approve`**

`cli/commands/bridge.py`：

- `--root` 的 `default` 从 `None` 改成 `""`，并在文档串里写清两种语义；
  `bridge_command` 里 `project_root = Path(args.root).resolve() if args.root else None`。
- 启动时认领旧配对：有 `--root` 就认领它，没有就对 `workspaces.known()` 里每一个都认领，
  打一行合计"已从旧配对记录认领 N 条放行"。
- 构造 `Bridge(..., root=project_root, channel_name=args.channel)`；
  `AgentRunner(lambda key: bridge_target(key), ...)` 由 `bridge_command` 拼好
  （`resolve_target(workspace=project_root, client=key, session_fallback="bridge")`）。
- `spool approve`：`approve_code` 之后如果缺工作区（旧文件里的码没有通道与工作区），
  用 `--ws` 指定的那个；没给且登记表里多于一个工作区时**报错并列出候选**，
  要求 `spool approve <码> --ws <名字>`。`--list` 改成同时列机器级与旧文件的待批准码。

`_check()` 里那行"工作区：{project_root}"在没绑定时改成"工作区：整台机器（没给 --root）"，
并列出这条通道现在放行了谁：`spool approve --list` 的内容。

- [ ] **Step 6: 跑通道相关用例**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/bridge tests/cli/test_bridge_check.py tests/cli/test_approve_command.py tests/cli/test_pairing_admin.py`
Expected: PASS。旧的桥用例里凡是用 `Bridge(..., root=...)` 的照旧能用（`root` 还在，
只是变成可选）；用假 runner 的用例**一行都不用改**（`for_client` 那条兜底就是为它写的）。

- [ ] **Step 7: 提交**

```bash
git add src/spoolkit/bridge src/spoolkit/cli/commands/bridge.py tests/bridge tests/cli
git commit -m "feat(bridge): 一条通道服务整台机器，换工作区不再重新配对" -m "为什么：桥原先在启动时钉一个 --root，放行记录又挂在工作区里，于是换绑 = 杀进程 + 重敲长命令 + 重新配对。人还是同一个人，不该重来一遍。" -m "怎么做：--root 变可选（不给=整台机器，给了=只服务那一个，向后兼容）；准入分两步（身份 -> 这个工作区在不在他的放行列表里）；每个聊天一个指针，所以两个群各指一个工作区；首次消息由桥显式开一条会话；AgentRunner 按 (工作区, 会话) 各复用一个 Runner，y/n 找正在等的那个。旧配对文件启动时由当前通道认领，不删不改。" -m "测试：tests/bridge/test_bridge_targets.py"
```

---

### Task 7: MCP · 按调用点名工作区

**Files:**
- Modify: `src/spoolkit/mcp/server.py`（`delegate_task` 加 `workspace` / `session`；新增两个只读工具；按目标各一个 Runner）
- Modify: `src/spoolkit/cli/app.py`（`mcp` 的 `--root` 变可选）
- Test: `tests/mcp/test_server.py`（没有 `tests/mcp/` 就新建；`scripts/mcp_driver.py` 是现成的手动驱动脚本）

**设计要点：**

- `delegate_task` 的 `workspace` / `session` 都是可选的。给了就用（`resolve_target(workspace=..., session=..., client="mcp", session_fallback="mcp")`）；
  没给就用 `mcp` 指针；指针为空而登记表里有多个 → 返回一条**列出候选并要求点名**的错误文本
  （沿用现有 `WorkspaceError` 那种"说不清是哪一个"的形状），不要瞎猜一个。
- 一个 MCP 会话里可以有多个 Runner（换了工作区就是另一个）。`task_status` / `task_events` /
  `confirm_changes` 作用在**最近一次 delegate 的那个** Runner 上（就是现在的语义：
  一个活跃任务），换目标时清掉旧的。
- `list_workspaces` / `list_sessions` 是**只读**工具：给调用方先看有什么，再点名。
  它们不进 agent 的工具表（那是 `tools/registry.py`，本批不动）。
- 工作区仍然只能由人建：MCP 只能选已登记的。工具描述里要写这一句，并说清
  "没有 `workspace` 而登记表里多个时会报错要求点名"。

- [ ] **Step 1: 写失败的测试**

```python
# tests/mcp/test_server.py（新文件；已存在就追加这几条）
"""MCP：按调用点名工作区。"""
from pathlib import Path

from spoolkit.mcp.server import McpServer
from spoolkit.kernel import workspaces as kws


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def _server(root: Path | None) -> McpServer:
    return McpServer(root, runner=None)


def test_列工作区(tmp_path: Path) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    answer = _server(None)._call_tool("list_workspaces", {})
    assert str(root) in answer["content"][0]["text"]


def test_说不清是哪一个就要求点名(tmp_path: Path) -> None:
    first = _ws(tmp_path, "a")
    second = _ws(tmp_path, "b")
    kws.register(first)
    kws.register(second)
    answer = _server(None)._call_tool("delegate_task", {"goal": "干点活"})
    assert answer.get("isError") is True
    assert str(first) in answer["content"][0]["text"]


def test_点名的工作区不存在时报清楚(tmp_path: Path) -> None:
    kws.register(_ws(tmp_path, "a"))
    answer = _server(None)._call_tool(
        "delegate_task", {"goal": "干点活", "workspace": "查无此区"}
    )
    assert answer.get("isError") is True


def test_点名之后就在那个工作区里派活(tmp_path: Path, monkeypatch) -> None:
    root = _ws(tmp_path, "alpha")
    kws.register(root)
    started: list[str] = []
    server = McpServer(None, runner=None)
    monkeypatch.setattr(
        McpServer, "_runner_for", lambda self, target: _Recorder(started, target)
    )
    server._call_tool("delegate_task", {"goal": "干活", "workspace": "alpha"})
    assert started and started[0] == str(root)


class _Recorder:
    """假 Runner：只记下目标，不真起子进程。"""

    def __init__(self, sink: list[str], target) -> None:
        self._sink = sink
        self.session = target.session

    def start(self, goal: str) -> bool:
        self._sink.append(str(_Recorder.last_root))
        return True

    def snapshot(self) -> dict:
        return {"running": True}
```

> 上面 `_Recorder` 里那句 `_Recorder.last_root` 是给实现留的钩子：真正要断言的是
> **派活时用的 root**。实现里把 `_runner_for(target)` 的 `target.root` 记进
> `self._last_target`，测试改成断言 `server._last_target.root == root` 即可——
> 别为了测试把 `Runner` 弄成可注入的全局单例。

- [ ] **Step 2: 跑一次，确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/mcp`
Expected: FAIL（没有 `list_workspaces` 工具）

- [ ] **Step 3: 改 MCP 服务端**

`TOOLS` 增加两项（描述里要写清"只读""没有 `.agent/` 的目录要主人先 `spool ws add`"），
`_call_tool` 的 `handlers` 加上 `list_workspaces` / `list_sessions`；
`_delegate` 改成：

```python
    def _delegate(self, arguments: dict) -> dict:
        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return _text("goal 不能为空", is_error=True)
        try:
            target = resolve_target(
                workspace=arguments.get("workspace") or None,
                session=arguments.get("session") or None,
                client="mcp",
                session_fallback="mcp",
            )
        except WorkspaceError as exc:
            # 说不清是哪一个时**要求点名**，不要挑一个最近的替调用方决定。
            return _text(str(exc), is_error=True)
        runner = self._runner_for(target)
        ...
```

```python
    def _runner_for(self, target) -> Runner:
        """一个 (工作区, 会话) 一个 Runner；换目标就换 Runner，
        并且把"当前任务"指到新的那个（`task_status` 等跟着走）。"""
        key = f"{target.root}\u0000{target.session}"
        runner = self._runners.get(key)
        if runner is None:
            runner = WorkspaceRunner(target.root, session=target.session, mode=self.mode)
            self._runners[key] = runner
        self._active = runner
        self._last_target = target
        return runner
```

`McpServer.__init__` 里 `project_root` 允许为 `None`（表示"还没定"），
`_status` / `_events` / `_confirm` / `_workspace` 一律用 `self._active`；
还没有活跃任务时回一句"还没有任务，先 `delegate_task`（可带 workspace）"。

- [ ] **Step 4: `spool mcp` 的 `--root` 变可选**

`_add_mcp_command` 里 `--root` 说明改成"可选：限定这条 MCP 会话只在它里面干活；
不给则按每次调用给的 workspace 或 `mcp` 指针解析"。`mcp_command` 里
`project_root = Path(args.root).resolve() if args.root else None`。

- [ ] **Step 5: 跑 MCP 与全套**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/mcp tests/cli/test_cli_entry.py`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/spoolkit/mcp/server.py src/spoolkit/cli/app.py tests/mcp
git commit -m "feat(mcp): 按调用点名工作区，新增两个只读工具" -m "为什么：MCP 原先和桥一样，在启动时把工作区钉死；Codex/Cursor 想连着跨两个仓库干活就得起两个 MCP 进程。" -m "怎么做：delegate_task 加可选 workspace/session（没给就用 mcp 指针，指针为空而登记表多个时报错要求点名而不是替调用方决定）；新增 list_workspaces/list_sessions 两个只读工具；一个 (工作区, 会话) 一个 Runner，当前任务跟着最近一次 delegate 走。工作区仍然只能由人建。" -m "测试：tests/mcp/test_server.py"
```

---

### Task 8: 端到端验收 + 文档回写

**Files:**
- Create: `tests/kernel/test_switch_end_to_end.py`
- Modify: `docs/dogfooding.md`、`docs/bridge.md`、`docs/manual.md`、`docs/cli.md`、`docs/mcp.md`、`docs/getting-started.md`、`docs/roadmap.md`、`README.md`

**Interfaces:**
- Consumes: 前面七个任务的全部产出
- Produces: 无新代码（只加验收与文档）

- [ ] **Step 1: 写端到端验收（本批的核心验收）**

```python
# tests/kernel/test_switch_end_to_end.py
"""换工作区不用重新配对——这是用户当初抱怨的那条流程，钉成用例。

用假模型（只回放）与假通道：不起真 llama-server、不发真消息。
"""
from pathlib import Path

from spoolkit.bridge.channel import Incoming
from spoolkit.bridge.core import Bridge
from spoolkit.kernel import pointers, sessions, trust, workspaces as kws


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, conversation: str, text: str) -> None:
        self.sent.append((conversation, text))

    def close(self) -> None:
        pass


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / kws.MARK).mkdir(parents=True)
    return root.resolve()


def test_换工作区不用重新配对且各留各的会话(tmp_path: Path) -> None:
    a = _ws(tmp_path, "项目A")
    b = _ws(tmp_path, "项目B")
    kws.register(a)
    kws.register(b)

    # 主人一次批准：这个人能碰两台（默认只给当时那一个，这里显式给两个）。
    trust.grant("fake", "u1", [a, b])
    pointers.set("chat:fake:u1", a, "")

    ran: list[str] = []
    channel = _FakeChannel()
    bridge = Bridge(channel, lambda text: ran.append(text) or "做完", channel="fake")

    # 在 A 里说一句 → 开一条会话、留下标题
    bridge.handle(Incoming(user="u1", text="在项目A里改点东西", conversation="c1"))
    session_a = pointers.get("chat:fake:u1").session
    assert session_a
    assert sessions.list_sessions(a)[0].title == "在项目A里改点东西"

    # 切到 B：一句话，**没有重新配对**
    bridge.handle(Incoming(user="u1", text="/ws use 项目B", conversation="c1"))
    assert pointers.get("chat:fake:u1").workspace == b
    bridge.handle(Incoming(user="u1", text="在项目B里干活", conversation="c1"))
    session_b = pointers.get("chat:fake:u1").session
    assert session_b and session_b != session_a
    assert sessions.list_sessions(b)[0].title == "在项目B里干活"

    # 切回 A：那条会话还在，能被选回去
    bridge.handle(Incoming(user="u1", text=f"/use {session_a}", conversation="c1"))
    assert pointers.get("chat:fake:u1").session == session_a
    assert [item.id for item in sessions.list_sessions(a)] == [session_a]
    assert sessions.list_sessions(a)[0].title == "在项目A里改点东西"
    assert len(ran) == 2  # 两条命令、两次干活；`/ws use` 与 `/use` 没进 agent
```

- [ ] **Step 2: 跑它，并跑全套**

Run: `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`
Expected: **1394 + 本批新增** passed，仍然只有那 3 条沙箱导致的失败。

- [ ] **Step 3: 回写文档**

按设计文档第 9 节那张表逐条改（**别只改 README**）：

| 文件 | 改什么 |
|---|---|
| `docs/bridge.md` | "换绑工作区"那一节重写：现在是"一条通道服务整台机器，`/ws` 切，不用重新配对"；`--root` 变成可选的收紧手段；列全通道侧斜杠命令 |
| `docs/manual.md` | 工作区那节加"客户端指向"与"放行是身份级"；通道一节把 `--root` 的两种语义写清；会话一节写"标题来自第一句目标、归档只是隐藏" |
| `docs/cli.md` | 加 `spool ws`（含 `use/add/forget/alias`）；`spool session` 的新子命令；`--session` 留空 = 用指针；`spool approve --ws` |
| `docs/mcp.md` | `delegate_task` 的 `workspace` / `session`；`list_workspaces` / `list_sessions`；"工作区只能由人建" |
| `docs/getting-started.md` | 第 4 节补一句：`spool ws add` 一次，以后在哪儿都能按名字用 |
| `docs/roadmap.md` | 第 5 节"运行时切换绑定工作区：故意没做"改成已做，并指向设计文档与本文 |
| `README.md` | 常用命令表加 `spool ws`；"和别的 Agent 协同"那节补一句 MCP 可按次点名工作区 |

- [ ] **Step 4: 在 `docs/dogfooding.md` 补一条**

一条记录写四件事：**做了什么**（上面八个任务一句话）、**量到什么**（新增用例数、
全套通过数、端到端那条证明的流程）、**哪条被证伪**（真跑出来的问题，比如
"`--session` 默认值不改的话指针永远轮不到"这条就是写计划时才发现、施工中该复现的坑）、
**没做什么**（批次二：网页壳换脸）。

- [ ] **Step 5: 提交**

```bash
git add tests/kernel/test_switch_end_to_end.py docs README.md
git commit -m "test+docs: 换工作区不用重新配对的端到端验收，并对齐文档" -m "为什么：这条流程是用户抱怨的原话，必须有用例钉住；文档不改等于没做（换绑那一节现在写的是旧流程）。" -m "怎么做：假模型 + 假通道的端到端（A 里说一句 -> /ws use 切到 B -> 再切回 A，会话与标题都还在）；bridge/manual/cli/mcp/getting-started/roadmap/README 逐条回写。" -m "没做：网页壳换脸（批次二）。"
```

---

## 批次二的入口

批次二（网页壳对话面：工作区与会话选择器、消息成块、模式开关、人专属按钮、
非环回只读）**不在本文**。它消费的接口就是本批产出的 `resolve_target` /
`sessions.list_sessions` / `trust.*`，开工前照设计文档第 5.3 节另写一份计划。

---

## 自查（写计划的人自己做的，不是派给人）

**规格覆盖**：设计文档 §3.1→Task 1/2（文件派、指向）、§3.2/3.3→Task 3、§3.4/3.5/3.6→Task 2、
§3.7→Task 4/6、§3.8→Task 5/7（工作区只能由人建）、§3.9→Task 5/6/7（人专属动作不进模型）、
§3.10→本批不做，无任务（符合预期）、§4→Task 1-4、§5.1→Task 5、§5.2→Task 6、§5.4→Task 7、
§5.3→批次二、§6→Task 4/6（旧配对认领）、§7→Task 1（坏文件）、Task 3（不认识就报错）、
Task 2（指针失效退化）、§8→各任务的测试 + Task 8 端到端、§9→Task 8、§11→无任务（不做）。
**没有漏掉的项。**

**占位符扫描**：全文没有 TBD / TODO / "类似 Task N" / "适当处理错误"这类句子；
每个代码步骤都给了可以直接粘的代码或明确到函数名与参数的改法。

**类型一致性**：`Target(root, session, workspace_source, session_source)` 在
Task 2 定义、Task 6/7 使用同一组字段名；`resolve_target` 的关键字参数
（`workspace` / `session` / `client` / `session_fallback` / `on_ambiguous`）在各任务里一致；
`trust` 的 `grant / revoke / allowed_roots / may_use / ensure_code / approve_code /
find_code / forget_code / adopt_legacy` 在 Task 4 定义、Task 6 使用，名字一致；
`sessions` 的 `create_session / resolve_session / ensure_title / title_from_goal /
list_sessions / read_meta / rename_session / archive_session` 在 Task 3 定义、
Task 5/6/8 使用，名字一致。
