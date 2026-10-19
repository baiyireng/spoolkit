# 长回答改造 · 新会话交接

> 给**新开 Cursor 会话**用。先读本文，再读方案与对接笔记；不要被旧会话里的「显示闪烁 / 字面 `\\n`」带偏——那些已处理，当前主线是协议拆分。

---

## 1. 一句话目标

在 **8K 本地 llama.cpp** 上，把长 Markdown 终稿从受约束 JSON 的 `final` 字段拆出来：控制回合只决策/调工具，终稿走纯文本（可续写、可分节），用 `protocol.v2` 灰度，旧路径可回滚。

---

## 2. 必读文件（按顺序）

| 顺序 | 路径 | 作用 |
|---|---|---|
| ① | `docs/long-answer-handoff.md`（本文） | 现状与边界 |
| ② | `docs/local-agent-long-answer-plan.md` | 完整方案（P0→P2、验收、反模式） |
| ③ | `docs/integration-notes.md` | P0-0 代码定位 + 方案与仓库差异 |

方案原稿也在：`D:\workSpace\DSH-qq-work-space\docs\local-agent-long-answer-plan.md`（已拷进本仓库 docs）。

代码根目录：`D:\workSpace\agents_dev`（包名 `spoolkit`，Python）。

---

## 3. 当前进度

| 项 | 状态 |
|---|---|
| 显示层：JSON `\n` 解码、Wait/Live 抢屏、截断重试整段重刷 | **已修**（`FinalFieldTap` 解码、`set_stream_holds_tty`、Live transient、内核跳过重复前缀） |
| P0-0 代码定位 | **已完成** → `docs/integration-notes.md` |
| P0-1 预算计算器 | **已完成**：`context/phase_budget.py`；`PROTOCOL_V2=False` 时主循环仍用 `output_reserve()` |
| P0-1b 预算口径 | **已完成**：`prompt_tokens` 是 P_total；`n_past` 保留为废弃别名。字数估算未加 5% |
| P0-2 ControlTurn v2 | **已完成**：`TurnProtocol`；开关关闭时仍解析旧 `final` |
| P0-3 FinalTurn | **已完成**：`request_final`；开关关闭时不发第二次请求 |
| P0-4 终稿截断 | **已完成**：续写一次或短答；控制回合仍可 ×2 |
| P0-5 终稿流式 | **已完成**：`final_delta` 原文追加；v2 关闭时仍抽 JSON `final` |
| P0-6 集成验收 | **已完成**：回归套件全绿（命令与数字见第 10 节）。真机指标未采集 → `integration-notes` §6 |
| P1-1 触发 | **已完成**：`should_use_long_answer`。主循环尚未改走长文 |
| P1-2 大纲 | **已完成**：`writing_state.py`。3–7 节；超窗从末尾删，三节仍超则按比例缩小。主循环未接入 |
| P1-3 逐节 | **已完成**：`sections.py`。每节只带大纲、约束和事实；组装校验标题并去重。主循环未接入 |
| P1-4 续写去重 | **已完成**：`continuation.py`。重叠拼接；续写重复则重写该节，终稿按 1.5 倍预算重写一次。续写再截断仍是短答。拆分该节未做 |
| P1-4b overlap 配置 | **已完成**：`continuation_overlap_chars` 默认 200，夹在 200–400。续写 prompt 仍不带旧正文 |
| P1-5 章节事件 | **已完成**：`section_start` / `section_done`。重写只替换这一节的节点。主循环未发这些事件 |
| P1-6 长文回归 | **已完成**：20 条脚本夹具标题齐全。真机比率未采集 → `docs/metrics/2026-10-18-report.md` |
| P2-1 证据门槛 | **已完成**：`evidence_gate.py`。实时问题必须引用工具结果；普通问题可写「无需外部信息」。只在 v2 的 done 分支退回 |
| P2-2 指标埋点 | **已完成**：请求记录在 llama.cpp 封装。回合结束写 `turn_metric`（见第 12 节）。没有 `timings.prompt_n` 时命中率为空 |
| P2-3 灰度与回滚 | **已完成**：旋钮 `protocol_v2` 默认 `0`。回滚步骤见 `docs/protocol-v2-runbook.md`。代码默认未改成打开 |
| 变更单 §8.2 节末结论 | **已完成**：`决定：` 行去重后最多 20 条。正文不进约束 |
| 变更单 §8.3 用户停止 | **已接请求取消**。已提交节保留；当前节未完成。进行中的模型连接在停止时关掉。见第 13 节 |
| 变更单 §8.1 结构检查 | **已完成一部分**：围栏没闭上就续写一次，且这次不重写。节在 `\n## ` 处截断。截断恢复和重复重写未改 |
| 变更单 §8.4 | 已是进程内状态，重开不续写 |
| P3-1 预算接线 | **已改**。见第 10 节。`protocol_v2` 默认仍是 `0` |
| P3-2 终稿二次请求 | **已接**（`764c8f3` 起，主循环 `done` 时调用 `request_final`）。本轮补了循环级验收，见第 11 节 |
| P3-3 长文改道 | **已接**：`answer_plan.mode=long` 时走 `compose_long_answer`。大纲失败改短终稿。`v2=0` 不发章节事件。见第 11 节 |
| P3-4 回合指标 | **已接**：`run` 结束调用 `observe_turn`。未打开采集时不写。命中率只算终稿请求；没有样本写未采集。见第 12 节 |
| P3-5 停止 | **已接**：停止关掉当前 HTTP 读。已提交节保留，当前节未完成，不再发下一次请求。见第 13 节 |
| P3-6 真机 | **收口**。见第 14 节。`protocol_v2` 默认仍是 `0` |
| P3.5 护栏与空节 | **收口**。见第 15 节。护栏和长文主路径不再改。第三次空响应重试不做 |

