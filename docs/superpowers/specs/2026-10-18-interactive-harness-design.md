# 交互 Harness（进度可见）—— 设计文档

日期：2026-10-18  
状态：已定，已实施  
范围：一期 —— CLI `spool chat` / Web 网页壳的**进度轴**；二期正文流式见 [`2026-10-18-streaming-output-design.md`](2026-10-18-streaming-output-design.md)

---

## 1. 为什么做

非流式模型调用期间，用户只看到 15s 心跳（「主循环 / 输出加长」），看不到步骤、工具与截断重试含义。`spool chat` 未绑 `on_event`，长等时 stdout 完全盲；Web 虽有 SSE，但进展默认折叠，`wait` 逐条堆叠。

缺口是**进行中可见性**，不是超时公式。token 级流式是二期；只做流式治不好「输出加长」循环（中间产物是结构化回合，不是给人读的 Markdown）。

---

## 2. 范围

### 做（一期）

- CLI `chat`：装配 `on_event`，实时渲染 `step` / `tool` / `note`（stderr，muted）；`wait` 仍走既有 `WaitDisplay`。
- 截断重试：发出人话 `note`（放大后的 `max_tokens`），不仅靠 phase 名「·输出加长」。
- chat 输入：`exit` / `quit` / `q`（大小写不敏感）在进模型前等同 `/exit`。
- Web：同一 phase 的 `wait` **替换**轨迹最后一行；运行中自动展开「思考过程」；`final` 后可再收起。
- 文档：`getting-started` 注明心跳 ≠ 有输出；截断重试会重置该次等待计时。

### 不做（一期）

- 上游 `stream: true` / token 级 Markdown Live（二期）。
- 改超时公式、改 `--events` JSON 字段契约（可增 note 文案，不改既有键语义）。
- QQ / 桥推送手机心跳。
- 把工具 JSON 半截流式上屏。

---

## 3. 核心决策

### 3.1 进度轴 vs 正文轴

| 轴 | 一期 | 二期 |
|----|------|------|
| 进度（step/tool/note/wait） | 立刻可见 | 保持 |
| 正文（assistant 打字机） | 仍整段 `final`/`print_md` | `delta` + Live |

### 3.2 CLI chat 与 `--events` 同源语义、不同出口

| 模式 | 事件去哪 |
|------|----------|
| `spool chat` / `run` 散文 | human 渲染（stderr 进度；结论 stdout） |
| `spool run --events` | stdout JSON（网页 / 桥 / MCP） |

回调签名与 `AgentLoop.on_event(kind, data)` 一致；chat 注入 human handler，不写 JSON 到 stdout。

### 3.3 截断 note

在 `chat_with_escalation` 放大预算重试前，可选回调；loop 转成：

```text
note: 输出被截断，放大预算重试（max_tokens → N）
```

`--events` 与 human 同一条 note，Web 活动条能看见。

### 3.4 Web wait 替换规则

- 若轨迹末行 `type === 'wait'` 且 **phase 相同**（含空）：**替换**该行（更新 elapsed/limit 文案）。
- phase 变化（如「主循环」→「主循环·输出加长」）：**追加**新 wait 行，保留上一阶段最后一刻。
- 运行中（`live` 未 `done`）：自动展开思考过程；`final`/`error` 后恢复默认收起（失败仍展开——既有失败可见规则优先）。

### 3.5 exit 卫生

`EXIT_PLAIN = frozenset({"exit", "quit", "q"})`（大小写不敏感）→ 与 `/exit` 同路径退出，不进模型。`/help` 写明。

---

## 4. 事件表（一期消费）

沿用 [`protocol.py`](../../../src/spoolkit/web/protocol.py) 既有类型，不新增：

| type | CLI human | Web |
|------|-----------|-----|
| `step` | stderr 一行 | 思考过程 |
| `tool` | stderr 一行（名 → 成败） | 同上 |
| `note` | stderr 一行 | 同上 |
| `wait` | `WaitDisplay`（网关） | 替换/追加见 §3.4 |
| `final` | 回合后 `print_md`（不变） | 主气泡 |

---

## 5. 验收

1. `spool chat` 长任务：15s 内可见 step 或 note（不只 wait）。
2. 截断重试：出现「放大预算重试」note；心跳 phase 带「输出加长」。
3. 输入 `exit` 立即退出，历史不增加「再见」类助手轮。
4. Web：连续 wait 不堆成几十行；运行中思考过程展开。
5. 既有 `tests/cli/test_chat_command.py`、`tests/web/test_page*.py`、`tests/llm/test_retry.py` 通过。

---

## 6. 与心跳设计的关系

[`2026-09-21-model-wait-heartbeat-design.md`](2026-09-21-model-wait-heartbeat-design.md) 仍成立：心跳报「还在等」。本设计补「在等什么 / 做了什么」。二期流式前，心跳不可删除。
