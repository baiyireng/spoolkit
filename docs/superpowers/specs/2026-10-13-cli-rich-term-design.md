# CLI Rich 统一着色层（status / ws / service）—— 设计文档

日期：2026-10-13  
状态：待评审  
范围：第一期仅 `spool status` / `spool ws` / `spool service`

---

## 1. 目标

为 CLI human 输出引入 **Rich**，并封装统一终端处理器，使：

1. 状态色、强调色、muted 说明集中在一处（theme），命令侧只选语义样式；
2. 尊重非 TTY、`NO_COLOR`、管道重定向（无色纯文本）；
3. 不改动现有信息结构（卡片字段、文案、退出码、`--json`）；
4. 为后续 chat / 模型 Markdown 输出预留同一套 `Console` + theme（本期不实现）。

---

## 2. 非目标（一期不做）

- `chat` / 流式模型输出 / Markdown / Syntax 高亮
- 全库替换所有 `print`
- 用 Panel/Table 重做 status 布局（保持现有卡片字符串结构）
- 改 `--json` 契约或探测逻辑

---

## 3. 核心决策

### 3.1 依赖

- 主依赖增加 `rich`（与「后续要美化模型输出」一致；不为可选依赖）。

### 3.2 统一模块 `spoolkit.cli.term`

新建 `src/spoolkit/cli/term.py`，职责：

| 符号 | 作用 |
|------|------|
| `THEME` | Rich `Theme`：语义名 → 样式 |
| `console(*, stderr=False) -> Console` | 按 stdout/stderr 缓存；默认跟随 TTY / `NO_COLOR` |
| `state_style(state: str) -> str` | 状态字 → theme key（如 `state.up`） |
| `print_out(renderable)` / `print_err(renderable)` | 经 Console 输出（替代直接 `print` 的 human 路径） |

Theme 键（固定集合，命令不得硬编码颜色码）：

| key | 用途 |
|-----|------|
| `state.up` | 绿：健康 / running |
| `state.down` | 红：失败 / down |
| `state.slow` | 黄：超时偏慢 |
| `state.unknown` | dim：未知 |
| `state.none` | dim：未配置 |
| `muted` | 标签、页脚提示、次要说明 |
| `accent` | critical 标记 `*`、`current`、标题强调 |
| `ok` / `bad` | 页脚 critical ok / down；service 成功 / 失败句 |

颜色值用 Rich 标准色名（如 `green`、`red`、`yellow`、`cyan`、`dim`），避免真彩色绑死终端。

### 3.3 渲染仍返回可打印物，着色在语义点完成

- `status.render_human` / `services.render_status`：继续拼**行文本**，在状态、标签、关键短语处嵌入 Rich markup（如 `[state.up]up[/]`），或局部使用 `Text`；最终 `join` 成字符串后由 `console().print(..., highlight=False, markup=True)` 输出。
- `ws` 列表：在 `_list` 内直接 `print_out`，对序号 / `current` / 页脚用 markup。
- **禁止**事后对整段纯文本做正则上色。

### 3.4 无色与机器可读路径

| 路径 | 行为 |
|------|------|
| stdout 非 TTY，或环境 `NO_COLOR` 已设 | Rich 输出无 ANSI（测试默认如此） |
| `spool status --json` | 仍 `print(render_json(...))`，无 markup |
| usage / config 错误（exit 2） | stderr 纯文本即可；可用 `print_err` 但不强制上色 |

### 3.5 命令接入点

| 命令 | 改动 |
|------|------|
| `cli/commands/status.py` | human：`print_out(render_human(...))`；json 不变 |
| `status.render_human` | 标题 muted；state 按 `state_*`；critical `*` → accent；字段名 muted；页脚 ok/bad |
| `cli/commands/ws.py` | 列表/提示走 `print_out`；`current` → accent |
| `cli/commands/service.py` | list：`print_out(render_status())`；start/stop 成功→stdout、失败→stderr，message 可带 ok/bad |
| `services.render_status` | recipe 行的 state 用 `state_*`；说明段 muted |

---

## 4. 测试

- 现有 `test_status_command` / `test_ws_command` / `test_service_command`：在非 TTY 下断言**纯文本**仍包含原关键子串（与现在一致）；必要时对输出做 ANSI strip 再断言。
- 新增 `tests/cli/test_term.py`：theme key 存在；`force_terminal=True` 时 `state.up` 输出含 ESC；`NO_COLOR` 或非 TTY 无 ESC。
- 不要求断言具体 RGB / 色号，只断言「有/无 ANSI」与文案。

---

## 5. 文档

- `docs/cli.md`：在 status / ws / service 相关处补一句——human 输出可带颜色；管道与 `NO_COLOR` 无色；`--json` 无色。
- 不扩写叙事。

---

## 6. 后续

- **已推进**：列表/检查类命令走 `print_out`/`print_err`，并上语义色（`config` `env` `session` `policy` `limits` `init` `extra` `diagnose` `mcp-servers` `ws` `status` `service` `shortcuts` 起手行等；`--json` 仍裸 `print`）。
- **二期 2a 已做**：`term.print_md`；`chat` / `run`（非 `--events`）结论文本 TTY 下 Rich Markdown；分隔线/usage/trace muted。派发路径的 implementer/reviewer final 同口径。
- **二期 2b 已做**：`plan` `_say` → `print_err`/`print_md`（stderr，保 `--events` stdout）；`settle` 默认 `out` → `print_out`；`bridge` / `hub` / `bench` human 路径 → `print_out`/`print_err`。
- **二期 2c 已做**：模型等待心跳 → `WaitDisplay`（TTY 上 Rich `Status` 原地刷新；管道/`NO_COLOR` 仍逐行 `print_err` muted）；`WaitingGateway.on_wait_end` 主线程收尾；`print_md` 代码高亮主题抽为 `CODE_THEME=ansi_dark`。
- **交互 Harness（2026-10-18）**：`chat` 绑 `on_event` / `HumanProgress`；截断 note；Web wait 替换与运行中展开；见 `2026-10-18-interactive-harness-design.md`。
- **流式（2026-10-18）**：`delta` 事件 + llamacpp/`FakeModel` `chat_stream`；主循环经 `FinalFieldTap` 只上屏 `final`；CLI Live / Web 气泡增量。见 `2026-10-18-streaming-output-design.md`。
- **仍待做**：`chat` 交互提示行全面迁 `print_out`（可选）

---

## 7. 验收

1. `uv run pytest tests/cli/test_status_command.py tests/cli/test_ws_command.py tests/cli/test_service_command.py tests/cli/test_term.py` 通过  
2. 交互终端下 `spool status` / `spool ws` / `spool service` 状态字有色、标签偏暗  
3. `spool status --json` 与 `spool status | more`（或重定向到文件）无 ANSI  
4. 退出码与 critical 规则不变  
