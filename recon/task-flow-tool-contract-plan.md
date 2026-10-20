# 任务流 / 工具契约 与 对话流改造：调整后的路线

## 为什么要调整

原计划把“对话流：用户/助手气泡、段落节奏、轨迹、代码/diff”当作 M3 的视觉工作包。
这个拆法不成立：

- 当前对话流的“长文列表感”不是 CSS 问题，而是**数据模型问题**：
  - 工具返回仍以 prose 为主；
  - 事件只有 step/tool/diff/final 等少量类型；
  - 轨迹是文本行，不是可分组、可折叠、可回放的 task events；
  - 模型看到的上下文、UI 看到的原始输出、证据层三者没有彻底分开。
- 如果先做气泡/间距/轨迹样式，等 `ToolResult` 和 `TaskEvent` 落地后，
  这些组件很可能要重做一遍。
- 因此：**对话流任务后移，与工具契约/事件模型合并成一条任务流改造线**。
  当前前端 M1/M2 的壳层改造保留，不浪费；只是不再继续做“纯视觉对话流”。

## 结论

1. **暂停 M3-C（对话流视觉）**，不先做气泡、段落、轨迹、diff 的样式重做。
2. **先做 B0：测量 + 盘点**，不再凭感觉改工具说明。
3. **新增 B1–B3：ToolResult / TaskEvent / 上下文投影 / 对话流 UI**，
   让 UI 消费结构化事件，而不是继续解析长文本。
4. **M3-A/B/D/E（token、壳层、状态面板、视觉验收）可以保留**，
   但应排在 B1 契约之后，和 B3 一起做，避免视觉变量反复。
5. C2a（顶栏/搜索/临时目录）是独立壳层任务，可并行或稍后；
   C2b（重命名）、P1-15（标题抢模型）、P2 打磨继续后放。

## 新路线

```text
现在
├─ B0  工具输出 / 上下文 token 测量 + TaskEvent / ToolResult 盘点
├─ C2a 顶栏/搜索/临时目录（可选并行，不碰对话数据）
│
├─ B1  契约：ToolResult v2 + TaskEvent + 上下文投影
├─ B2  迁移高价值工具 + runner 事件 + 旧工具适配器
│
├─ B3  对话流重构（吸收原 M3-C）
│    用户/助手气泡、段落节奏、任务时间线、轨迹、代码/diff
│
├─ B4  视觉系统与验收（吸收原 M3-A/B/D/E）
│
└─ 后置：C2b 重命名、P1-15 标题竞态、P2 打磨
```

## B0：先测量，不写业务代码

交付一份 `recon/tool-output-audit.md`，至少包含：

### 1. ToolResult 使用盘点

逐工具列出：

- 是否返回 `ToolResult`；
- `content` 的典型长度 / 上限；
- 是否使用 `raw`；
- 是否使用 `facts` / `usage`；
- 是否有结构化 `data`；
- UI 当前如何消费（trace / diff / toast / 直接文本）。

### 2. 上下文 token 测量

真实任务一轮，拆出：

- system 模板
- 工具索引 `registry.describe()`
- workflow 规则 `build_workflow()`
- `tool_help` 输出
- 最近工具结果
- 历史 / 记忆 / 预取

找出 top 5 消耗源。重点验证“工具说明长”到底是：

- `description`（通常只在 tool_help）？
- `brief`/索引？
- `WORKFLOW_*` 规则？
- 工具 `content` 本身？

### 3. 事件盘点

当前 runner / web 已有：

- `step` / `tool` / `await` / `diff` / `confirm` / `final` / `error`
- `trace` 文本行
- `diffs` / `awaiting_detail`

列出每类事件：

- 谁产生；
- 给 UI 什么；
- 给模型什么；
- 是否足够支撑“任务时间线”。

### 4. 迁移候选

按“上下文收益 + UI 收益 + 风险”选 3–5 个工具先迁移，候选：

- `read_file`
- `search_code`
- `run_command`
- `write_file` / `edit`
- `verify_file`

## B1：契约

### ToolResult v2

```text
ok
summary    # 给模型的短摘要，硬上限
data       # 结构化结果，给后续编排/UI
artifacts  # 文件、diff、命令、URL 等引用
raw        # 原始输出，只给 UI/证据，默认不回灌
usage
next       # 可空的下一步建议
content    # 兼容别名
```

原则：
- 模型只吃 `summary` + 必要 `data`；
- UI 可以吃 `data/artifacts/raw`；
- 证据层保留 `raw`，但不进上下文；
- 单工具可扩展，不要强行统一所有 payload。

### TaskEvent

```text
turn_id, seq, phase
  phase: understand | act | observe | decide | final | error
tool, intent, args_digest
status, summary, refs, artifacts
duration, risk
```

### 上下文投影

- 完整事件进事件库/日志；
- 每轮只投影：目标、最近 N 个动作 intent、结果摘要、未决问题、失败/下一步；
- 原始 `thought` 默认不进上下文；只保留可审计版本；
- 上下文里不重复 UI 的详细 raw。

## B2：迁移

- 先迁移 3–5 个高价值工具；
- 其余工具用适配器：旧 `ToolResult` → 新 envelope；
- runner 发出 TaskEvent；
- 保留现有行为测试，新增契约测试：
  - 每个工具返回合法 envelope；
  - `summary` 有上限；
  - `raw` 不进模型可见区；
  - 事件顺序/阶段可回放。

## B3：对话流重构（原 M3-C）

只有 B1/B2 之后才做：

- 用户/助手气泡：角色、边界、时间/分组；
- 段落节奏：标题/列表/代码/表格/引用；
- 任务时间线：intent → tool call → result → decision → final；
- 轨迹：默认折叠，不再是一行行文本；
- 代码/diff：结构化卡片，和事件关联；
- 失败/重试：事件化，不靠系统消息拼接。

## B4：视觉系统

B3 稳定后再做：

- tokens / typography / spacing / radius / elevation；
- 顶栏、侧栏、输入条、状态面板；
- 移动端 sheet/scrim；
- 1440 / 1024 / 390，深浅色验收。

## 现在冻结什么

- 冻结 M3-C 的对话流样式重做；
- 冻结“先改工具 description”的瘦身，等 B0 数据；
- 冻结 M3 的大规模视觉 token 改动，等 B3 组件形态稳定；
- 保留：M1/M2 已完成的壳层、Markdown、空态、草稿、diff/命令卡；
- 保留：C2a 这类不依赖事件模型的壳层任务可并行。

## 验收原则

- B0 没有数据，不进入 B1；
- B1 没有契约测试，不进入 B2；
- B2 没有事件回放，不进入 B3；
- B3 没有真实任务流数据，不进入 B4；
- 任何阶段都不以“看起来更好”代替“数据结构更清楚”。
