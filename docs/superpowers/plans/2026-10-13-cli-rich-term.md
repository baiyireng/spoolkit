# CLI Rich 统一着色层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入 `rich`，用 `spoolkit.cli.term` 统一主题与输出，为 `status` / `ws` / `service` 的 human 输出上色，且管道/`NO_COLOR`/`--json` 保持无 ANSI。

**Architecture:** 新建 `cli/term.py`（Theme + 缓存 Console + `print_out`/`print_err`/`state_style`）。渲染函数继续返回带 Rich markup 的字符串；路径与用户文案用 `rich.markup.escape` 后再嵌入。命令入口 human 路径改走 `print_out`/`print_err`；JSON 与 exit 2 纯文本路径不变。

**Tech Stack:** Python 3.12+、`rich`（主依赖）、现有 pytest/`capsys`。

## Global Constraints

- Spec：`docs/superpowers/specs/2026-10-13-cli-rich-term-design.md`
- 一期只改 `status` / `ws` / `service`；不碰 chat / Markdown。
- 不改卡片字段语义、退出码、`--json` 字段名。
- 禁止整段纯文本事后正则上色；禁止命令里写死 `\033[` 或 RGB。
- Theme 键仅：`state.up` `state.down` `state.slow` `state.unknown` `state.none` `muted` `accent` `ok` `bad`。
- 用户可见 CLI 文案保持英文（与近期 status/ws/service 一致）。
- **测试在沙箱内单独跑，git 提交在沙箱外单独执行，两者绝不合并成一条命令。** 未明确要求提交则不要 commit。
- Windows 路径含 `\`；markup 中凡用户/路径字符串必须先 `escape`。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `pyproject.toml` / `uv.lock` | 增加主依赖 `rich` |
| `src/spoolkit/cli/term.py` | Theme、`console`、`state_style`、`print_out`、`print_err`、测试用 `reset_consoles` |
| `src/spoolkit/status.py` | `render_human` / `_title_line` / `_field` / 页脚加 markup |
| `src/spoolkit/cli/commands/status.py` | human → `print_out`；json 仍 `print` |
| `src/spoolkit/cli/commands/ws.py` | `_list` 等 human → `print_out` + markup |
| `src/spoolkit/services.py` | `render_status` 状态与说明 markup |
| `src/spoolkit/cli/commands/service.py` | list → `print_out`；start/stop 消息 → out/err |
| `tests/cli/test_term.py` | theme / ANSI 有无 |
| `tests/cli/test_status_command.py` 等 | 必要时 strip；现有子串断言应仍过 |
| `docs/cli.md` | 一句颜色契约 |

---

### Task 1: 依赖 + `cli.term` + 单测

**Files:**
- Modify: `pyproject.toml`（`dependencies` 增加 `"rich>=13"`）
- Create: `src/spoolkit/cli/term.py`
- Create: `tests/cli/test_term.py`
- Run: `uv lock`（更新 `uv.lock`）

**Interfaces:**
- Consumes: `rich.console.Console`, `rich.theme.Theme`
- Produces:
  - `THEME: Theme`
  - `STATE_STYLE_KEYS: frozenset[str]`（含全部 `state.*`）
  - `console(*, stderr: bool = False) -> Console`
  - `reset_consoles() -> None`（清空缓存，供测试）
  - `state_style(state: str) -> str`（返回 theme key，未知 → `"state.unknown"`）
  - `print_out(renderable: object) -> None`
  - `print_err(renderable: object) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/cli/test_term.py`:

```python
from __future__ import annotations

import io
import os

from rich.console import Console

from spoolkit.cli import term


def test_theme_has_required_keys() -> None:
    required = {
        "state.up",
        "state.down",
        "state.slow",
        "state.unknown",
        "state.none",
        "muted",
        "accent",
        "ok",
        "bad",
    }
    assert required <= set(term.THEME.styles)


def test_state_style_maps_and_falls_back() -> None:
    assert term.state_style("up") == "state.up"
    assert term.state_style("down") == "state.down"
    assert term.state_style("nope") == "state.unknown"


def test_force_terminal_emits_ansi() -> None:
    term.reset_consoles()
    buf = io.StringIO()
    c = Console(file=buf, theme=term.THEME, force_terminal=True, color_system="truecolor")
    c.print(f"[{term.state_style('up')}]up[/]", highlight=False)
    assert "\x1b[" in buf.getvalue()


