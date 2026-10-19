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
| `budget.ts` / TS 模块名 | Python `spoolkit` | `compute_budget` 的 `prompt_tokens` 是 P_total（含缓存前缀）。`n_past` 保留，只传它时按 P_total 解释并警告。字数估算仍是 `chars/1.6`，未加变更单里的 5%：现有 `resolve_n_past` 测试锁死了这个数 |
| Control JSON **无 `final`** | 旧 schema 仍含 `final` | 已加 `build_turn_schema_v2` / `TurnProtocol`。v2 去掉 `final`，保留 `state` / `assessment`（主循环还用）。`PROTOCOL_V2=False` 时仍走旧解析。连续两次旧格式降级并打日志 |
| `answer_plan` / `summary` 字段 | v2 已解析。`should_use_long_answer` 已实现，主循环尚未改道 | 大纲在 `writing_state.py`，逐节在 `sections.py`。主循环未改道。开关关闭时仍是单次终稿 |
| `n_past = timings.prompt_n` | 预算已改用 P_total。客户端仍只读到 `usage.prompt_tokens`，没有单独的 `prompt_n` 与复用 token 数 | 拿到 `prompt_n` 与缓存复用数之前，把 `usage.prompt_tokens` 当作 P_total。不要用更小的重算量去放大输出预算 |
| `cache_prompt=true` | `ChatRequest.cache_prompt` 默认关；终稿请求为真时写入 payload | 用两次请求的 `prompt_tokens` 增量验收（阈值 200）。`cache_hit` 只在响应带 `timings.prompt_n` 时推导：小于 `prompt_tokens` 为真，相等为假，缺字段则为空。默认不写记录 |
| 终稿纯文本、无 grammar | `PROTOCOL_V2=False` 时主循环仍带 schema，结论取 `final` | v2 打开且 `done` 时，`request_final` 复用控制前缀、不带 schema、不翻倍重试 |
| 截断后不做 ×2 整段重试 | 控制回合仍走 `chat_with_escalation`（×2） | 终稿与逐节：续写一次。重叠字数读 `continuation_overlap_chars`（默认 200，夹在 200–400）。续写再截断或空续写则短答，不再请求。续写完整但 12 字片段重合超过一半：逐节重写一次（原预算）；终稿按 1.5 倍预算重写一次。变更单写「只带重叠区 + 本节要求」：现有续写末条仍是续写指令，旧正文（含重叠区）不进 prompt，否则会改掉已锁定的末条。拆分该节仍未做 |
| 流式事件 `final_delta` 等 | 已登记 `final_delta`、`section_start`、`section_done`。网页按 `data-section` 只替换这一节 | 主循环尚未发出章节事件。控制回合在 v2 下仍走 `FinalFieldTap` |
| 配置 `protocol.v2` 等 | 旋钮名 `protocol_v2`，默认 `0`。主循环按本轮 `config.overrides` 读取 | 回滚：`.agent/limits.json` 写 `0` 或删键，新开一轮。步骤在 `docs/protocol-v2-runbook.md`。json 里的 `true` 会被忽略 |
| v2 语法与解析 | `summary` 在 v2 语法的 `required` 里，类型是字符串，允许空串。`done=true` 且为空时 `kind=summary_empty`，主循环回灌一次；再空则用不超过 200 字的 `thought` 兜底并标 degraded | 缺键和空串都能过语法之后才出现。默认 `protocol_v2` 仍是 0 |
| 长文子流程 WritingState | `writing_state.py` 管大纲；`sections.py` 逐节生成。v2 且判定为长文时，主循环在证据门之后调用 `compose_long_answer` | 开关关闭时仍是单次终稿。章节生成不另计控制步数 |
| 硬 `required_tools` | 仓库里没有这个门禁 | 实时标签「最新 / 今天 / 价格 / 版本」无引用时退回一次，第二次放行并记 `evidence_gaps`。普通问题和 `answer_plan.mode=long` 不退回；普通问题缺引用只记一笔。不改 `finished` |
| 变更单 §9 回合指标 | `request_metric` 在 `llamacpp.py` 的 chat / chat_stream 写出。`run` 结束调用 `observe_turn` 写 `turn_metric`。没打开 `capture_metrics` 时不写。缓存命中率只算终稿请求；缺样本为未采集。`prefill_ms` 取服务端 `timings.prompt_ms`。流式请求的 `first_token_ms` 是客户端从发出到第一段正文的毫秒 | `非流式 chat 没有首 token 时刻。recovery=short/split 不另发请求。主循环不自动把 JSONL 写进 docs/metrics/` |
| 变更单 §8.1 结构不完整再续写 | 停笔但 ``` 未成对：续写一次，这次不重写。续写再截断或为空仍短答。截断后的重复续写仍重写。节正文在 `\n## ` 处截断 | 没有 `#` 标题的普通停笔仍算写完。若把「必须有标题」也当成未完成，现有「写完了」会多一次请求 |
| 变更单 §8.2 decisions | 节末 `决定：` 行抽进 `WritingState.decisions`。每节最多 3 条、每条 ≤ 40 字、总数 ≤ 20，按空白归一去重 | 正文留在该节，不进约束。下一节的 system 能看见这些结论 |
| 变更单 §8.3 用户停止 | `apply_user_stop`：已提交节保留；当前节标未完成。`run` 把 Ctrl+C 接到 `CancelToken`，关掉正在读的 llama.cpp 连接。长文停在当前节，不再发下一次请求 | 终稿阶段插话仍只排队（`interrupt_final` 为假）。网页壳 `Runner.stop` 在 Windows 上仍是结束子进程，不走这条进程内取消 |
| 变更单 §8.4 跨进程恢复 | `WritingState` 只在当次进程里用 `to_dict` / `from_dict` | 重开会话不读回这份状态，长文从头开始 |

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

`PROTOCOL_V2=False`。回归套件全绿（命令、收集数与通过数见 `docs/p3-1-review.md` §6）：短答（`test_loop`、`test_chat_command`、`test_loop_prefetch`）、工具轮（`test_loop` 的工具后终稿、`test_loop_events`）、截断（`test_output_truncated`、`test_retry`、`test_final_truncation`、`test_delta_truncation_replay`），加上协议 / 预算 / 终稿流式单测。

| 方案 §13 指标 | 这次能说的 |
|---|---|
| 控制 JSON 合法率 | 旧协议与 v2 单测通过；开关关闭，线上仍是旧协议 |
| 工具正确性 | 假模型工具轮通过，不是真机实时任务 |
| 终稿截断 / 重复生成 | 终稿路径：续写一次或短答；事件只追加。控制回合仍可 ×2 |
| 长文完整率、截断率、首次成功率、时延、缓存命中率 | 真机仍未采集。脚本夹具 20/20 标题齐全，见 `docs/metrics/2026-10-18-report.md`。请求记录含命中率列；没有 `prompt_n` 时该格写未采集 |
