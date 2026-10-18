# 长回答方案 × 仓库对接笔记（P0-0）

> 方案原文：`docs/local-agent-long-answer-plan.md`（拷自 `D:\workSpace\DSH-qq-work-space\docs\`）  
> 本笔记只做定位，**未改业务代码**。  
> 仓库是 Python（`spoolkit`），方案示例用 TS 命名——落地时按同语义建 Python 模块即可。

---

## 1. 符号定位表

| 主题 | 位置 | 现状一句话 |
|---|---|---|
| 主循环入口 | `src/spoolkit/agent/loop.py:1584` `AgentLoop`；`run` 约 `1982` | 每步装配上下文 → 调模型 → `parse_turn` → 工具或终稿 |
| 主循环模型调用 | `loop.py:2744–2758` | `wait_phase("主循环")` 内 `chat_with_escalation(..., max_tokens=self._budget.output_reserve(), response_schema=self._schema, on_delta=...)` |
| 截断翻倍重试 | `src/spoolkit/llm/retry.py:39` `chat_with_escalation` | 检出 `response.truncated` 后 `max_tokens × 2` 整段重试，默认最多 2 次尝试 |
| 截断 UI note | `loop.py:6899` `_on_truncated_retry` | 清 `_final_tap`，发 `note(kind=truncation_retry)`，置 `_truncation_replay` |
| 流式只抽 final | `loop.py:6917` `_on_model_delta` + `6932` `_delta_for_display` | `FinalFieldTap` 解码 JSON 转义；截断重试时跳过已上屏前缀 |
| Final 抽取器 | `src/spoolkit/llm/stream_tap.py:25` `FinalFieldTap` | 从半截 JSON 抠 `"final"` 字符串并解码 `\n` 等 |
| 输出预算 | `src/spoolkit/context/budget.py:101` `output_reserve`；`105` `boost_output` | `window × output_reserve_ratio`；截断后可抬到 boost 比例 |
| 预算旋钮 | `src/spoolkit/limits.py:307` `output_reserve_ratio=0.15`；`311` `boost=0.30`；`153` `output_boost_min_lines=120` | 8K → ≈1228 token；大文件改写可提前 boost |
| 截断后反馈文案 | `src/spoolkit/context/templates.py:584` `OUTPUT_TRUNCATED` | 要求拆小 / 骨架分轮；`loop.py:2920–2932` 在 `turn.kind=="truncated"` 时注入 |
| 回合 schema | `src/spoolkit/agent/protocol.py:17` `TURN_SCHEMA`；`55` `build_turn_schema` | **含 `final`**；`required: thought, tool_calls, done`；按工具并集约束 arguments |
| 回合解析 | `protocol.py:197` `parse_turn` | `done=true` 时要求非空 `final`；失败返回 `ParseFailure` 不抛异常 |
| Schema 装入循环 | `loop.py:1827` `self._schema = build_turn_schema(registry)` | 整次 run 共用一份动态 schema |
| 请求/响应类型 | `src/spoolkit/llm/types.py:16` `ChatRequest`；`25` `ChatResponse` | `max_tokens` + 可选 `response_schema`；`truncated` 显式字段 |
| llama.cpp 封装 | `src/spoolkit/llm/llamacpp.py:70` `_to_payload`；`102` `LlamaCppGateway`；`140` `chat`；`199` `chat_stream` | OpenAI 兼容 `/v1/chat/completions`；schema → `response_format.json_object`（服务端转 GBNF） |
| 截断判定 | `llamacpp.py:196` / `296` | `finish_reason in ("length", "max_tokens")` → `ChatResponse.truncated` |
| 上下文窗口探测 | `llamacpp.py:307` `context_window` | 读 `/props`；**未暴露** `timings.prompt_n` / `cache_prompt` / `cache_hit` |
| CLI 进度+流式 | `src/spoolkit/cli/events.py:180` `HumanProgress`；`243+` `_AssistantStream` | TTY Live Markdown；截断 note 不关面板；与 `WaitDisplay` 互斥 |
| Wait 心跳 | `src/spoolkit/cli/term.py` `WaitDisplay`；`runtime.py` `bind_model_wait` | stderr Status；流式时 `set_stream_holds_tty` |
| chat 接线 | `src/spoolkit/cli/commands/chat.py:155` `_turn` | `on_event=progress.handle`；终稿再 `print_md(report_text)` |
| 事件协议 | `src/spoolkit/web/protocol.py:27` `DELTA` | 现有 `delta`；**尚无**方案中的 `final_start` / `final_delta` / `section_*` |
| Web 增量 | `src/spoolkit/web/page.py` `appendDelta`（约 1643） | 气泡追加；`final` 事件整段覆盖 |
| 其它 escalation 调用方 | `memory/distill.py`；`agents/plan.py` / `dispatcher.py` / `supervisor.py` | 各自带 schema；**长文改造应只动主循环终稿路径**，勿误伤蒸馏/督导 |

---

## 2. 与方案文档的差异（落地前需对齐）

| 方案假设 | 仓库现状 | 建议 |
|---|---|---|
| `budget.ts` / TS 模块名 | Python `spoolkit` | 已落在 `spoolkit/context/phase_budget.py` 的 `compute_budget`。`n_past` 用 `usage.prompt_tokens`，缺省按字数估算并打日志 |
| Control JSON **无 `final`** | 旧 schema 仍含 `final` | 已加 `build_turn_schema_v2` / `TurnProtocol`。v2 去掉 `final`，保留 `state` / `assessment`（主循环还用）。`PROTOCOL_V2=False` 时仍走旧解析。连续两次旧格式降级并打日志 |
| `answer_plan` / `summary` 字段 | v2 已解析。`should_use_long_answer` 已实现，主循环尚未改道 | 大纲在 `writing_state.py`，逐节在 `sections.py`。主循环未改道。开关关闭时仍是单次终稿 |
| `n_past = timings.prompt_n` | 仅有 `usage.prompt_tokens`（且流式依赖 `stream_options.include_usage`） | P0-1 先用 `prompt_tokens`；缺省再退化估算；`cache_hit` 需查 llama-server 是否返回 |
| `cache_prompt=true` | `ChatRequest.cache_prompt` 默认关；终稿请求为真时写入 payload | 用两次请求的 `prompt_tokens` 增量验收（阈值 200）。服务端是否回 `cache_hit` 仍未接入 |
| 终稿纯文本、无 grammar | `PROTOCOL_V2=False` 时主循环仍带 schema，结论取 `final` | v2 打开且 `done` 时，`request_final` 复用控制前缀、不带 schema、不翻倍重试 |
| 截断后不做 ×2 整段重试 | 控制回合仍走 `chat_with_escalation`（×2） | 终稿 `request_final`：续写一次（预算用 `continuation`，不 ×2）；仍截断则短答 +「继续」入口并打日志。×1.5 整段重写与重叠去重留在 P1-4 |
| 流式事件 `final_delta` 等 | 已登记 `final_delta`；CLI / 网页与 `delta` 一样追加 | 控制回合在 v2 下仍走 `FinalFieldTap`（没有 `final` 就不上屏）。章节事件留到 P1-5 |
| 配置 `protocol.v2` 等 | `limits.py` 无对应项 | P2-3 / 更早用 settings 或 limits 旋钮灰度 |
| 长文子流程 WritingState | `writing_state.py` 管大纲与状态；`sections.py` 逐节生成并组装。主循环未调用 | 节内续写与重叠去重在 P1-4。开关关闭时仍是单次终稿 |

---

## 3. 建议的接入切口（供 P0-1+，本步不实现）

1. **预算**：在 `loop.py` 调 `chat_with_escalation` 前，用 `compute_budget(phase=...)` 替换裸 `output_reserve()`；`v2=false` 时结果应等于今日行为。  
2. **协议**：`build_turn_schema` / `parse_turn` 增加 v2 变体；`AgentTurn.final` 在 v2 控制回合可为空，由后续 FinalTurn 填充。  
3. **二次请求**：`done=true` 后（或 chat 无工具路径）调用 `final_turn(messages)`——**复制**当前 messages 前缀，仅 append 终稿 user 指令；`response_schema=None` + `chat_stream`。  
4. **重试**：`chat_with_escalation` 增加「 escalation 关闭 / 改续写」开关，避免终稿再 ×2。  
5. **流式**：FinalTurn 路径绕过 `FinalFieldTap`，delta 原文上屏。

---

## 4. 风险备忘（已在方案中，对照仓库再强调）

- `loop.py` 体积大（~7k 行）：避免一次性重写 `run`；用旁路函数 + 开关切入。  
- 多处 `chat_with_escalation`：蒸馏/计划/督导仍要旧行为。  
- GBNF 对 `final` 长字符串本身昂贵且难续写——符合方案「拆出 JSON」的判断。  
- 中文 + 表格 Markdown 的 token 密度高；8K 上分节是主路径，不是备选。

---

## 5. P0-0 完成标志

- [x] 主循环 / 预算 / schema / 流式抽取 / 截断重试 / llama 封装均已标「文件:行号 + 一句话」  
- [x] 方案与仓库差异已列出  
- [x] **未改任何业务代码**（仅新增本笔记 + 拷贝方案 md）

P0-1 起的落地状态见 `docs/long-answer-handoff.md` 第 3 节。

---

## 6. P0-6 回归（无新功能）

`PROTOCOL_V2=False`。133 项通过：短答（`test_loop`、`test_chat_command`、`test_loop_prefetch`）、工具轮（`test_loop` 的工具后终稿、`test_loop_events`）、截断（`test_output_truncated`、`test_retry`、`test_final_truncation`、`test_delta_truncation_replay`），加上协议 / 预算 / 终稿流式单测。

| 方案 §13 指标 | 这次能说的 |
|---|---|
| 控制 JSON 合法率 | 旧协议与 v2 单测通过；开关关闭，线上仍是旧协议 |
| 工具正确性 | 假模型工具轮通过，不是真机实时任务 |
| 终稿截断 / 重复生成 | 终稿路径：续写一次或短答；事件只追加。控制回合仍可 ×2 |
| 长文完整率、截断率、首次成功率、时延、缓存命中率 | 未采集。要 8K 真机，且 `cache_hit` 尚未从服务端读出 |