def test_no_color_env_strips_ansi(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    term.reset_consoles()
    buf = io.StringIO()
    # Fresh console that sees NO_COLOR (rich reads env at construct time).
    c = Console(file=buf, theme=term.THEME, force_terminal=True)
    c.print("[state.up]up[/]", highlight=False)
    assert "\x1b[" not in buf.getvalue()
    assert "up" in buf.getvalue()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cli/test_term.py -v`  
Expected: FAIL（`spoolkit.cli.term` 不存在或无 `THEME`）

- [ ] **Step 3: Add dependency and implement `term.py`**

`pyproject.toml` dependencies:

```toml
dependencies = [
    "httpx>=0.27",
    "rich>=13",
]
```

Then: `uv lock`

Create `src/spoolkit/cli/term.py`:

```python
"""Shared Rich console + semantic theme for CLI human output."""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

THEME = Theme(
    {
        "state.up": "green",
        "state.down": "red",
        "state.slow": "yellow",
        "state.unknown": "dim",
        "state.none": "dim",
        "muted": "dim",
        "accent": "cyan",
        "ok": "green",
        "bad": "red",
    }
)

STATE_STYLE_KEYS = frozenset(
    k for k in THEME.styles if k.startswith("state.")
)

_stdout: Console | None = None
_stderr: Console | None = None


def reset_consoles() -> None:
    global _stdout, _stderr
    _stdout = None
    _stderr = None


def console(*, stderr: bool = False) -> Console:
    global _stdout, _stderr
    if stderr:
        if _stderr is None:
            _stderr = Console(stderr=True, theme=THEME, highlight=False)
        return _stderr
    if _stdout is None:
        _stdout = Console(theme=THEME, highlight=False)
    return _stdout


def state_style(state: str) -> str:
    key = f"state.{state}"
    if key in STATE_STYLE_KEYS:
        return key
    return "state.unknown"


def print_out(renderable: object) -> None:
    console().print(renderable, markup=True, highlight=False)


def print_err(renderable: object) -> None:
    console(stderr=True).print(renderable, markup=True, highlight=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cli/test_term.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**（仅当用户要求提交时）

```bash
git add pyproject.toml uv.lock src/spoolkit/cli/term.py tests/cli/test_term.py
git commit -m "$(cat <<'EOF'
feat(cli): add Rich term theme helper

EOF
)"
```

---

### Task 2: `status` human 着色

**Files:**
- Modify: `src/spoolkit/status.py`（`_title_line`、`_field`、`render_human` 约 813–866 行）
- Modify: `src/spoolkit/cli/commands/status.py`（human 分支约 74–77 行）
- Test: `tests/cli/test_status_command.py`（应无需改断言；若 capsys 偶发含 ANSI，用 strip）

**Interfaces:**
- Consumes: `spoolkit.cli.term.state_style`, `print_out`; `rich.markup.escape`
- Produces: `render_human` 返回含 markup 的 `str`（非 TTY 打印后无色）

- [ ] **Step 1: Write a focused failing check for markup in render_human**

Add to `tests/cli/test_status_command.py`（或新建小测）：

```python
from spoolkit.status import Row, Report, render_human  # use actual class names in file

def test_render_human_embeds_state_markup() -> None:
    # Build a minimal Report/Row the same way other unit tests do if helpers exist;
    # otherwise construct via dataclasses used in status.py (Row / Report).
    row = Row(
        label="local model",
        state="up",
        target="http://127.0.0.1:8080",
        detail="HTTP 200",
        start="spool service start x",
        stop="spool service stop x",
        critical=True,
    )
    text = render_human(
        Report(rows=(row,), elapsed_ms=1, timeout=0.5)  # match real Report fields
    )
    assert "[state.up]" in text
    assert "[accent]" in text or "[accent]*" in text
    assert "[muted]target[/]" in text or "[muted]target" in text
```

Adjust constructor fields to match `status.py` 里真实的 `Row` / `Report`（打开文件核对：`@dataclass` 字段名以源码为准；若叫 `state_label` 等一并填上）。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cli/test_status_command.py::test_render_human_embeds_state_markup -v`  
Expected: FAIL（无 markup）

- [ ] **Step 3: Implement markup in `status.py`**

Import:

```python
from rich.markup import escape
from spoolkit.cli.term import state_style
```

`_title_line`:

```python
def _title_line(row: Row) -> str:
    mark = "[accent]* [/]" if row.critical else "  "
    state = row.state_label or STATE_LABELS.get(row.state, row.state)
    styled_state = f"[{state_style(row.state)}]{escape(state)}[/]"
    name = _pad(mark + escape(row.label), _LABEL_WIDTH)
    # NOTE: _pad/display width must ignore markup; pad the plain label then wrap mark.
```

**宽度陷阱：** `_pad` / `_display_width` 若计入 `[accent]` 会把列撑歪。做法二选一（本任务必须选 **A**）：

**A（推荐）：** 先对**纯文本**算 pad，再把已 pad 的可见名与 markup 标记组装：

```python
def _title_line(row: Row) -> str:
    plain_mark = "* " if row.critical else "  "
    state = row.state_label or STATE_LABELS.get(row.state, row.state)
    plain_name = _pad(plain_mark + row.label, _LABEL_WIDTH)
    # Re-apply mark styling on the leading two chars only:
    if row.critical:
        visible = "[accent]* [/]" + escape(plain_name[2:])
    else:
        visible = escape(plain_name)
    styled_state = f"[{state_style(row.state)}]{escape(state)}[/]"
    return f"{visible}  {styled_state}".rstrip()
```

`_field`：标签用 muted，值 `escape`：

```python
def _field(name: str, value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    label = f"[muted]{name:<7}[/]"
    prefix_plain = f"{_FIELD_INDENT}{name:<7}"
    pieces = _wrap(text, _DETAIL_WIDTH)
    cont = " " * _display_width(prefix_plain)
    lines = [f"{_FIELD_INDENT}{label}{escape(pieces[0])}"]
    lines.extend(cont + escape(piece) for piece in pieces[1:])
    return lines
```

`render_human` 页眉/页脚：

```python
lines = [
    f"[muted]spoolkit status · {report.elapsed_ms}ms · timeout {report.timeout:g}s · loopback[/]",
    "",
]
# ...
if down:
    lines.append(
        "[bad]critical down:[/] " + ", ".join(escape(row.label) for row in down)
    )
else:
    lines.append("[ok]critical ok[/]")
lines.append(
    "[muted]* critical (exit 1 if down): provider · local model (llamacpp) · "
    "[[service]] with command  ·  else informational  ·  exit 0/1/2[/]"
)
```

注意：页脚里的 `[[service]]` 在 Rich markup 中 `[` 有特殊含义——**整段页脚必须 `escape` 后再包一层 muted**，或不用 markup 包含 `[[` 的子串：

```python
footer = (
    "* critical (exit 1 if down): provider · local model (llamacpp) · "
    "[[service]] with command  ·  else informational  ·  exit 0/1/2"
)
lines.append(f"[muted]{escape(footer)}[/]")
```

- [ ] **Step 4: Wire command**

In `cli/commands/status.py`:

```python
from spoolkit.cli.term import print_out

# ...
if getattr(args, "json", False):
    print(status_report.render_json(report))
else:
    print_out(status_report.render_human(report))
```

- [ ] **Step 5: Run status tests**

Run: `uv run pytest tests/cli/test_status_command.py tests/cli/test_term.py -q`  
Expected: PASS。若某断言因 ANSI 失败，在 `_flatten` 前加：

```python
from rich.markup import render as _rich_render  # DO NOT use this
# instead:
import re
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)
```

并在 `_flatten` 入口 `text = _strip_ansi(text)`。capsys 在非 TTY 下通常已无 ANSI。

- [ ] **Step 6: Commit**（仅当用户要求）

```bash
git add src/spoolkit/status.py src/spoolkit/cli/commands/status.py tests/cli/test_status_command.py
git commit -m "$(cat <<'EOF'
feat(cli): colorize spool status human output via Rich

EOF
)"
```

---

### Task 3: `ws` 列表着色

**Files:**
- Modify: `src/spoolkit/cli/commands/ws.py`（`_list` 约 53–73 行）
- Test: `tests/cli/test_ws_command.py`

**Interfaces:**
- Consumes: `print_out`, `escape`
- Produces: 列表输出含 `[accent]current[/]` 等 markup（打印后非 TTY 无色）

- [ ] **Step 1: Extend ws test for current marker markup (optional raw) / keep text asserts**

Ensure existing test still finds `"current"`. Add:

```python
def test_ws_list_marks_current_with_accent_markup(tmp_path, monkeypatch, capsys):
    # reuse existing fixture setup that registers workspaces + sets pointer
    # after `spool ws`, if testing render path via capturing print_out with
    # force_terminal — simpler: call internal by running CLI then:
    from spoolkit.cli import term
    term.reset_consoles()
    # Run list command the same way as test_ws_lists...
    # Assert plain "current" in capsys (non-TTY).
    # Separately unit-check the line builder if extracted.
```

若不想抽函数：在 `_list` 拼行时用 markup，现有 `assert "current" in out` 在非 TTY 下仍成立（Rich 剥掉标签后留下 `current`）。**不必**断言 ANSI。

- [ ] **Step 2: Implement `_list` markup**

```python
from rich.markup import escape
from spoolkit.cli.term import print_out

def _list() -> int:
    items = workspaces.list_workspaces()
    if not items:
        print_out("No workspaces registered.")
        print_out("  [muted]spool ws add .[/]")
        return 0
    here = _current()
    name_width = max(len(item.name) for item in items)
    print_out("[muted]workspaces[/]")
    print_out("")
    for index, item in enumerate(items, start=1):
        marks: list[str] = []
        if here is not None and item.root == here:
            marks.append("[accent]current[/]")
        if item.alias:
            marks.append(f"alias:{escape(item.alias)}")
        tail = f"  ({', '.join(marks)})" if marks else ""
        print_out(
            f"  {index:>2}  {escape(item.name):<{name_width}}  "
            f"{escape(str(item.root))}{tail}"
        )
    print_out("")
    print_out("[muted]  spool ws use <name|index|path>[/]")
    print_out("[muted]  spool ws alias <name> <alias>[/]")
    return 0
```

**注意：** `f"{escape(item.name):<{name_width}}"` 在 escape 后长度可能变化（若名含 `[`）。工作区名几乎不含 `[`；若要绝对稳妥，先 pad 纯文本再 escape 整段字段：

```python
name_col = f"{item.name:<{name_width}}"
print_out(f"  {index:>2}  {escape(name_col)}  {escape(str(item.root))}{tail}")
```

其它 `print`（`use`/`forget`/`alias` 成功句）本任务可改为 `print_out`，错误仍 `print(..., file=sys.stderr)`。

- [ ] **Step 3: Run ws tests**

Run: `uv run pytest tests/cli/test_ws_command.py -q`  
Expected: PASS

- [ ] **Step 4: Commit**（仅当用户要求）

```bash
git add src/spoolkit/cli/commands/ws.py tests/cli/test_ws_command.py
git commit -m "$(cat <<'EOF'
feat(cli): colorize spool ws list via Rich

EOF
)"
```

---

### Task 4: `service` list / start-stop 消息着色

**Files:**
- Modify: `src/spoolkit/services.py`（`render_status` 约 608–648 行；`STATE_LABELS` 附近）
- Modify: `src/spoolkit/cli/commands/service.py`
- Test: `tests/cli/test_service_command.py`（`render_status()` 直接断言子串；markup 标签不影响 `"down" in line`）

**Interfaces:**
- Consumes: `state_style`, `escape`, `print_out`, `print_err`
- Produces: `render_status() -> str` 含 state markup

- [ ] **Step 1: Add markup assertion on render_status**

```python
def test_render_status_embeds_state_markup(tmp_path, monkeypatch):
    # minimal config with one down recipe (closed port) — mirror test_list_三种状态分得清 setup
    out = render_status()
    assert "[state." in out
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/cli/test_service_command.py::test_render_status_embeds_state_markup -v`  
Expected: FAIL

- [ ] **Step 3: Implement `render_status` markup**

```python
from rich.markup import escape
from spoolkit.cli.term import state_style

def render_status() -> str:
    path = settings.config_path()
    head = f"recipes: {path}" + ("" if path.is_file() else " (missing)")
    lines = [f"[muted]{escape(head)}[/]", ""]
    items = settings.load_services()
    if not items:
        for para in (
            "No [[service]] recipes. Without them, tunnels are not probed or started;",
            "the model is still probed via provider / base_url.",
            # ... existing help lines ...
        ):
            lines.append(f"[muted]{escape(para)}[/]")
        # keep example TOML blocks escaped inside muted or as plain escape-only lines
        return "\n".join(lines)
    lines.append(escape(f"{len(items)} recipe(s):"))
    for item in items:
        state = inspect_service(item, cached=False)
        how = "probe" if not item.command else "start"
        where = state.target or "(no health)"
        label = STATE_LABELS[state.state]
        styled = f"[{state_style(state.state)}]{escape(label)}[/]"
        lines.append(
            f"  - {escape(item.name)} [{how}] {styled}  "
            f"{escape(where)}  {escape(state.detail)}"
        )
    lines.append("")
    lines.append(
        "[muted]"
        + escape("start: spool service start <name>    stop: spool service stop <name>")
        + "[/]"
    )
    return "\n".join(lines)
```

空配方帮助文案里的 `[[service]]`：**整行 `escape` 后再包 `[muted]...[/]`**，与 status 页脚同一规则。

- [ ] **Step 4: Wire `service_command`**

```python
from spoolkit.cli.term import print_err, print_out

# list:
print_out(render_status())

# start/stop loop:
for item in names:
    code, message = fn(item)
    if code:
        print_err(f"[bad]{escape(message)}[/]" if "[" not in message else f"[bad]{escape(message)}[/]")
    else:
        print_out(f"[ok]{escape(message)}[/]")
```

简化为统一：

```python
from rich.markup import escape
from spoolkit.cli.term import print_err, print_out

styled = f"[{'bad' if code else 'ok'}]{escape(message)}[/]"
(print_err if code else print_out)(styled)
```

usage/config 错误保持：

```python
print(problem, file=sys.stderr)
```

- [ ] **Step 5: Run service + term tests**

Run: `uv run pytest tests/cli/test_service_command.py tests/cli/test_term.py -q`  
Expected: PASS

- [ ] **Step 6: Commit**（仅当用户要求）

```bash
git add src/spoolkit/services.py src/spoolkit/cli/commands/service.py tests/cli/test_service_command.py
git commit -m "$(cat <<'EOF'
feat(cli): colorize spool service list and start/stop messages

EOF
)"
```

---

### Task 5: `docs/cli.md` + 验收

**Files:**
- Modify: `docs/cli.md`（`## spool ws` / `## spool service` / `## spool status` 各补一句）

- [ ] **Step 1: Document color contract**

在 `## spool ws` 表后或段末加：

```markdown
Human list may use color in a TTY; pipes and `NO_COLOR` stay plain.
```

在 `## spool service` 段加同口径一句。

在 `## spool status` 段（卡片说明旁）加：

```markdown
Human cards may use color in a TTY; `--json`, pipes, and `NO_COLOR` stay plain.
```

- [ ] **Step 2: Full acceptance pytest**

Run:

```bash
uv run pytest -q \
  tests/cli/test_term.py \
  tests/cli/test_status_command.py \
  tests/cli/test_ws_command.py \
  tests/cli/test_service_command.py
```

Expected: all PASS

- [ ] **Step 3: Manual smoke（交互终端，非 CI）**

```bash
uv run spool status
uv run spool ws
uv run spool service
uv run spool status --json
uv run spool status > /tmp/status.out && head /tmp/status.out
```

Check: TTY 有色；json/重定向无 `\x1b`；exit code 规则不变。

- [ ] **Step 4: Commit**（仅当用户要求）

```bash
git add docs/cli.md
git commit -m "$(cat <<'EOF'
docs(cli): note TTY color for status/ws/service

EOF
)"
```

---

## Spec coverage（自检）

| Spec 要求 | Task |
|-----------|------|
| 主依赖 `rich` | 1 |
| `cli/term.py` Theme + console + state_style + print_out/err | 1 |
| status 着色 + print_out；json 纯文本 | 2 |
| ws 列表 accent/muted | 3 |
| service render_status + start/stop ok/bad | 4 |
| 测试 theme / ANSI / 现有命令测 | 1–4 |
| docs/cli.md 一句 | 5 |
| 非目标 chat/Panel 重做 | 未列入任务 |

## Placeholder scan

无 TBD；`[[service]]` markup 陷阱已在 Task 2/4 写明；`Row`/`Report` 构造以源码字段名为准（Task 2 Step 1 要求打开 `status.py` 核对）。
