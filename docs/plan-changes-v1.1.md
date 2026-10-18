# 方案变更单 v1.1：P1-4 之后的安全改道

> **状态**：生效中。主文档 `docs/local-agent-long-answer-plan.md` 冻结为 v1.0，正文不再改动；本单是唯一增量来源。
> **适用**：Cursor 已实施到 P1-4（节内续写与去重），后续按本单执行，不重读、不重排主文档。

---

## 1. 变更摘要（只列 delta）

| # | 变更 | 类型 | 影响已完成代码 |
|---|---|---|---|
| 1 | 预算容量口径从 `prompt_n` 改为 `P_total`（完整渲染 prompt，含缓存前缀） | **语义修正** | 是（P0-1 需回填） |
| 2 | 续写 overlap 从固定 200 改为可配置 200–400 | 参数化 | 轻微（P1-4 需微调） |
| 3 | 新增「提交边界 vs 恢复边界」说明 | 文档性 | 否 |
| 4 | 新增四组基准实验与指标修正 | 文档性 | 否（P2 执行） |

**不变**：协议 v2、`answer_plan`、分节流程、恢复状态机、P0-2～P0-6、P1-1～P1-3 的设计与验收。

---

## 2. 变更 1：预算口径

### 原文档（v1.0）

```ts
interface BudgetInput {
  nCtx: number;
  nPast: number;   // 曾按 prompt_n 解释
  ...
}
const oMax = nCtx - nPast - margin;
```

### 新口径

```ts
interface BudgetInput {
  nCtx: number;
  promptTokens: number; // P_total：完整渲染 prompt 的 token 总数（含缓存前缀）
  /** @deprecated 兼容别名；传入时按 P_total 解释，并打 warning */
  nPast?: number;
  ...
}
const total = promptTokens ?? nPast ?? 0;
const oMax = nCtx - total - margin;
```

**为什么**：缓存命中时 `prompt_n`（本次重算量）小于 `P_total`（上下文占用）。用 `prompt_n` 算预算会高估可用空间，导致越界或触发 context shift。

**兼容要求**：

- 不重命名已有公开字段；`nPast` 保留为 deprecated 别名，避免破坏调用方。
- 调用点优先传 `promptTokens`；暂时只传 `nPast` 的路径行为不变（按 P_total 解释），但日志打一次 warning。
- `P_total` 取数：客户端 tokenizer 渲染后计数；或服务端 `prompt_n + 复用 token 数`；两者都拿不到时用估算值 + 5% 余量。

---

## 3. 变更 2：续写 overlap

- 原：`mergeContinuation(head, tail, overlap = 200)` 固定 200。
- 新：`continuation.overlapChars` 配置项，默认 `200`，允许 `200–400`；接口签名不变。
- 约束：只带 overlap 区 + 本节要求，**禁止把全量旧正文放进续写 prompt**。
- 失败降级不变：续写重复/失败 → 重写本节 → 拆小节。

---

## 4. 回填任务（插在 P1-5 之前，各一个提交）

### P0-1b：预算口径回填（30–60 分钟）

- **改动**：`BudgetInput` 增加 `promptTokens`，保留 `nPast` 别名；内部统一按 `P_total` 计算；调用点逐步切换。
- **测试**（新增 3 条）：
  1. `prompt_n < P_total` 时，`maxTokens ≤ n_ctx - P_total - margin`（旧实现会偏大）。
  2. `P_total = n_ctx - 100` → `maxTokens = 0`、`reason = context-full`。
  3. `prompt_n == P_total` 时，新旧实现结果一致。
- **验收**：旧预算测试全绿 + 新 3 条通过；日志出现 deprecated warning 时不报错。

### P1-4b：overlap 配置化（30 分钟）

- **改动**：把硬编码 `200` 改为读 `continuation.overlapChars`（默认 200，clamp 200–400）；只带 overlap 区。
- **测试**：`overlap=200/400`、续写重复触发重写本节、空续写降级短答。
- **验收**：P1-4 已有用例全绿；新增 3 条边界通过。

---

## 5. 影响矩阵（已完成部分怎么处理）

| 任务 | 状态 | 是否受影响 | 动作 |
|---|---|---|---|
| P0-0 代码定位 | 已完成 | 否 | — |
| P0-1 预算计算器 | 已完成 | **是（语义）** | 做 P0-1b；不改公开接口名 |
| P0-2～P0-6 | 已完成 | 基本否 | 检查预算调用点是否传 `nPast`；是则切 `promptTokens` |
| P1-1～P1-3 | 已完成 | 否 | — |
| P1-4 节内续写 | 已实施/进行中 | 轻微 | 完成后做 P1-4b；接口不变 |
| P1-5、P1-6 | 未开始 | 是（新口径） | 按本单执行 |
| P2-x | 未开始 | 是 | 按本单 + 主文档执行 |

**结论**：不需要回退任何已完成任务；只加两个小补丁提交。

---

## 6. 给 Cursor 的切换指令（可直接粘贴）

```text
方案主文档已冻结为 v1.0，请先读 docs/plan-changes-v1.1.md，再继续。

1. 不要重读、重排主文档；只按本变更单的 delta 执行。
2. 先做 P0-1b、P1-4b 两个回填任务，各一个独立提交，再继续 P1-5。
3. 不重命名已有公开字段：保留 nPast 作为 deprecated 别名，内部切换 promptTokens。
4. 不改已完成的协议、分节与恢复逻辑；遇到与主文档不一致，先报告差异再改。
5. 每步跑对应测试；P0-1b 必须包含 prompt_n < P_total 的用例。
```

---

## 7. 回滚

