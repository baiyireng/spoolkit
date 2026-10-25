# 正文流式输出（CLI + Web）—— 设计文档

日期：2026-10-18  
状态：已定，已实施（窄切：主循环 schema 流式 + FinalFieldTap 只上屏 final；CLI Live / Web 气泡）  
前置：[`2026-10-18-interactive-harness-design.md`](2026-10-18-interactive-harness-design.md)（进度轴已落地）  
相关：[`2026-09-21-model-wait-heartbeat-design.md`](2026-09-21-model-wait-heartbeat-design.md)

---

## 1. 目标

面向人的自然语言结论像 ChatGPT / DeepSeek 一样**边生成边出现**（CLI TTY + Web 主气泡）。与一期进度轴互补：进度回答「在干什么」，流式回答「正在写什么」。

---

## 2. 范围

### 做

- `ModelGateway` 增加可选流式入口（如 `chat_stream` → yield text deltas）；llamacpp `"stream": true` 读 SSE chunk。
- 协议新增 `delta`：`{"type":"delta","text":"…","channel":"assistant"}`（可后续扩 `trace`）。
- `final` 仍表示回合完成；累计文本与 `delta` 拼接一致（幂等：只收 `final` 的老客户端仍可用）。
- CLI human：TTY 上 Rich `Live` 刷新 Markdown 缓冲；管道 / `NO_COLOR` 追加纯文本；`--events` 只发 JSON。
- Web：主气泡增量更新；节流 Markdown 渲染；与 `final` 收敛。
- 截断（`finish_reason=length`）：停 Live、发既有 truncation note、再开第二轮流。

### 不做

- 工具调用 JSON / 结构化 schema 回合的半截流式上屏（仍缓冲完整再解析）。
- 改超时公式；删心跳（无 delta 时 wait 仍必要）。
- 强制所有 provider 同日支持流式（未实现则静默退回非流式整段）。

---

## 3. 核心决策

### 3.1 双通道

| 通道 | 内容 | 流式？ |
|------|------|--------|
| 结构化回合（主循环 tool schema） | JSON tool_calls | 否，整段 |
| 面向人的报告 / chat 终稿 | Markdown 正文 | 是（本设计）|

Agent 主循环多数步是结构化输出——流式收益在**终稿报告**与「少工具的纯问答」；进度轴仍靠一期事件。

### 3.2 事件

```json
{"type":"delta","text":"一段增量","channel":"assistant"}
```

- `text`：增量（非全量快照）。
- `channel`：先只实现 `assistant`。
- 加入 `KNOWN`；老网页忽略未知类型的行为不变——**必须**把 `delta` 加入白名单。

### 3.3 CLI Live

- 仅 TTY 且非 `NO_COLOR`：`Live` 刷新 `Markdown(buffer)`。
- 非 TTY：每个 delta `print` 追加，无 Live。
- `--events`：不写散文，只 `EventWriter.emit(DELTA, …)`。

### 3.4 Web

- `start` 后气泡可从「生成中」改为空串，收到 `delta` 追加。
- `final`：用完整文本覆盖（防漏 delta）；思考过程收起规则同一期。

---

## 4. 实施顺序（建议）

1. protocol + 单测 parse  
2. llamacpp `chat_stream` + fake 回放流  
3. loop / report 路径在「纯文本终稿」处接流（先窄切，勿一次改全主循环 schema）  
4. CLI Live + Web 气泡  
5. 截断重试与 delta 的衔接手工验收  

---

## 5. 验收

1. 纯问答（少工具）TTY：可见正文逐段增长，不必等整段 HTTP。  
2. Web 同口径。  
3. `--events` 消费者只见 JSON；管道无 ANSI。  
4. 工具回合仍整段解析，无半截 JSON 炸解析器。  
5. 既有非流式路径（`stream:false` / 无 `chat_stream`）行为不变。
