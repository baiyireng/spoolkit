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
| `budget.ts` / TS 模块名 | Python `spoolkit` | 新建如 `spoolkit/llm/phase_budget.py`（或 `context/phase_budget.py`）实现 `compute_budget` |
| Control JSON **无 `final`** | `TURN_SCHEMA` / `build_turn_schema` / `parse_turn` **强依赖 `final`** | P0-2 需双协议 + `protocol.v2` 开关；旧路径完整保留 |
| `answer_plan` / `summary` 字段 | 协议中不存在 | P0-2 / P1-1 扩展 schema 与解析校验 |
| `n_past = timings.prompt_n` | 仅有 `usage.prompt_tokens`（且流式依赖 `stream_options.include_usage`） | P0-1 先用 `prompt_tokens`；缺省再退化估算；`cache_hit` 需查 llama-server 是否返回 |
| `cache_prompt=true` | 客户端**未传**该参数 | P0-3 在 payload 增加可选字段；用两次请求的 `prompt_tokens` 增量验收缓存 |
| 终稿纯文本、无 grammar | 主循环**始终**带 `response_schema` | P0-3 新增无 schema 的第二次请求路径 |
| 截断后不做 ×2 整段重试 | `chat_with_escalation` **默认 ×2**，主循环终稿也走这条 | P0-4：终稿通道单独 retries；控制通道可保留短预算重试 |
| 流式事件 `final_delta` 等 | 只有通用 `delta` | P0-5 可先复用 `delta` + `note`；或扩展 `KNOWN`（老 Web 忽略未知类型） |
| 配置 `protocol.v2` 等 | `limits.py` 无对应项 | P2-3 / 更早用 settings 或 limits 旋钮灰度 |
| 长文子流程 WritingState | 不存在 | P1；P0 只做短答纯文本终稿 |

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

**暂停点**：请确认本笔记与差异表无误后，再指示开始 **P0-1 预算计算器**。
