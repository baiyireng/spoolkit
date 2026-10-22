# ADR：长文 State / Graph 边界

状态：已接受（L0.1），L0.3–L0.14 后续写结论（L3.0）  
基线：`web-taskflow-rc2` / `ab08bf2`  
范围：契约。本文件不改 `AgentLoop`、不引入 LangGraph。L3.1 只落地 `DocumentState` 类型。

一份长文档用一套 `DocumentState` 和一张 `LongFormGraph`。聊天和文件是两个 sink，不是两条管道。正文在 artifact / 文件里。State、checkpoint、每轮上下文、`TaskEvent` 只放进度和引用。

## DocumentState

所有权在图运行时。节点不持有它，`AgentLoop` 不写它。

| 字段 | 含义 | 所有权 | reducer | checkpoint |
|------|------|--------|---------|------------|
| `schema_version` | 状态模式版本，从 1 起 | 运行时 | replace | 存 |
| `goal` | 这份文档要完成什么 | 调用方 | replace | 存 |
| `source_artifacts` | 旧文档 id 元组 | 调用方 | append（按 id 去重） | 只存 id |
| `deliverable` | `chat` / `file` / `both` | 调用方 | replace | 存 |
| `path` | 目标路径；纯聊天为空 | 调用方提议，审批后才算数 | replace | 只存路径 |
| `outline` | 节：`index` / `title` / `points` / `target_chars` | `plan_outline` | replace | 存大纲，不存正文 |
| `completed_sections` | 已提交节：`index` / `title` / `summary` / `artifact_id` / `path` / `char_count` | `commit_section` | append | 只存引用、摘要、字数 |
| `current_section` | 当前节：`index` / `title` / `summary` / `tail` / `char_count` / `status` | `write_section` / `continue_section` / repair / rewrite | replace | 见下 |
| `pending_sections` | 还没开始的节下标 | 运行时 | replace | 存下标 |
| `phase` | `plan` / `write` / `continue` / `repair` / `rewrite` / `commit` / `finalize` / `stopped` / `error` | 运行时 | replace | 存 |
| `status` | `running` / `paused` / `done` / `failed` / `cancelled` | 运行时 | replace | 存 |
| `error` | 最近一次失败说明；无则空 | 运行时 | replace | 存 |
| `length_floor` | 单节完成下限，默认 450 | 运行时 | replace | 存 |
| `repair_attempts` | 每节已补写次数，按下标对齐 | repair | replace | 存次数 |
| `rewrite_attempts` | 每节已改写次数，按下标对齐 | rewrite | replace | 存次数 |

正文规则：

- state 不存整节正文，也不存整篇文档。
- `completed_sections[]` 只有引用、摘要和字数。`summary` 不超过现有 240 字摘要上限。
- `current_section.tail` 最多 200 字，只给下一次续写看最后一个字之后的衔接。更早的正文在文件或 artifact 里，用 `path` / `artifact_id` 指回去。L0.6、L0.7 里 280 字窗口会从窗口开头重放，短尾才接得上。
- `char_count` 是字数，不是正文。
- 禁止字段：节全文、整篇文档、聊天记录、工具 raw。

## State / Update / reducer

| 规则 | 约定 |
|------|------|
| State | frozen。更新产出新对象，旧对象不变。 |
| Update | 节点的唯一返回值。缺省字段表示不动。 |
| replace | 标量：`goal`、`path`、`deliverable`、`outline`、`current_section`、`pending_sections`、`phase`、`error`、`status`、`schema_version`、`length_floor`、`repair_attempts`、`rewrite_attempts`。 |
| append | `completed_sections`、`source_artifacts`。后者按 id 去重，顺序稳定。 |
| merge_tuple | 有序不可变元组的合并，调用方指定；未知项拒绝。 |
| merge_dict | 预留给元数据。v1 没有正文 map。 |
| 非法字段 | reducer 拒绝，不静默丢掉。 |
| 顺序 | 同一线程里按节点返回顺序归约，结果确定。 |

运行时是唯一写入者：`new_state = reduce(state, update)`。节点函数签名是 `(state, ctx) -> Update`。`ctx` 只提供 emit、限额、取消，不提供 state 的 setter。

## 节点不直接改 state

- 节点不给 `DocumentState` 字段赋值，不原地改元组或列表。
- 节点不写 checkpoint，不写 Web UI，不直接落工作区文件。
- 副作用只通过 `ctx.emit` 交给运行时。运行时先归约 Update，再按 Update 里声明的 sink 动作调用 `ChatSink` 或 `FileSink`。
- 节点失败时返回带 `error` / `status` 的 Update，或抛给运行时；不把半截对象塞回调用方。

## Checkpoint

一条 checkpoint 只含：

| 项 | 内容 |
|----|------|
| `thread_id` | 这份文档的恢复键 |
| `schema_version` | 与 state 相同 |
| `next_node` | 恢复后要进入的节点 |
| `attempts` | 该节点已尝试次数 |
| `state` | 上表允许落盘的字段 |

不存：节正文、整篇文档、prompt、模型 raw、`TaskEvent` 列表、工具输出。