**下一步：** P3.5 已收口。不要改护栏判据，也不要改长文主路径。`protocol_v2` 默认保持 `0`。第三次空响应重试不做；若以后做，只在 `empty_sections` 超阈值时启用，单独提交，默认关闭。

---

## 4. 新会话开场指令（可直接粘贴）

```text
请先阅读：
1. docs/long-answer-handoff.md
2. docs/local-agent-long-answer-plan.md
3. docs/integration-notes.md

方案任务表已做到 P2-3。围栏未闭合会再续写一次；截断恢复和重复重写不要改。不要把 `protocol_v2` 默认改成打开。
约束：
- 每个任务独立提交；完成前跑对应测试；没有测试先补测试。
- 不要一次性重写 AgentLoop；不要删除旧协议，用 protocol.v2（或等价开关）灰度。
- 仓库是 Python（spoolkit），方案里的 .ts 名称按同语义落成 .py 模块。
- 遇到文档与仓库不一致：先记入 integration-notes 并暂停确认，不要擅自扩大范围。
- 蒸馏 / plan / supervisor 等其它 chat_with_escalation 调用方不要误伤；长文改造只动主循环终稿相关路径。
- 改 HTTP 路由或客户端前先读 code/DEV_API_CANON.md（若涉及）。
```

---

## 5. 架构要点（避免重复踩坑）

**根因**：`thought` + `tool_calls` + 长 `final` 共用一份 `max_tokens`；8K×15%≈1228，长报告必截断；截断只能整段 ×2 重试（schema 不能可靠续写）。

**目标结构**：
- ControlTurn：严格 JSON，**无 `final`**；可有 `answer_plan` / `summary`
- FinalTurn：纯文本二次请求；**前缀与 Control 一致**以吃 `cache_prompt`
- 长文：大纲 → 逐节 → 组装；截断：节内续写 → 重写本节 → 短答+继续（不做无条件 ×2 整段重试）

**仓库关键切口**（细节见 integration-notes）：
- 主循环调用：`src/spoolkit/agent/loop.py` ≈2744–2758 `chat_with_escalation`
- 预算：`context/budget.py` + `limits.py` 的 `output_reserve_ratio`
- 协议：`agent/protocol.py`（仍含 `final`）
- 截断重试：`llm/retry.py`
- 流式抽 final：`llm/stream_tap.py`（v2 终稿应绕过）
- llama 封装：`llm/llamacpp.py`（尚无 `cache_prompt` / `timings.prompt_n`）

---

## 6. 已知差异（落地时必须处理）

1. 方案示例是 TS；实现用 Python。
2. 现协议强依赖 `final`；v2 要双协议 + 开关回滚。
3. 客户端未接 `cache_prompt` / `cache_hit`；`n_past` 可先用 `usage.prompt_tokens`。
4. 主循环始终带 `response_schema`；需新增无 schema 的 FinalTurn。
5. 事件目前只有 `delta`；章节事件可映射或扩展 `KNOWN`。
6. 显示层闪烁/`\n`/重刷 **已修**，P0 不要再当主任务重做，除非回归失败。

