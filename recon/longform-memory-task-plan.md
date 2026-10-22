# Long-form 与 Memory 任务计划

基线：`web-taskflow-rc2` / `ab08bf2`  
工作树：`.worktrees/longform-memory`  
分支：`feat/longform-memory`

## 目标

同时支持两种“完整文档”：

1. **聊天长回答**：分节流式输出，截断后能连续续写，不静默降级成简略版。
2. **文件型文档**：分节写入文件，可中断、可恢复、原子落盘。

上一轮对话产出的长文档必须成为可检索、可引用的 artifact；下一轮要能基于它整理出新的文档并落地。

## 设计原则

- 先修续写正确性，再做图编排。
- 一套 `DocumentState` / `LongFormGraph`，两个 sink：`ChatSink`、`FileSink`；不要做两条独立管道。
- 文档正文放 artifact/file，不进 state、不进每轮上下文。
- Artifact memory 与 semantic memory 分开；J4 继续拒绝长交付正文进入 semantic。
- v1 不引入 LangGraph 依赖，按 LangGraph 的 state/node/edge/checkpoint/interrupt 纪律自封装。
- 不重写 `AgentLoop`；主循环负责 ReAct，图负责确定性长文/记忆工作流。
- 每个任务独立提交、独立测试；不做跨任务大爆炸改动。

## 续写实验结论（L0.3–L0.14）

L0.3 到 L0.14 不改产品代码。三篇各 3 次，`temperature=0`，同一模型，`n_ctx=8192`。

- **L1.2 取消。** 多轮 raw tail 不能稳定写完，还会整段重放。长文不靠加续写轮数。
- **L1.1 只做 chat fallback。** `request_final` 在聊天截断时仍可带预算内的尾部。这不是 L3 的主路径。
- **L3 用结构续写，不按节各写一次。** 大纲、最近标题、上一节摘要、未完成标题一起给。先收当前节，再一次写出未开始的节。按节单独请求会丢标题。
- **repair 按节隔离，失败后 rewrite。** 每节最多补 2 次。仍短、重复或脚手架命中，就整节改写并替换。一节失败不停下其他节。
- **守卫：** novelty（12 字重叠或同一结尾句）、scaffold（行首提示标签）、anchor（重试接不上最后 12 字就改写）、think（整段拒绝，不删标签）。
- **完成判据：** 每节至少 450 字且为完整句，全文至少 3000 字，无脚手架，无重复凑字数，无未恢复的 `anchor_lost`，无 think。

L0.14 三篇都达到这条线。L3.1 只把 `DocumentState` 做成类型和校验。不写图，不进入 L3.2。

## 任务清单

### L0：契约与观测

#### L0.1 State / Graph 边界 ADR

- 产出 `recon/adr-longform-state.md`。
- 明确：
  - `DocumentState` 字段与所有权；
  - `State` / `Update` / reducer 的规则；
  - node 不直接 mutate state；
  - checkpoint 只存状态与引用，不存全文；
  - chat/file sink 的副作用边界；
  - 与 `TaskState`、`WritingState`、`TaskEvent` 的关系。
- 验收：ADR 评审通过，列出不做的能力。
- 依赖：无。

#### L0.2 续写/分节观测

- 增加结构化指标：final 调用数、continuation 轮数、截断次数、每节 tokens、总耗时、完成状态。
- 不改变行为。
- 验收：一次截断测试能输出 `phase=continuation`、`round`、`truncated`、`completion_tokens`。
- 依赖：无。

### L1：先修续写正确性（P0）

#### L1.1 续写携带 partial 尾部（chat fallback，已落地）

- 已修改 `src/spoolkit/agent/final_turn.py`。
- 只服务聊天截断后的那一次续写，不是长文主路径。
- 续写请求包含预算内的已写尾部。长文 state 里的 `current_section.tail` 另有 200 字上限。
- 续写指令只要求输出新增部分，不重复、不重开。
- 保留现有 overlap 机制。
- 测试：
  - 请求里能看到 partial 尾部；
  - 不同模型输出经 merge 后不重复；
  - 空尾部/极短尾部不崩。
- 依赖：L0.1。

#### L1.2 续写循环（取消）

- L0.3 否定了多轮 raw tail。不把 `request_final` 改成 4–6 轮循环。
- 长文完成靠结构续写、按节 repair，以及失败后的 rewrite。
- 依赖：无。不再实施。

