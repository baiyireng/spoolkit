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
| P0-2 ControlTurn v2 | **已完成**：`TurnProtocol`；开关关闭时仍解析旧 `final` |
| P0-3 FinalTurn | **已完成**：`request_final`；开关关闭时不发第二次请求 |
| P0-4 终稿截断 | **已完成**：续写一次或短答；控制回合仍可 ×2 |
| P0-5 终稿流式 | **已完成**：`final_delta` 原文追加；v2 关闭时仍抽 JSON `final` |
| P0-6 及以后 | **未开始** |

**下一步：P0-6 P0 集成验收**（方案 §11），然后 P1…；每个任务独立提交；不要一次性重写主循环。

---

## 4. 新会话开场指令（可直接粘贴）

```text
请先阅读：
1. docs/long-answer-handoff.md
2. docs/local-agent-long-answer-plan.md
3. docs/integration-notes.md

然后从 P0-6 开始，按方案 §11 的 P0 → P1 → P2 顺序执行。
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