---

## 7. 明确不要做（方案 §15 摘要）

- 长 Markdown 继续塞进 JSON `final`
- 终稿截断后无条件 ×2 整段重试
- 对 schema JSON 做 completion 续写
- 一次重写整个 `AgentLoop.run`
- 关掉旧协议且无法回滚
- 误改 distill / supervisor 等非主循环 escalation 路径

---

## 8. 旧会话里已改、可参考的文件

若 P0-5/流式回归需要对照：

- `src/spoolkit/llm/stream_tap.py` — JSON 转义解码
- `src/spoolkit/cli/term.py` — `set_stream_holds_tty` / Wait 与 Live 互斥
- `src/spoolkit/cli/events.py` — truncation_retry 保留面板
- `src/spoolkit/agent/loop.py` — `_delta_for_display` 跳过截断重放前缀
- 测试：`tests/llm/test_stream_tap.py`、`tests/cli/test_human_progress.py`、`tests/cli/test_term.py`、`tests/agent/test_delta_truncation_replay.py`

这些是**显示层止血**，不是长文协议改造本身。

---

## 9. 提交与分支建议

- 在功能分支上做（勿直接堆 main，除非用户明确要求）。
- 提交粒度对齐方案任务 ID（如 `P0-1 budget compute`）。
- 用户未要求时不要 push；不要改 git config。

---

*生成目的：新会话只带方案与切口，不带旧调试噪音。*

---

## 10. P3-1 预算接线（供审查）

| 处 | 改动 |
|---|---|
| 2.1 | `AgentLoop.__init__` 增加 `_last_prompt_tokens: int \| None = None` |
| 2.2 | 主循环控制回合把 `_last_prompt_tokens` 传给 `output_tokens_for_turn`，并传 `safety_ratio=1.05` |
| 2.3 | 响应 `prompt_tokens > 0` 时写回 `_last_prompt_tokens` |
| 2.4 | 三处重建/截断 history 后清空：素材压缩（`history = [Message(...)]`）、硬线重置、软线整理 |
| 2.5 | `chat_with_escalation(..., attempts=1 if v2 else DEFAULT_ATTEMPTS)`。旧协议仍是 2 |
| 溢出 | `ContextOverflowError` 不改 history，不清 `_last_prompt_tokens`。注释与 trace 改为「原样重发」；历史要等下一轮硬线重置才丢 |
| 估算 | `safety_ratio` 默认 `1.0`。只有主循环传 `1.05`。`distill` 未改。`protocol_v2` 默认仍是 `0` |

新增测试（不并进回归数字）：

| 命令 | 收集 | 通过 |
|---|---|---|
| `PYTHONPATH=src python -m pytest tests/context/test_phase_budget.py::test_v2关闭时不估算也不打日志 tests/context/test_phase_budget.py::test_估算余量默认一点零只有显式比例才放大 tests/context/test_phase_budget.py::test_v2首轮按一点零五估算且落在控制上限内 tests/context/test_phase_budget.py::test_v2次轮用上一轮实测收紧且不超过控制上限 tests/agent/test_p3_budget_wire.py -q --basetemp=/d/tmp/spool-p31j/new` | 8（预算 4 + 接线 4） | 8 passed |

接线 4 条：次轮用上一轮实测；素材压缩后入口为 `None` 且 `resets == 0`；硬线重置后入口为 `None`；v2 截断只请求一次，反馈含「拆小」，`max_tokens` ≤ 768 且不翻倍。

回归（`p3-1-review` §6 的文件清单；终稿截断文件实际在 `tests/agent/`，§6 写成了 `tests/llm/test_final_truncation.py`，那个路径不存在）：

| 命令 | 收集 | 通过 |
|---|---|---|
| `PYTHONPATH=src python -m pytest tests/agent/test_loop.py tests/agent/test_loop_events.py tests/agent/test_loop_prefetch.py tests/agent/test_delta_truncation_replay.py tests/cli/test_chat_command.py tests/llm/test_stream_tap.py tests/llm/test_retry.py tests/agent/test_final_truncation.py tests/cli/test_human_progress.py tests/cli/test_term.py -q --basetemp=/d/tmp/spool-p31j/reg` | 72 | 72 passed |