#### L1.3 预算与重写策略

- 第一次 final 前根据 target/room 预分配，不等截断后才 boost。
- 禁止把整篇 1.5x 重写作为常规路径；重复时优先重写当前段/换续写指令。
- `sections.generate_section` 复用同一续写策略，不再每节只续一次。
- 测试：
  - 长目标第一次就拿到更大预算；
  - 重复时只重写当前节；
  - 失败时 partial 保留。
- 依赖：L1.1。不按已取消的 L1.2 做多轮 raw tail。L3 主路径不走这里。

### L2：自封装 Graph Runtime

#### L2.1 State / Update / Reducer

- 新增 `src/spoolkit/workflow/state.py`（名字可评审）：
  - frozen dataclass / TypedDict state；
  - `Update` 增量；
  - reducer：`replace`、`append`、`merge_tuple`、`merge_dict`；
  - schema version。
- 测试：不可变更新、并发/顺序确定性、非法字段拒绝。
- 依赖：L0.1。

#### L2.2 Graph Runtime

- 新增 `workflow/graph.py`：
  - `add_node`、`add_edge`、`add_conditional_edges`；
  - `max_steps`；
  - cancel token；
  - node context（emit、limits、cancel）。
- 测试：条件边、终止、max_steps、节点异常。
- 依赖：L2.1。

#### L2.3 Checkpoint / Resume

- 新增 `workflow/checkpoint.py`：
  - thread_id；
  - state + next node + attempts + schema_version；
  - JSON 原子写；
  - resume 后不重复已完成节点。
- 测试：中途停止、恢复、schema 迁移、坏文件跳过。
- 依赖：L2.2。

#### L2.4 Interrupt / Events

- 定义 interrupt：stop / continue / approval。
- 图节点只发事件，不直接写 Web UI。
- 与现有 `TaskEvent` / `on_event` / pending confirm 对接，不新造一套。
- 测试：暂停后 resume、取消后保留 partial。
- 依赖：L2.2、L2.3。

### L3：LongFormGraph + ChatSink

#### L3.1 DocumentState

- 新增 `src/spoolkit/agent/document_state.py`。
- 字段：`schema_version`、goal、source_artifacts、deliverable、path、outline（index/title/points/target_chars）、completed_sections（引用/摘要/字数）、current_section（tail 最多 200 字）、pending_sections、phase、status、error、length_floor、repair_attempts、rewrite_attempts。
- state 不存整节正文。
- 校验：单节不少于 450、全文不少于 3000、完整句、无 scaffold、无 think、无重复凑字数、无未恢复的 anchor_lost。
- `schema_version` 不符则拒绝恢复。
- 不写 Graph、sink，不接 `AgentLoop`。
- 测试：合法/非法 state、序列化往返、不存全文、长度、scaffold/think/重复、版本拒绝。
- 依赖：L0.1 与 L0.3–L0.14 的结论。类型本身不等 L2.3。

#### L3.2 LongFormGraph

- 节点：
  - `plan_outline`
  - `write_section`
  - `continue_section`
  - `commit_section`
  - `finalize`
- 条件边：继续/提交/下一节/完成/停止。
- 第一版只接 `ChatSink`。
- 测试：3–6 节、截断续写、停止、恢复。
- 依赖：L3.1。

#### L3.3 ChatSink 与进度

- 流式写入助手消息；
- 事件：section_start / section_complete / continuing；
- 不重复正文，不重新开头；
- 中断时保留 partial 和“继续”入口。
- 测试：节顺序、无重复、中断可继续。
- 依赖：L3.2。

### L4：FileSink 与 Artifact

#### L4.1 Artifact Store

- 新增 artifact registry：
  - id、session、task、turn、kind、title、summary、path、hash、size、created_at；
  - section/chunk 元数据。
- 正文落文件；DB 只存元数据/摘要/索引。
- 复用脱敏；按 hash 去重。
- 测试：写入/读取/去重/缺失文件。
- 依赖：L2.3。

#### L4.2 FileSink

- 每节写入临时文件，完成后原子替换目标；
- 中断保留 `.partial`；
- 文件写入遵守现有 pending/confirm 审批；
- 不绕过 `write_file`/权限。
- 测试：中断恢复、原子替换、审批拒绝不落盘。
- 依赖：L3.2、L4.1。