- 任一补丁失败：`git revert` 对应提交；主文档 v1.0 与新代码保持可用。
- 若预算口径切换引发问题：临时以 `nPast` 传入 `P_total` 值（行为等价），不阻塞 P1-5。
- 记录：在 PR 描述里注明「v1.1 delta，P0-1b/P1-4b」。

---

## 8. 交付前默认决定（4 条，已拍板）

| # | 决定 | 理由 |
|---|---|---|
| 1 | 终稿不引入自定义结束标记；单次终稿以 `finish_reason=stop` + 结构校验（标题/代码围栏闭合）为准；分节以 `\n## ` 截断 + 标题齐全校验。`stop` 但结构不完整 → 补一次续写，不重写 | 少一个模型必须遵守的格式约定，减少失败面；结构校验能覆盖「提前结束」 |
| 2 | `decisions` 每节完成后由模型输出 1–3 条（每条 ≤ 40 字），harness 去重合并，总数上限 20；只存结论，不存原文 | 控制上下文增长，保证跨节一致性 |
| 3 | 用户停止 = 取消当前请求，保留已提交章节，当前节标未完成；终稿阶段插话默认排队到下一轮 control，不打断当前节（`interrupt_final=false`） | 避免半截章节被插话污染；行为可解释 |
| 4 | `WritingState` 进程内保存；跨进程恢复放 P2。重开会话时长文从头开始，不假装续写 | P1 控复杂度；避免状态不一致 |

---

## 9. 指标采集规范（Metrics Spec）

> 主文档 §13 只写了「测什么」，本节补「怎么采、采成什么、怎么判定」。不按统一 schema 采，各次实验的数字不可比，B 方案的成本假设也无法证伪。

### 9.1 两条记录，一个口径

**每次模型请求一条 `request_metric`（JSONL）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | string | ISO 时间 |
| `turn_id` / `request_id` | string | 关联同一次用户请求 |
| `phase` | enum | `control` / `final` / `outline` / `section` / `continuation` |
| `task_type` | enum | `short` / `long` / `tool` |
| `n_ctx` | int | 模型窗口 |
| `prompt_tokens` | int | `P_total`（完整渲染 prompt，含缓存前缀） |
| `prompt_n` | int | 本次实际重算的 prompt token（服务端返回） |
| `cache_hit` | bool | 前缀是否命中 |
| `cache_reused_tokens` | int | 复用的 token 数（能拿到则记） |
| `max_tokens` | int | 本次 `n_predict` |
| `generated_tokens` | int | 实际生成 |
| `finish_reason` | enum | `stop` / `length` / `cancelled` / `error` |
| `first_token_ms` | int | 首 token 延迟 |
| `prefill_ms` | int | prompt 处理时间 |
| `total_ms` | int | 端到端 |
| `grammar` | bool | 是否启用 GBNF/schema |
| `retry_index` | int | 0 = 首次 |
| `outcome` | enum | `ok` / `truncated` / `error` / `recovered` |
| `recovery` | enum | `none` / `continue` / `rewrite` / `short` / `split` |
| `generated_chars` | int | 生成长度（字符），用于校准 `chars_per_token` |

**每次用户请求一条 `turn_metric`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `turn_id` | string | 主键 |
| `task_type` | enum | `short` / `long` |
| `sections_total` / `sections_completed` / `sections_retried` | int | 长文分节状态 |
| `total_generated_tokens` | int | 含控制与终稿 |
| `recovery_tokens` | int | 恢复重试消耗 |
| `total_ms` | int | 端到端 |
| `cache_hit_rate` | float | 命中请求数 / 终稿请求数 |
| `completed` | bool | 是否完整交付 |
| `fallback_short` | bool | 是否降级为短答 |

### 9.2 采集点（不要散落在业务代码里）

1. **llama.cpp 请求封装处**：一条 `request_metric`（`prompt_tokens`、`prompt_n`、`cache_hit`、时延、`finish_reason`）。
2. **Orchestrator 的 turn 结束处**：一条 `turn_metric`（汇总）。
3. **恢复状态机转移处**：追加 `recovery` 字段（`continue` / `rewrite` / `short` / `split`）。
4. **章节提交处**：更新 `sections_completed`，供 UI 与指标共用。

### 9.3 三个必须回答的问题

| 问题 | 计算 | 判定 |
|---|---|---|
| `P_total` 口径准不准 | 对比客户端 tokenizer 计数与服务端 `prompt_tokens`，记录差值 | 差值 > 2% 时以服务端为准并打日志 |
| 缓存到底省了多少 | 实验 B：`cache_prompt` 开/关，同请求同 slot，比 `prefill_ms` | 命中时 prefill 降幅 ≥ 70% 才保留双请求；< 50% 回退单请求轻协议 |
| 单节预算合不合适 | `target_tokens` vs `generated_tokens`；`chars_per_token` 按 phase 统计 | 偏差 ±20% 内算合理；超出则重定 `section_max` 或 `chars_per_token` |

### 9.4 采样与判定

- 四组实验（§12.1）每组 ≥ 20 条同类型样本；长文每组至少 5 条 ≥ 5 节。
- 固定模型、量化、采样参数、slot；缓存实验必须开/关各跑一轮。
- 判定阈值写进报告，不达标不允许「口头通过」。

### 9.5 报告与验收物

- 原始数据：`metrics/YYYY-MM-DD.jsonl`（每行一条，单条 < 1KB）。
- 汇总：`docs/metrics/YYYY-MM-DD-report.md`，含分 phase 的 count / 截断率 / P50 首 token / P50 总时延 / 缓存命中率 / 预算利用率 / 恢复率。
- **P1-6 与 P2 的验收附件必须包含该报告**；没有报告视为验收未完成。
- 不存 prompt / final 正文，只存计数、时延、`finish_reason` 与内容 hash（隐私与体积）。