§2.6 只改措辞之后（含 `test_服务端说提示词超长时原样重发`）：

| 命令 | 收集 | 通过 |
|---|---|---|
| `uv run pytest tests/agent/test_loop.py tests/agent/test_run_failure_loud.py -q --basetemp=/d/tmp/spool-p31k/ovf` | 12 | 12 passed |

---

## 11. P3-2 / P3-3（供审查）

| 项 | 改动 |
|---|---|
| P3-2 | 主循环在 `protocol_v2=1` 且 `done` 时已调用 `request_final`：复制控制回合 messages，末尾追加终稿指令，`response_schema=None`，`cache_prompt=True`，`max_tokens` 走 `phase=final`。delta 走 `final_delta`，不经 `FinalFieldTap`。本轮只加 `tests/agent/test_p3_final_request.py` |
| P3-3 | `should_use_long_answer` 为真时调用 `compose_long_answer`（大纲 → `write_sections` → `assemble`），事件经 `_emit` 送出。大纲或组装失败则改 `request_final`。旧协议不进这个分支 |

| 命令 | 收集 | 通过 |
|---|---|---|
| `uv run pytest tests/agent/test_p3_final_request.py tests/agent/test_p3_long_answer.py -q --basetemp=/d/tmp/spool-p33/base` | 5（终稿 2 + 长文 3） | 5 passed |
| `uv run pytest tests/agent/test_loop.py tests/agent/test_loop_events.py tests/agent/test_loop_prefetch.py tests/agent/test_delta_truncation_replay.py tests/cli/test_chat_command.py tests/llm/test_stream_tap.py tests/llm/test_retry.py tests/agent/test_final_truncation.py tests/cli/test_human_progress.py tests/cli/test_term.py tests/agent/test_section_events.py tests/agent/test_outline.py tests/agent/test_p3_budget_wire.py -q --basetemp=/d/tmp/spool-p33/reg` | 86 | 86 passed |

---

## 12. P3-4 回合指标（供审查）

| 项 | 改动 |
|---|---|
| 采集 | `run` 绑一个 `turn_id`，结束时调用 `observe_turn`。`capture_metrics` 没打开则直接返回 |
| 命中率 | 仍是终稿请求里 `cache_hit is True` 的比例。没有终稿样本时字段为 `null`，报告写「未采集」 |
| 分阶段 | `render_phase_report` 按 `phase` 列请求数、截断率、缓存命中率、P50 首 token。首 token 服务端没给，写未采集 |
| JSONL | `write_metrics` / `read_metrics`。主循环不自己往 `docs/metrics/` 落盘 |

| 命令 | 收集 | 通过 |
|---|---|---|
| `uv run pytest tests/agent/test_p3_turn_metric.py tests/agent/test_metrics_report.py tests/agent/test_p3_final_request.py tests/agent/test_loop.py tests/llm/test_llamacpp.py -q --basetemp=/d/tmp/spool-p34/base` | 37 | 37 passed |

其中 `test_p3_turn_metric.py` 2 条：v2 终稿命中率 1.00，且首 token 为未采集；旧协议没有终稿样本时命中率是 `null`，阶段表写「未采集」。

---

## 13. P3-5 停止（供审查）

| 项 | 改动 |
|---|---|
| 连接 | `LlamaCppGateway` 的 `chat` / `chat_stream` 把当前响应登记到 `CancelToken`。`request_stop` 调用 `close()` |
| 信号 | `run` 在主线程把 Ctrl+C 接到这个开关。没在读模型时仍是键盘中断 |
| 长文 | 当前节不提交。已写完的节组装后返回，文末标明第几节未完成。`finished` 为假 |
| 控制回合 | 停止不再当成模型失败，trace 写「用户停止，当前请求已取消」 |

| 命令 | 收集 | 通过 |
|---|---|---|
| `uv run pytest tests/agent/test_p3_stop.py tests/llm/test_llamacpp.py tests/llm/test_llamacpp_stream.py tests/agent/test_p3_long_answer.py tests/agent/test_sections.py -q --basetemp=/d/tmp/spool-p35/base` | 34 | 34 passed |
| `uv run pytest tests/agent/test_loop.py tests/agent/test_user_stop.py tests/agent/test_p3_final_request.py tests/agent/test_p3_turn_metric.py -q --basetemp=/d/tmp/spool-p35/reg` | 14 | 14 passed |

