# ADR：长文 State / Graph 边界

状态：已接受（L0.1）  
基线：`web-taskflow-rc2` / `ab08bf2`  
范围：契约。本文件不引入代码、不改 `AgentLoop`、不引入 LangGraph。

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
| `outline` | 节标题、要点、目标 tokens | `plan_outline` | replace | 存大纲，不存正文 |
| `next_section` | 下一节下标 | `commit_section` | replace | 存 |
| `partial` | 当前节的有界尾部或空 | `write_section` / `continue_section` | replace | 见下 |
| `sections` | 已提交节的引用和摘要 | `commit_section` | append | 只存引用和摘要 |
| `phase` | `plan` / `write` / `continue` / `commit` / `finalize` / `stopped` / `error` | 运行时 | replace | 存 |
| `error` | 最近一次失败说明；无则空 | 运行时 | replace | 存 |
| `status` | `running` / `paused` / `done` / `failed` / `cancelled` | 运行时 | replace | 存 |

`partial` 和 `sections` 的正文规则：

- `sections[]` 每项只有 `index`、`title`、`summary`、`artifact_id`、`path`、`char_start`、`char_end`。`summary` 沿用现有摘要上限，不放节正文。
- `partial` 要么为空，要么是 `{section_index, artifact_id, path, char_end, tail}`。`tail` 最多 2000 字符（落在 1500–2500 的续写窗口内），只为续写请求服务。更早的正文在文件或 artifact 里，用 `path` / `artifact_id` / `char_end` 指回去。
- 禁止字段：节全文、整篇文档、聊天记录、工具 raw。

## State / Update / reducer

| 规则 | 约定 |
|------|------|
| State | frozen。更新产出新对象，旧对象不变。 |
| Update | 节点的唯一返回值。缺省字段表示不动。 |
| replace | 标量：`goal`、`path`、`deliverable`、`outline`、`next_section`、`partial`、`phase`、`error`、`status`、`schema_version`。 |
| append | `sections`、`source_artifacts`。后者按 id 去重，顺序稳定。 |
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

原子写 JSON。恢复时跳过已完成节点，从 `next_node` 继续。`partial.tail` 超过窗口的文件视为坏 checkpoint，不把多余正文读进 state。正文以 `path` / `artifact_id` 为准。

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
| 中断留下 `.partial`，路径写进 `partial` | 未批准就替换目标文件 |
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

`AgentLoop` 继续负责 ReAct。图只负责确定性的长文和记忆流程。L1 的续写修复在 `request_final` 里，尾部只存在那一次请求的消息里，不写入 `TaskState`，也不写 checkpoint。

## 本轮不做

- 不实现 `DocumentState`、reducer、图运行时、checkpoint、sink。
- 不做 L0.2 指标。
- 不做 L1.2 多轮续写。`request_final` 仍是截断后续写一次，再截断就停并留下「继续」。
- 不做 L1.3 预算预分配，也不改整篇 1.5 倍重写。
- `sections.generate_section` 仍用原来的续写指令，不带 partial 尾部。
- 不改 UI、`task_events`、持久化。
- 不把长文放进 semantic memory。
- 不做多租户或分布式 checkpoint。
- 长文落盘不绕过审批。
