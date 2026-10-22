# Cursor 执行计划：Task Flow / Tool Contract / Conversation UI

## 0. 这份文件怎么用

- 这是给 Cursor 的**分阶段执行手册**，不是一次性大重构。
- 每个 Phase 完成必须写报告，然后**停下等确认**，不要自动跳下一阶段。
- 报告目录：`recon/cursor-reports/`。
- 当前基线：分支 `s10-drop-orchestrate`，HEAD `c76a4bb`，`web_ci.py` **396 passed**。
- 当前工作树有 `.cursor/`、`metrics/`、`recon/` 旧截图、`scripts/p3_*` 等本地未跟踪产物；不要提交它们。

## 0.1 分支与提交

从当前 HEAD 新建分支：

```powershell
git switch -c feat/task-flow-contract
```

提交策略：

- B0：只提交 `recon/tool-output-audit.md` 和报告，不改产品代码。
- B1/B2/B3/B4：每个 Phase 一个 commit，测试通过再提交。
- 不 push，不 amend，不 reset，不 checkout 旧 commit。
- 不提交 `.cursor/`、`metrics/`、`scripts/p3_*`、`recon/cdp.py`、`recon/drive*.py`、`recon/audit*.json`。

## 0.2 全局硬约束

- 不先做 B3 对话流，不先精装修气泡/轨迹。
- 不重写所有工具，先迁移 3–5 个。
- 不把原始 `thought` 整段回灌上下文。
- 不引入前端框架/ESM 大迁移。
- 不删除/弱化行为测试。
- 每个 Phase 结束时 `python scripts/web_ci.py` 必须通过；数字不能低于 396。
- 遇到需要产品决策/接口扩展，写进报告并停下，不要自行扩大范围。

## 0.3 报告模板

每个 Phase 写 `recon/cursor-reports/<phase>.md`：

```md
# <Phase> 报告

## 状态
完成 / 部分完成 / 阻塞

## 改了什么
- 文件：
- 行为：

## 关键数据
- 测试：
- token/长度：
- 事件：

## 决策
- 做了什么选择，为什么

## 风险/未完成
- ...

## 下一步建议
- ...
```

---

# Phase B0：测量与盘点

**目标**：拿到可决策的数据，不改产品代码。  
**输出**：
- `recon/tool-output-audit.md`
- `recon/cursor-reports/B0.md`

## B0.1 ToolResult 盘点

逐工具列出：

- 是否返回 `ToolResult`
- `content` 典型长度/上限
- 是否使用 `raw`
- 是否使用 `facts/usage/data`
- UI 当前消费方式

## B0.2 上下文 token 拆分

拆出：

- system 模板
- `registry.describe()` 工具索引
- `build_workflow()` 工作方式
- `tool_help` 完整说明
- 最近工具结果
- 历史/记忆/预取

真实 provider 优先；没有就明确标注估算方法。

结论必须回答：

- “工具说明长”到底长在 description、brief、WORKFLOW，还是工具结果；
- top 5 消耗源。

## B0.3 事件盘点

列出：

- step / tool / await / diff / confirm / final / error / wait
- trace 文本行
- diffs / awaiting_detail

每个事件：谁产生、UI 怎么用、模型看不看、缺什么字段。

## B0.4 动态采样

如果 provider 可用，跑 3 个代表任务：

1. 读/查 + 回答
2. 改一个文件 + 验证
3. 失败/重试或命令授权

记录 prompt/completion tokens、每步工具结果长度、实际事件形状。

## B0.5 迁移建议

按“上下文收益 × UI 收益 ÷ 风险”选 3–5 个候选，优先：

- read_file
- search_code
- run_command
- write_file / edit
- verify_file

每个候选写清 B1 后 `summary/data/raw/artifacts` 各是什么。

## B0 停止条件

- 报告完成；
- 不改产品代码；
- 停下等确认，不进入 B1。

---

# Phase B1：契约

**前置**：B0 报告已确认。  
**输出**：
- `ToolResult` v2（向后兼容）
- `TaskEvent`
- 上下文投影规则
- 契约测试
- `recon/cursor-reports/B1.md`

## B1.1 ToolResult v2