`test_p3_stop.py` 2 条：第三节生成中停止，2 秒内结束，前两节正文还在；挂起的流被 `request_stop` 在 2 秒内关掉。

---

## 14. P3-6 真机（诊断后）

| 项 | 结果 |
|---|---|
| 旋钮 | `resolve("protocol_v2")` 无覆盖是 `0.0`（默认）。仓库没有 `limits.json`。smoke 的 overrides 写成 `1` 时返回 `1.0` |
| 改前 3 条 | 短答 3/3、工具轮完成回合 2/2、长文完成回合 1/1 都是 **缺字段**（没有 `summary` 键）。空串 0，旧格式 0，空回合 0，证据退回 0。原文在 `docs/metrics/2026-10-19-smoke.md` |
| 改动 | v2 语法 `required` 加上 `summary`（允许空串）。`done=true` 且为空：`kind=summary_empty`，回灌一次；再空则用 ≤200 字的 `thought` 兜底并标 degraded。v2 的 system 末尾写明不要 `final` |
| 改后 3 条 | 短答、工具轮都写出 summary，并进入终稿纯文本。长文先出现 1 次空串，重试后有 summary，接着证据门退回 2 次，6 步内没有终稿。原文在 `docs/metrics/2026-10-19-smoke-after.md` |
| 证据门 | 普通问题缺引用改为放行并 `evidence_gaps + 1`。`answer_plan.mode=long` 直接放行。实时问题最多退回 1 次，第二次放行。v2 system 补了「无需外部信息」一句。`max_steps` 提到 12 |
| 门后 3 条 | 证据退回 0。短答、工具轮进入终稿纯文本。长文 summary 写了「无需外部信息」后走了短终稿（没报 `mode=long`，题目短于 1500 token），终稿被截断并续写一次，随后「结论没有落盘」退回，剩下的步数在重复 `read_file`。没有分节。原文在 `docs/metrics/2026-10-19-smoke-gate.md` |
| 触发与落盘 | 强线索不看题目长短，也不被 `mode=short` 否决。用户写「简短 / 一句话 / 简要 / 不用展开」仍走短答。弱线索仍要超过 1500 token。纯问答和长文终稿里的「接下来」不再要求落盘；编码任务仍退回 |
| 分节 smoke | 长文发出了大纲请求。模型把「标题 \| 要点 \| tokens」包进控制 JSON 的 `final`，解析要节数组，日志是「大纲 JSON 需要节数组」，然后改走短终稿并续写一次。没有分节，也没有落盘死循环。原文在 `docs/metrics/2026-10-19-smoke-sections.md` |
| 大纲容错 | 写作层指令不再要控制 JSON。解析会拆开 `final` 和 \`\`\`json 围栏，嵌套超过 2 层失败。大纲仍失败时用固定三节继续，不退回短终稿。原文在 `docs/metrics/2026-10-19-smoke-unwrap.md` |
| 容错后 3 条 | 短答、工具轮进入终稿纯文本，`section_start` 都是 0。长文大纲是 8 行纯文本（不是控制 JSON），超过 7 节，日志「改用固定三节」，`section_start` 为 3。写完后终稿被「谎称改了文件」退回，剩下的步数在重复 `read_file`。没有隔离大纲前缀 |
| 落盘总开关 | 长文和纯问答的终稿不再做落盘校验。文档题（方案、报告、分析、设计、对比、说明、总结、文档）优先于「实现」等编码线索 |
| 落盘后 3 条 | 短答、工具轮 `finished` 为真，`section_start` 为 0，落盘退回 0。长文 `section_start` 为 3，`finished` 为真，落盘退回 0，控制响应 7 条，没有 `read_file` 空转。原文在 `docs/metrics/2026-10-19-smoke-landing.md` |
| 收口 | 不改协议。final / outline 成对 20/20，prefill 降幅 61%，中等收益，双请求保留。section 降幅 32%，命中 cache-on 77/98、去掉首节 77/78，cache-off 为 0；每节 user 尾不同，只能复用稳定前缀。单节中位数 51%，不越界 166/197。下限只做观察。数据在 `metrics/2026-10-19.jsonl`，报告在 `docs/metrics/2026-10-19-report.md`。`protocol_v2` 默认仍是 0 |
| P3-7 | 空内容先重试一次，并记 `empty_response`（finish_reason、prompt_tokens、max_tokens、n_ctx、temperature、是否流式、是否贴近余量）。仍空则该节写「本节没有生成」，后文继续。不阻塞 P3-6 |

| 命令 | 收集 | 通过 |
|---|---|---|
| `uv run pytest tests/agent/test_metrics_report.py tests/llm/test_llamacpp_stream.py tests/agent/test_p3_turn_metric.py -q --basetemp=/d/tmp/spool-p36/unit` | 7 | 7 passed |
| `uv run pytest tests/agent/test_control_schema.py tests/agent/test_metrics_report.py tests/agent/test_p3_final_request.py tests/agent/test_p3_turn_metric.py -q --basetemp=/d/tmp/spool-p36c/unit` | 23 | 23 passed |
| `uv run pytest tests/agent/test_evidence_gate.py tests/agent/test_p3_final_request.py tests/agent/test_p3_long_answer.py tests/agent/test_control_schema.py -q --basetemp=/d/tmp/spool-p36d/gate` | 28 | 28 passed |
| `uv run pytest tests/agent/test_outline.py tests/agent/test_p3_long_answer.py tests/agent/test_p3_stop.py tests/agent/test_long_regression.py -q --basetemp=/d/tmp/spool-p36f/outline` | 22 | 22 passed |
| `uv run pytest tests/agent/test_probe_regression.py tests/agent/test_p3_long_answer.py tests/agent/test_p3_stop.py -q --basetemp=/d/tmp/spool-p36g/land` | 20 | 20 passed |
| `uv run pytest tests/agent/test_plan_trigger.py tests/agent/test_sections.py tests/agent/test_p3_long_answer.py tests/agent/test_probe_regression.py -q --basetemp=/d/tmp/spool-p36h/cue` | 53 | 53 passed |

`protocol_v2` 默认仍是 `0`。没有用 `minLength` 或 `oneOf`。

---

## 15. P3.5 结论

护栏和长文主路径到此为止。旋钮 `protocol_v2` 在 `limits.py` 的默认值是 `0`，`PROTOCOL_V2 = False`。没有推送。

| 项 | 结果 |
|---|---|
| 20 条 | v2 20/20 完成。P50 总时延 35736 ms，截断 5/163。单节 generated/target 中位数 55%，不越界 89/108。`empty_sections` 合计 1，只在题目 4。报告 `docs/metrics/2026-10-19-p35-report.md`，数据 `metrics/2026-10-19-p35.jsonl`。未写入 `metrics/2026-10-19.jsonl`，也未改手改日报 |
| 构造 smoke | `.agent/xxx` 写入前后两次 `list_dir(.)`。只执行了第一次。`kind=useless`，`observed=current=da39a3ee5e6b`，提示含「没有执行」。清单行仍有 `.agent/`。原文在 `docs/metrics/2026-10-19-smoke-p35.md` |
| 空响应重试 | `5c1ac2d`。节请求第一次空内容时追加「必须输出正文，至少 100 字。没有材料时基于目标写，不要留空。」，不原样重发。仍空则该节占位，回合记 `empty_sections`，报告写「内容缺失」。题目 4 有 4 次第一次空；换提示后 `prompt_tokens` 从 259 增到 287，3 节写出正文，1 节仍空 |
| 第 1 节原文 | 改重试前抓的题目 4 第 1 节有正文。message 只有 `content` / `role`。没有 `reasoning` 字段。`finish_reason=stop`，`prompt_tokens=257`，`completion_tokens=62`。附录在同一份 smoke |
| 第三次重试 | 不做。20 条合计 `empty_sections` 为 1，不启用。若以后做：只在 `empty_sections` 超阈值时启用，单独提交，默认关闭 |

| 命令 | 收集 | 通过 |
|---|---|---|
| `uv run pytest tests/llm/test_llamacpp.py tests/llm/test_llamacpp_stream.py tests/agent/test_sections.py tests/agent/test_metrics_report.py -q --basetemp=/d/tmp/spool-p35/close` | 44 | 44 passed |

此前护栏与任务卡：`c08f59d`、`8e8ba38`、`86590ad`、`b1134fc`、`9607867`、`14cfa71`。本轮只有 `5c1ac2d`。