原子写 JSON。恢复时跳过已完成节点，从 `next_node` 继续。`schema_version` 与当前代码不一致就拒绝恢复，不猜字段。`current_section.tail` 超过 200 字的文件视为坏 checkpoint，不把多余正文读进 state。正文以 `path` / `artifact_id` 为准。

## Sink 副作用

两个 sink 都不归约 state。它们消费运行时已经接受的增量，把引用回交给运行时，由 reducer 写入。

### ChatSink

| 做 | 不做 |
|----|------|
| 把已接受的文本增量流式写入助手消息 | 写工作区文件 |
| 发 `section_start` / `section_complete` / `continuing` 这类进度 | 改 `DocumentState` |
| 中断时停下流；已显示的字留下 | 把全文写入 `TaskEvent` |
| | 重开已写段落、重复已提交节 |

### FileSink

| 做 | 不做 |
|----|------|
| 节内容先写临时文件，完成后原子替换目标 | 绕过现有 `write_file` / 权限 |
| 中断留下 `.partial`，路径写进 `current_section` | 未批准就替换目标文件 |
| 走现有 pending / confirm | 把文件内容抄进 state 或 `TaskEvent` |
| 完成后回 `path`、hash、size、区间 | 自己发 UI 事件 |

拒绝审批：不替换目标，state 保持未提交。ChatSink 与 FileSink 可以先后消费同一节，顺序由 `deliverable` 决定，不各写一套状态。

## 和现有对象的关系

三者回答不同的问题，不合并成一个对象。

| 对象 | 问题 | 谁写 | 正文 |
|------|------|------|------|
| `TaskState` | 这次任务做到哪：`goal` / `done` / `current` / `verify` / `hypothesis` | `AgentLoop` 经 `StateDelta.apply` | 不放。检查点是任务进度，任务结束后清掉 |
| `WritingState` | 当前大纲、已完成节号、跨节「决定」 | 现有写作层 | 不放逐节正文。`DocumentState` 以后复用它的大纲和决定规则，本轮不迁移 |
| `TaskEvent` | 一轮里按 `seq` 排好的一条观察 | runner / `on_event` | `summary` 截到 240 字。`artifacts` 只有 `type` / `path` / `ref`，注释写明不进模型提示 |

图节点以后只通过现有 `on_event` 发 `TaskEvent`，不新造事件信封，不在事件里塞全文。`TaskState.current` 可以点出「正在写某份文档」，文档进度仍在 `DocumentState`。

`AgentLoop` 继续负责 ReAct。图只负责确定性的长文和记忆流程。L1.1 留在 `request_final` 里，只作为聊天截断时的 fallback：那一次请求可以带预算内的尾部，尾部不写入 `TaskState`，也不写 checkpoint。它不是长文主路径。

## 续写实验结论（L0.3–L0.14）

三篇技术说明、项目复盘、方案对比，在 `temperature=0`、同一本地模型、`n_ctx=8192` 下各跑 3 次。初稿是已经裁好的半句。完成线在看到结果之前定下，之后没有改：每节至少 450 字且为完整句，全文至少 3000 字，每节有标题，不提前写「完」，12 字重叠低于 0.5。

| 结论 | 依据 |
|------|------|
| L1.2 多轮 raw tail 取消 | L0.3：只把尾部再送一轮，不能稳定写完，还会整段重放 |
| L1.1 只做 chat fallback | 产品路径仍是截断后带尾部续写一次。长文主路径不靠它加轮数 |
| L3 用结构续写，不按节各写一次 | 大纲、最近标题、上一节摘要、未完成标题要一起给。按节单独写会丢标题。形状是先收当前节，再一次写出未开始的节 |
| 短尾衔接 | 当前节用最后约 80 字，从最后一个字之后接着写。280 字窗口会从窗口开头重放 |
| repair 按节隔离，失败后 rewrite | 每节最多补 2 次。仍短、重复或脚手架命中，就整节改写并替换，不追加。一节失败不停下其他节。改写通过也不代替全文判定 |
| novelty 守卫 | 补写对已有正文的 12 字重叠达到 0.5，或同一结尾句再出现，整段不写入。重试一次仍重复则这次补写失败 |
| scaffold 守卫 | 行首标签「要点」「目标剩余字数」「本节目标」「提示词」「指令」「大纲」不写入。句子里的「系统提示词」「指令」「大纲」不算 |
| think 守卫 | `<think>`、思考过程或大段英文分析整段拒绝，不删标签再算完成。重试一次仍泄漏则失败 |
| anchor 守卫 | 重试开头看不到当前节最后 12 字，也看不到被拒绝返回的最后 12 字，记 `anchor_lost`，不再从尾部接，改为改写当前节。未恢复的 `anchor_lost` 不算完成 |

L0.14 上三篇都达到上述完成线。泄漏和脚手架留在被拒绝的返回里，没有进入正文。

## 本轮不做

- 不实现图运行时、checkpoint、`ChatSink`、`FileSink`。
- 不把 `DocumentState` 接进 `AgentLoop`、UI、artifact 或 session memory。
- 不做 L0.2 指标。
- 不做 L1.2。不做 L3.2。
- 不改 UI、`task_events`、持久化。
- 不把长文放进 semantic memory。
- 不做多租户或分布式 checkpoint。
- 长文落盘不绕过审批。