#### L4.3 Artifact 与 TaskEvent / UI

- final/节完成时把 artifact_id 写进消息 meta / TaskEvent；
- 任务卡显示文档 artifact；
- 不把全文放进 TaskEvent。
- 测试：刷新后仍能定位 artifact。
- 依赖：L4.1、L4.2。

### L5：Artifact Memory / Session Memory

#### L5.1 自动捕获长 final

- final 后按长度/标题/用户意图自动保存为 artifact；
- transcript 只挂引用；
- 可配置关闭、大小上限、保留期。
- 测试：长 final 自动归档；短回答不归档；secret 脱敏。
- 依赖：L4.1。

#### L5.2 Artifact 索引与检索

- 按标题/摘要/章节建 FTS；
- 新增 `search_artifacts`（或扩展现有 search_history）；
- 返回 artifact 引用 + 章节片段，不返回全文。
- 测试：按标题、章节词、路径命中；跨 session 范围正确。
- 依赖：L5.1。

#### L5.3 Session Summary

- 每轮增量更新会话摘要：
  - 做过什么；
  - 产物目录；
  - 决策/约束；
  - 未完成事项。
- 持久化到 session 级存储；不把摘要当事实。
- 测试：多轮更新、压缩、恢复。
- 依赖：L5.1。

#### L5.4 Context Pack

- 按当前 goal 组装：
  - 当前 task state；
  - session summary；
  - 最近轮次；
  - 相关 artifact 目录/章节；
  - 相关 semantic facts/decisions；
  - 预算裁剪。
- 模型只拿引用/片段，不拿整篇旧文档。
- 测试：预算上限、优先级、无命中。
- 依赖：L5.2、L5.3。

#### L5.5 旧文档重组

- 识别“把之前对话/文档整理成新文档并落地”；
- 选 artifact（最近/用户指定）；
- 取 outline + 必要章节；
- 启动 `LongFormGraph`，`source_artifacts` 指向旧 artifact；
- 最终输出新 artifact + 文件。
- 测试：旧文档 → 新文档；引用正确；不把全文塞入 prompt。
- 依赖：L5.4、L4.2。

### L6：UX 与验收

#### L6.1 长文进度 / 继续

- 聊天/任务卡显示“长文 2/5 节”“续写中”；
- 停止/继续；
- 不静默短答。
- 测试：进度事件、恢复按钮。
- 依赖：L3.3、L4.2。

#### L6.2 评测与回归

- 固定用例：
  - 6K/10K 中文文档；
  - 3+ 次截断；
  - chat/file/both；
  - 中断恢复；
  - 旧 artifact 重组。
- 指标：完成率、continuation 次数、总 tokens、耗时、重复率。
- 依赖：L5.5、L6.1。

## 推荐执行顺序

```text
L1.1（chat fallback，已落地）
L1.2 取消
L3.1 DocumentState
L2 的图运行时在 L3.2 之前需要，但不挡住 L3.1 的类型
L3.2 及之后仍按原顺序，本轮不进入 L3.2
L4.1 → L4.2 → L4.3
L5.1 → L5.2 → L5.3 → L5.4 → L5.5
L6.1 → L6.2
```

L0.1/L0.2 可并行做，但不要晚于 L1。

## 明确不做

- 不重写 `AgentLoop`。
- 不引入 LangGraph 依赖。
- 不把整篇文档放进 state / prompt / TaskEvent。
- 不让长文自动绕过审批写工作区。
- 不做多租户/分布式 checkpoint。
- 不在 L1 前做通用图框架。
- 不提交 `.cursor/`、`metrics/`、`p3_*`。

## 每个任务的交付格式

每个任务报告必须包含：

- 改动文件；
- 行为变化；
- 新增测试与命令；
- 未解决问题；
- 与基线的测试对照。

## Definition of Done

- 一份 6–10K token 文档能分 4–6 节完成；
- 截断后能连续续写，不出现“第三次变简略”；
- chat/file/both 三条路径都可用；
- 中断可恢复；
- 旧文档能作为 artifact 被检索和重组；
- 长文内容不靠 recent_messages 碰运气；
- 现有 `web_ci` 与 `tests/agent + tests/tools` 不回归。