```text
ok
summary    # 给模型，硬上限
data       # 结构化，给后续编排/UI
artifacts  # 文件/diff/命令/URL 引用
raw        # 只给 UI/证据，默认不回灌
usage
next
content    # 兼容别名
```

要求：

- 旧工具不立即全部迁移；
- 新字段可选，不破坏现有调用；
- `summary` 有长度上限；
- `raw` 不进入模型可见区。

## B1.2 TaskEvent

```text
turn_id, seq, phase
  understand | act | observe | decide | final | error
tool, intent, args_digest
status, summary, refs, artifacts
duration, risk
```

先定义 dataclass/序列化/测试，不要求 UI 立刻消费。

## B1.3 上下文投影

- 完整事件进事件日志；
- 每轮只投影：目标、最近动作 intent、结果摘要、未决问题、失败/下一步；
- 原始 thought 默认不进上下文；
- raw 默认不进 prompt。

## B1 停止条件

- 契约测试通过；
- 396+ passed；
- 不迁移大批工具；
- 停下等确认，不进入 B2。

---

# Phase B2：迁移高价值工具

**前置**：B1 契约确认。  
**输出**：
- 3–5 个工具迁移
- 旧工具适配器
- runner 发 TaskEvent
- 契约测试
- `recon/cursor-reports/B2.md`

## 顺序建议

1. `read_file`
2. `search_code`
3. `run_command`
4. `write_file` / `edit`
5. `verify_file`

## 要求

- 迁移工具返回 `summary/data/raw/artifacts`；
- 未迁移工具继续走旧 `content`，适配器转换；
- runner 发出 TaskEvent，但 UI 暂不重构；
- 不删除现有行为测试；
- 396+ passed。

## B2 停止条件

- 事件顺序可回放；
- 契约测试通过；
- 停下等确认，不进入 B3。

---

# Phase B3：对话流重构（原 M3-C）

**前置**：B1/B2 完成。  
**输出**：
- 任务时间线 UI
- 用户/助手气泡与段落节奏
- 轨迹折叠
- 代码/diff 卡片关联 TaskEvent
- 浏览器截图
- `recon/cursor-reports/B3.md`

## 范围

- 用户/助手气泡：角色、边界、时间/分组；
- 段落节奏：标题/列表/代码/表格/引用；
- 任务时间线：intent → tool call → result → decision → final；
- 轨迹：默认折叠，不再是一行行文本；
- 代码/diff：结构化卡片，和事件关联；
- 失败/重试：事件化。

## 不做

- 不做全站视觉 token 重做；
- 不做移动端状态面板全案；
- 不改工具协议。

## B3 停止条件

- 396+ passed；
- 真实浏览器截图（1440/390）通过；
- 停下等确认，不进入 B4。

---

# Phase B4：视觉系统与验收

**前置**：B3 完成。  
**输出**：
- token/typography/spacing/radius/elevation
- 顶栏/侧栏/输入条/状态面板
- 移动端 sheet/scrim
- 1440/1024/390、深色/浅色验收
- `recon/cursor-reports/B4.md`

## 要求

- 以 B3 的组件形态为基础，不反向改数据模型；
- 不牺牲信息密度和键盘路径；
- 状态面板移动端用 sheet；
- 视觉验收截图。

---

# 后续队列（不混入 B0–B4）

## C2b：重命名

独立分支 `feat/session-rename`：

- 新增 `/session/rename` 写接口；
- 只读 403；
- `title_source=manual`，Agent 标题不能覆盖；
- 输入校验；
- 侧栏/设置入口。

## P1-15：标题生成与下一轮竞态

- 短超时 / idle 后生成 / 或合进本轮结构化输出；
- 保留种子标题兜底。

## P2：打磨

- 草稿刷新持久化（可选）；
- 失败气泡跨会话保留（可选）；
- 行内代码嵌反引号；
- 复制兜底；
- 手机代码块横向提示；
- 安全区、时间戳、手动主题、reduced-motion、favicon/theme-color、骨架屏；
- `thread.js/status.js` 拆分。

---

# 停止规则

以下情况必须停：

- 测试失败两次以上；
- 需要新增接口/改变协议但不在 Phase 范围；
- B0 数据不足以决定 B1 字段；
- B2 迁移工具出现行为差异；
- B3 需要先改回数据模型。

报告里写清：已完成、阻塞点、最小复现、下一步建议。
