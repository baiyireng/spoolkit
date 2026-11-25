# spool web 壳 UX 审阅（当前版本）

- 审阅对象：`spool web` 网页壳（`http://127.0.0.1:8765/`），当前工作树包含会话标题、归档收敛与侧栏展示改造。
- 审阅方式：无头 Edge 实际渲染 + DOM 量测（元素尺寸、重叠面积、对比度、可聚焦性）；覆盖 1440×900、1920×1080、1024×768、390×844，深色/浅色，空态/对话/状态面板/通知/待确认/命令授权/断线/抽屉/长路径/多行输入。
- 源码基线：`src/spoolkit/web/page.py`、`src/spoolkit/web/server.py`、`src/spoolkit/web/titles.py`、`src/spoolkit/kernel/sessions.py`。
- 截图目录：`recon/shots/`；原始量测：`recon/audit.json`、`recon/audit2.json`。
- 说明：diff、命令授权、错误、长路径、长输入等状态为审阅时注入的模拟数据；空态、正常页、状态区为真实运行状态。

> **2026-10-21 UX M1/M1.5/M2-A/M2-B/M2-C1 已落地**：P0-1～4（1024 浮钮/移动断线/面板盖通知/侧栏键盘）已修，
> P1-7～9、P1-11（对比度/触控/ARIA/浅色气泡）已修；M1.5 补真实 Chrome Tab 链路、
> `#st-close` DOM 首位、抽屉动画 pointer-events/inert；M2-A 补草稿隔离/失败恢复/重试；
> M2-B 补 diff 折叠/限高、命令卡分开、同帧组合审批；M2-C1 补 Markdown + 空态。
> `web_ci.py` **393 passed**。下面 P0/P1 对应小节保留作**验收证据与回归依据**，不再表示当前未修。
> **路线调整（2026-10-21）**：对话流视觉（气泡/段落/轨迹/代码/diff）不再按纯视觉
> M3-C 推进；它与工具结果契约、任务事件模型合并，先做 B0 测量/盘点，再做 B1–B3。
> 详见 [`task-flow-tool-contract-plan.md`](task-flow-tool-contract-plan.md)。
> C2a（顶栏/搜索/临时目录）是独立壳层任务，可并行；C2b、P1-15、P2 后置。

## 一句话结论

骨架已经够用，**不要再从“重做视觉”开始**。下一阶段应围绕 Agent 体验做三件事：

1. **长任务可控**：运行中始终能停止；浮层不能盖住输入条/停止键。
2. **失败可恢复**：发送失败不丢输入、不留下“像已发送”的气泡；错误可定位、可重试。
3. **输出可读、会话可认**：Markdown 渲染、标题/归档/搜索、确认卡片可读。
4. **工程化先行**：先把 `page.py` 的 3400 行大字符串拆成静态资源 + 模块，否则后续每个 UX 修复都会继续推高维护/ AI 成本。

## 本轮已落地（不再作为待修项）

| 项 | 当前行为 | 证据 |
|---|---|---|
| 归档默认不展示 | 左栏不再出现“已移除（N）”；页脚「设置」只露数量，列表在设置弹层里 | `shots/new2-desktop.png`、`shots/new2-mobile-settings.png` |
| 归档可恢复 | 设置 → 已归档会话：标题、id、恢复；只读壳禁用恢复 | 同上；`page.py:1292` `renderArchived` |
| 会话标题由 Agent 生成 | `/run` 先写种子标题；final 后 `POST /session/title` 让模型起 4–18 字标题；失败保留种子 | `server.py:514` `generate_title`；`titles.py` |
| 手工标题不被覆盖 | `title_source=manual`；只有 seed/agent 可被 Agent 覆盖 | `sessions.py:386` `set_agent_title` |
| 标题优先、id 次之 | 会话行两行：标题一行，id 小字一行；顶栏/页脚也用标题 | `page.py:348` `.nm-wrap` |
| 当前会话补全 | `cli` 等不在 `state.sessions` 的当前会话也会补进左栏并高亮 | `page.py:2571` `sessions.unshift` |
| 新建会话不再 prompt | 直接 `POST /session/new {}`，服务端自动生成 id；标题等首轮后生成 | `page.py:3432` `newSession` |
| 重复“工作区”标题 | 已去掉列表段内第二份标题，只保留 `.side-head` 一份 | `page.py:2456` 附近 |
| 中文输入法 | **按产品决定保留现状**：Enter 发送 / Shift+Enter 换行不变；不列入本轮改造 | — |

> 这些行为在报告后续条目中不再重复列为问题；行为细节与测试见
> [`web-shell-change-notes.md`](web-shell-change-notes.md) 与
> `tests/web/test_session_titles.py`。

---

# 一、仍影响体验的问题

## P0：先修

> 2026-10-20：P0-1～4 已在 UX M1 修复，以下保留为问题复现与验收记录。

### 1. 1024 宽屏下浮钮压住输入框，甚至盖住停止键

- 实测 1024×768：`#thinking` 与输入框重叠 **96×37px**；`#toBottom` 与输入框重叠 **38×38px**，并压住 36×36 的停止键。
- 截图：`shots/18-1024-composer.png`、`shots/18-crop.png`。
- 根因：`#thinking`/`#toBottom` 用 `position:absolute; right:22px; bottom:104px/64px`，而输入条高度随多行输入增长；媒体查询只在 `max-width:860px` 调整这两个数（[page.py:572](src/spoolkit/web/page.py#L572)、[page.py:590](src/spoolkit/web/page.py#L590)、[page.py:688](src/spoolkit/web/page.py#L688)）。
- 影响：运行中正是最需要点“停止”的时候，停止键被“回到最新”盖住。这是 Agent 长任务体验里最不能接受的一类。
- 建议：浮钮不要相对视口定位。放进输入条上方的 flex 行，或 `#thread` 内一个不滚动的 footer；用 `ResizeObserver` 读输入条实际高度也比写死 `bottom` 稳。验收：390/1024/1440 三档下浮钮与 `.box` 重叠面积为 0，停止键 44×44 可点。

### 2. 移动端断线横幅遮住输入框并拦截点击

- 实测 390×844：`#conn` 与 `.box` 重叠 **186×21px**；`elementFromPoint(200,815)` 返回 `#conn`，`pointer-events:auto`。
- 截图：`shots/19-mobile-disconnected.png`。
- 根因：`#conn` 固定 `bottom:10px; z-index:20`（[page.py:658](src/spoolkit/web/page.py#L658)），输入条也贴底。
- 影响：断线时更难重连/输入；用户越急越点不到输入框。
- 建议：做成输入条上方状态行（进入文档流），或至少 `bottom: calc(输入条高度 + 8px)` + `pointer-events:none`；同时补 `role="alert"` / `aria-live="assertive"`。

### 3. 状态详情面板会完整盖住通知

- 实测 390×844：`#st-panel` 与 `#toasts` 重叠 **370×270px**；打开面板后，刚弹出的错误通知完全消失。
- 截图：`shots/12-mobile-overlays.png`。
- 根因：`#toasts` 是 `#hints` 的兄弟，而 `#st-panel` 是 `#hints` 内绝对定位层（[page.py:293](src/spoolkit/web/page.py#L293)、[page.py:645](src/spoolkit/web/page.py#L645)）。
- 影响：错误被“详情”吃掉；用户以为没报错。面板开着时新错误也会被盖住。
- 建议：通知与面板放进同一个纵向 flex 容器，面板展开时把通知往下推；或面板打开时收起通知并显示“有 N 条新通知”。无论哪种，**通知永远不能被盖住**。面板同时加显式关闭键。

### 4. 侧栏行不可键盘操作，隐藏 select 成为键盘陷阱

- 可见行是 `DIV`，`role=null`、`tabIndex=-1`，只绑了 `onclick`（[page.py:2343](src/spoolkit/web/page.py#L2343)）。
- 真正的切目标入口是 `.sr-only` 里的两个原生 `select`，视觉上被 `clip-path` 裁掉，但 DOM 尺寸是 **627×24 / 133×24**，仍可 Tab 聚焦。
- 首屏 Tab 顺序实测：右上角 24×14 的探活点 → “新建会话” → 不可见的工作区 select → 不可见的会话 select → 输入框。
- 影响：键盘/读屏用户无法可靠地选工作区或会话，且可能在看不见的控件里改变目标。
- 建议二选一：
  1. 可见行改成真正的 `button`（或 `role="button"` + `tabIndex=0` + Enter/Space + `:focus-visible`），移除隐藏 select；
  2. 保留原生 select，但让它们可见、可用。
- 这条与 Agent 体验直接相关：键盘用户需要“切会话 → 输入 → 发送 → 停止”全程可达。

## P1：明显影响 Agent 体验

> 2026-10-20：P1-7（对比度）、P1-8（触控）、P1-9（ARIA/通知栈）、P1-11（浅色气泡）
> 已在 UX M1 修复；其余 P1 仍待处理。

### 5. 空态仍是整块空白

- 实测：有目标、无历史时 `#stream .col` 高度为 0；主区从顶栏到输入条之间什么都没有（`shots/01-desktop-empty.png`、`shots/10-mobile-empty.png`）。
- 影响：第一次打开不知道能做什么；空屏像没加载出来；未选工作区时的引导藏在右上角小点里。
- 建议：欢迎语 + 2–4 个示例任务 + 当前工作区/会话摘要 + “新建会话”；`/state` 慢时给骨架屏。
- Agent 体验角度：空态应该回答“我现在能指挥它做什么”，而不只是好看。

### 6. 长路径撑高顶栏，状态点压字

- 实测 390×844：正常顶栏 51px；长路径时 60px，而 `#corner` 固定 `top:52px`，重叠 **8×370px**；绿色状态点正好压在路径第二行（`shots/17-mobile-long-path.png`）。
- `#where { word-break:break-all }` 还会把英文/路径从词中间劈开（`nested` → `n`+`ested`）。
- 建议：顶栏固定一行，省略号 + `title` 放完整路径；只显示工作区名 + 会话标题，路径进详情。`#corner` 的 `top` 不要写死 52，至少加 `env(safe-area-inset-top)`；最好把状态点放回顶栏参与布局。

### 7. 对比度不足（实测）

WCAG AA 普通文本 4.5:1，UI 控件 3:1。

| 选择器 | 实测 | 字号 | 问题 |
|---|---:|---:|---|
| `.ask-apply`（应用按钮） | **2.51:1** | 13.5px | 白字压 `#6fb36f` 绿底，明显发灰 |
| `.st-hint`（Enter 提示） | **3.20:1** | 11.5px | 右下角几乎看不见 |
| `.row-act`（移除/恢复） | **3.57:1**（浅色 3.07） | 11.5px | 破坏性操作却很淡 |
| `#goal::placeholder` | **4.22:1**（浅色 4.19） | 15px | 输入框占位文字偏淡 |

- 建议：`.ask-apply` 改深字或深绿；`.st-hint`/`.row-act` 用 `--text-dim`；给 placeholder 显式颜色；浅色主题同样复查 `--text-faint`。
- 影响：确认写入/执行是高风险动作，却用了全页最难读的文本。

### 8. 触摸目标过小

实测 390×844：

| 控件 | 尺寸 | 备注 |
|---|---:|---|
| `#st-health` 探活点 | **24×14** | 页面第一个可聚焦控件，也是移动端唯一模型状态入口 |
| `#menu` | 34×34 | — |
| `#send` | 36×36 | — |
| `.toast-close` | 24×24 | 每条通知的关闭键 |
| `.row-act` | 39×22 | “移除”，破坏性操作 |
| `.ask-apply` / `.ask-reject` | 59×36 | 待确认主操作 |
| `#toast-all` | 74×29 | “全部关闭” |

- 建议：移动端至少 44×44 点击区（padding 扩展即可）；桌面 ≥24×24。探活点可做成 32–36px 圆钮或放进顶栏。

### 9. 动态通知/断线没有 aria-live；最新通知还会在可视区外

- `#toasts` 无 `role`/`aria-live`；`#conn` 无；`#stream` 无 `role="log"`；`#thinking` 无 `role="status"`；`#goal` 无 label（`goalLabel=0`、`goalAria=null`）。
- 4 条错误时：`#toasts` 可视 380px、内容 701px，第 4 条 y=611，**不在可视区**；容器不自动滚到最新（`shots/23-mobile-toast-stack.png`）。
- 建议：`#toasts` 加 `role="status" aria-live="polite"`（错误可单独 `role="alert"`）；`#conn` `role="alert"`；`#thinking` `role="status"`；`#goal` 加 label；追加通知后滚到底或把最新插到顶部；加上限（如 3 条 + “还有 N 条”）。

### 10. Markdown 原样显示

- `fillRich` 只识别围栏代码、行内代码、普通正文（[page.py:1482](src/spoolkit/web/page.py#L1482)）；`appendProse` 只按反引号切分。
- 实测：`## 结论`、`- 列表`、`**注意**`、`[链接](...)`、表格都显示原始符号（`shots/21-markdown-raw.png`）。
- 影响：这是 coding agent 的主输出形态；长答案很难扫读。
- 建议：至少支持标题、无序/有序列表、粗体、链接、表格；用 DOM API 构建，不要 `innerHTML` 拼模型输出。

### 11. 浅色主题助手气泡与页面同色

- 浅色下 `--bubble-assistant:#fff`，而 `--bg:#fff`；助手气泡只剩 1px `--line-soft:#ececec` 边框（[page.py:148](src/spoolkit/web/page.py#L148)、[page.py:392](src/spoolkit/web/page.py#L392)）。
- 截图：`shots/22-light-conversation.png`。
- 建议：浅色助手气泡改 `#f7f7f8` 或加明显边框/阴影，保持与深色主题相同的层次逻辑。

### 12. 切换会话草稿不隔离；发送失败丢输入

- `resetView()` 清空对话流，但没有动 `#goal`（[page.py:2682](src/spoolkit/web/page.py#L2682)）；`switchTarget` 直接 `applyState`。
- `say()` 在发请求前就 `goalBox.value=''`（[page.py:3443](src/spoolkit/web/page.py#L3443)）；`post()` 失败只追加错误 + 通知，不恢复输入，也不把乐观用户气泡标成失败。
- 影响：A 会话写一半，切到 B 再按 Enter 会发错；长任务发送失败要重新输入，气泡还像“已发送”。
- 建议：按“工作区|会话”保存草稿（`Map`），切换时恢复；POST 成功后再清空，失败恢复草稿 + 标记失败气泡 + 重试。

### 13. diff 全量展开；命令授权仍写“应用/拒绝”

- `showPending` 把每个文件完整 diff 塞进卡片；`.diff` 只有横向滚动，没有最大高度/折叠（[page.py:499](src/spoolkit/web/page.py#L499) 附近）。
- 命令授权复用待确认卡片，按钮文案仍是“应用/拒绝”，并写“点「应用」仅允许这一次”（`shots/07-command-ask.png`）。
- 影响：大 diff 把输入条推远；命令授权看起来像普通文件写入，没有“要执行命令”的危险感。
- 建议：diff 按文件折叠、整体限高、逐文件勾选（接口允许时）；命令授权单独一档：标题“要执行一条命令”、按钮“允许执行/拒绝”、warning 边框/图标。

### 14. 移动端抽屉没有关闭键；Esc 关不掉；焦点能跑到遮罩后

- 打开抽屉后 `#menu` 被抽屉盖住（`elementFromPoint(20,20)` 返回 `new-session`）；Esc 后 `body.drawer` 仍为 true；`#menu` 没有 `aria-expanded`/`aria-controls`。
- 截图：`shots/11-mobile-drawer.png`（旧结构，问题仍在）；设置弹层已单独有焦点管理，抽屉还没有。
- 建议：抽屉顶部加关闭键；Esc 关闭；打开时焦点进抽屉、`aria-expanded=true`、`aria-controls`；背景 `inert`/`aria-hidden`；关闭后焦点回菜单键。

### 15. Agent 标题生成会与下一轮抢模型

- 标题由 `POST /session/title` 在 final 后同步调用模型（[server.py:514](src/spoolkit/web/server.py#L514)、[titles.py](src/spoolkit/web/titles.py)）。
- 当前本地 llama.cpp 配的是 `-np 1`（单槽）。如果用户 final 后立刻发下一轮，标题请求和下一轮可能抢同一个槽，最长可让下一轮多等一个标题请求的超时时间（`TitleNamer` 12s）。
- 建议：
  - 标题任务只在 `runner.running == false` 且短暂 idle 后启动（当前只在 running 时拒绝，仍可能和下一轮竞态）；
  - 或者把命名合进本轮 agent 的结构化输出（少一次调用）；
  - 或给标题请求更短超时（3–5s），超时保留种子标题。
- 这是“Agent 体验”层面的隐性成本；UI 上看不见，但用户会感觉“下一轮怎么慢了”。

### 16. 顶栏仍是“绝对路径 + 会话”，缺产品感

- 顶栏现在显示完整路径 + 会话标题（`page.py` `whereBox`），标题已经优先，但路径仍占最宝贵的一行；长路径会换行。
- 建议：主标题用工作区名 + 会话标题；完整路径放 `title`/状态面板；加一个 `spool` 小标识/图标。Agent 产品需要“项目/会话”这一层语义，而不是盘符。

### 17. 没有重命名、搜索、筛选

- 新建会话不再提示名字，但 web 上没有重命名入口；名字只能来自 Agent 或 CLI。
- 工作区/会话多时没有搜索/筛选/折叠；临时目录（`spool-chat-*`、`tmp*`）会一直占左栏。
- 建议：设置里或行 hover 加“重命名”；侧栏加搜索/隐藏临时目录/只显示最近；section header 可 sticky。
- 这属于“会话组织”而不是视觉细节；Agent 体验需要人能快速找回某次任务。

## P2：打磨项

- **输入条与消息列宽度不一致**：`.col` 760px、`.box` 800px；1440 下左右各错 20px。建议统一变量。
- **浮钮离消息列太远**：`#thinking`/`#toBottom` 锚视口右侧，1440 下离列右缘约 100px，1920 更远。建议锚到列/输入条。
- **安全区**：`viewport-fit=cover` 已开，但只有 `#composer` 处理了 `env(safe-area-inset-bottom)`；`#top`、抽屉、`#corner` 没有顶部安全区。刘海屏/Home Indicator 需要补。
- **代码复制失败没有兜底**：`copyText` 注释说“失败就选中代码”，实现只改按钮文案（`page.py` `copyText`）。非安全上下文（局域网 http）`navigator.clipboard` 不可用时，用户只能自己选。建议临时 textarea + `execCommand` 或真正选中。
- **流式代码块在手机上横向滚动没有提示**：`shots/16-mobile-conversation.png` 右侧被截断；可加渐隐提示或“自动换行”开关。
- **失败块较长**：移动端失败说明约 220px 高且不可折叠；可加“展开详情/复制诊断信息”。
- **没有消息时间/日期分隔**：长会话难定位；建议日期分隔或时间戳。
- **没有手动主题切换**：只跟随系统；可加三态开关。
- **`prefers-reduced-motion` 没覆盖抽屉的 transform 过渡**。
- **没有 favicon / `theme-color`**：标签页与移动端地址栏缺少品牌色。
- **首屏没有骨架屏**：`/state` 慢时只有“连接中”和空白。
- **浅色主题 `--text-faint` 仍偏淡**：`#8f8f8f` 对 `#f9f9f9` 约 3.1–3.2:1。
- **错误同时出现在对话流和通知**：`post()` / `finishLive()` 失败会两边都画；可保留记录但让通知带“查看”定位，减少重复。
- **移除会话的二次确认会一直保持待确认**：`armed` 没有点击别处/超时复位；建议 5–8 秒复位或真正确认弹层。
- **标题来源不可见**：用户不知道标题是 Agent 起的还是自己改的；可在设置/行 hover 加“自动命名”提示，但不影响主流程。

---

# 二、面向 Agent 体验的改造主题

如果下一阶段的目标是“Agent 体验”，建议按主题而不是按页面拆：

### A. 任务生命周期：开始 → 进度 → 停止 → 恢复

- 停止键必须在所有宽度、所有输入高度下可点；浮钮不能覆盖输入条/停止键（P0-1）。
- 运行中显示“正在做什么”可放进实时卡片/状态区，不要让“生成中”浮在一角。
- 停止后明确说“已产出的内容保留”，并保留可继续的上下文（当前已有部分文案，需保证浮层不挡）。

### B. 失败可恢复

- 发送失败：恢复草稿、失败气泡可重试、错误块可折叠（P1-12）。
- 模型失败：通知 + 对话内失败说明已经做得不错；保留“下一步命令”与复制入口。
- 断线：输入条不被挡（P0-2），并明确“重连中”是否可继续输入/发送。

### C. 授权与确认

- 文件 diff 与“命令执行”必须是两种卡片，文案、颜色、危险级别不同（P1-13）。
- 大 diff 可折叠、限高、逐文件选择；确认动作要可读（P1-7）。
- 只读壳的禁用态要能解释“为什么点不动”（现有模式徽标已覆盖一部分）。

### D. 输出可读性

- Markdown 标题/列表/粗体/链接/表格（P1-10）。
- 代码块：语法高亮（可选）、手机横向滚动提示/自动换行、复制兜底（P2）。
- 长回复：默认折叠“思考过程”已有；失败详情、diff 也需要折叠策略。

### E. 会话组织

- 标题优先、id 次之（已落地）；补重命名、搜索、隐藏临时目录（P1-17）。
- 归档默认隐藏、设置里恢复（已落地）；归档数量角标保留。
- 空态/首屏要能回答“我是谁、在哪、能做什么”（P1-5）。

### F. 可达性与移动端

- 键盘全程可达（P0-4）；动态通知/断线可被读屏听到（P1-9）。
- 触摸目标 44×44（P1-8）；安全区（P2）。
- 抽屉/设置/面板的焦点与 Esc 行为统一（P1-14）。

---

# 三、建议里程碑

### M1：稳定性与可达性（先做，低风险）

> 进入 M1 前先做 **前端 Phase 0**（拆 HTML/CSS/JS 外壳、静态资源服务），
> 否则下面每一项都要继续在 `page.py` 的大字符串里改。

1. 1024/窄屏浮钮布局（不再用固定 `bottom`）。
2. 断线横幅进入文档流或避开输入条。
3. 状态面板与通知纵向堆叠；面板加关闭键。
4. 侧栏键盘可达 + 隐藏 select 移除/可见化。
5. 对比度、触摸目标、aria-live/label、最新通知滚动。
6. 浅色助手气泡。

验收：390/1024/1440 无浮层重叠；键盘可完成“切会话→输入→发送→停止”；通知与断线可被读屏播报。

### M2：长任务与失败恢复（Agent 体验核心）

7. 草稿按会话隔离；发送失败恢复 + 重试。
8. diff 折叠/限高 + 命令授权语义与警示样式。
9. 抽屉关闭/Esc/焦点陷阱。
10. 空态引导 + 首屏骨架。
11. 标题生成与下一轮抢模型的规避（短超时/idle 后生成）。

验收：长任务可停、可恢复、可重试；失败不丢输入；确认卡能分清“写文件”与“跑命令”。

### M3：内容与视觉打磨

12. Markdown 渲染。
13. 代码块手机体验、复制兜底。
14. 顶栏产品化、宽度统一、安全区。
15. 会话重命名/搜索/隐藏临时目录。
16. 消息时间、手动主题、favicon/theme-color、reduced-motion。

验收：长回答可扫读；手机端可完成一次完整任务；会话多时能快速找回。

---

# 四、前端工程化改造：已落地与残余风险

> 2026-10-20：Cursor 已实施 Phase 0/1/2/3 的文件级拆分。复核结果见
> [`web-frontend-refactor-review.md`](web-frontend-refactor-review.md)，详细方案见
> [`web-frontend-refactor-plan.md`](web-frontend-refactor-plan.md)。

## 已落地

- `page.py` 现在是 14 行加载器，只导出 `INDEX` / `HTML`；CSS/HTML/JS 在
  `src/spoolkit/web/assets/`。
- 浏览器 `/` 返回 `index.html`，样式/脚本走 `/assets/dist/app.<hash>.{css,js}`；
  `/assets` 有白名单、MIME、ETag、缓存头、路径穿越/符号链接拒绝。
- JS 分成 `core` / `render` / `features` / `main`；`features` 文件之间没有
  `import`，渲染层有 Markdown/code/diff/failure 纯函数和 `node --test`。
- `scripts/check_web_assets.py` 检查行数、文件清单、声明依赖方向、循环依赖、
  CDN、动态 `innerHTML`、dist 漂移；`scripts/web_ci.py` 串起构建、Node 测试、
  esbuild 和 pytest。
- wheel 包含 assets，安装布局能开首页；`HTML` 兼容导出保留给旧测试/嵌入。
- 第三轮已把页面状态收进 `createStore`、feature 改成 `createX(ctx)` 工厂；
  `applyState / switchTarget` 不再直接写状态；`keyFor` 移入 core/format，
  `live/pendingUser/lastDrawn` 收进 thread 闭包。
- 本机回归：`web_ci.py` 全权限 **393 passed**；Node 纯函数 **10 passed**；
  tsc/esbuild/浏览器 smoke 通过。
- 行数：`thread.js` 约 548、`status.js` 约 446、`sidebar.js` 约 394、
  `main.js` 约 328，均低于 600 行硬上限。

## 第三轮后仍剩的工程债

状态所有权和 feature ctx 已经做对，剩下的是“生产环境收口”，不是架构主路：

- **生产包还有测试钩子**：`main.js` 末尾仍有 `this.store = store; this.app = app;`，
  以及一串 `var` 全局函数别名。它们让假 DOM 能拿到 `ctx.store`/`ctx.applyState`，
  但也进入了生产 `dist/app.js`。应把这些暴露全部收进 `test-entry.js`
  （`__spoolTest` / `globalThis`），生产包不再有 `this.store` / `this.app` /
  全局函数名。
- **状态检查只覆盖 8 个名字**：store 实际有约 30 个 key，`check_web_assets.py`
  的 `_STATE_NAMES` 只有 `lastState/running/chosen/session/readonly/busy/stopping/
  stoppedRun`。`pendings/statusOpen/settingsOpen/healthView/...` 仍可能被 feature
  裸用；应让 store 的初始 key 成为唯一来源，检查脚本从它派生。
- **assets 仍未纳入 git**：`web_ci.py` 的 `git diff --exit-code` 对未跟踪文件
  不生效；dist 漂移目前主要靠 `check_web_assets.py` 的字节比对。提交前至少
  `git add src/spoolkit/web/assets`（不 commit）。
- `thread.js` 约 548 / `status.js` 约 446，仍高于建议 400 行；不急，但可继续拆。
- tsc 只覆盖 store/ctx/format/markdown；全量经典脚本类型检查不现实，保持现状即可。
- 浏览器 smoke 使用旧 headless（EventSource 不结束）；已能覆盖真实 `/` +
  `dist/app.js`，暂时够用。
- 自定义 bundler 仍只支持 side-effect import；named/default import 会抛错，
  不会静默进 dist，可接受。

## 下一步（最后一次工程收口，然后转 UX）

1. **删除生产测试钩子**：`main.js` 的 `this.store` / `this.app` 和 `var` 全局
   函数别名移出生产包；所有测试暴露收进 `test-entry.js`（`__spoolTest` /
   `globalThis`），生产 `dist/app.js` 不含 `this.store` / `this.app` /
   `__spoolTest`。`page_dom.js` 改用 `__spoolTest`。
2. **状态检查全量化**：store 初始 key 作为唯一来源，`check_web_assets.py`
   从它派生 `STATE_KEYS`，覆盖全部约 30 个 key，不只 8 个。
3. **assets 纳入 git**：至少 `git add src/spoolkit/web/assets`（不 commit）；
   之后 `web_ci.py` 的 `git diff --exit-code` 才有意义。
4. **可选**：`thread.js`/`status.js` 拆到 <400；tsc 保持当前四个纯文件即可，
   不必全量。
5. **停止工程化**：完成 1–3 后，转 UX M1（浮层/断线/通知/键盘/对比度/触控）。
   不要继续上 ESM、框架或全量类型检查；当前 `createStore + createX(ctx)` 已
   足够支撑后续 AI 改造。

与 UX 的关系：工程主路已经走通，剩下是收口；UX M1 的 P0 仍然是 1024 浮钮、
移动端断线、状态面板盖通知、侧栏键盘可达。这些解决后再做 M2/M3。

---

# 五、视觉打磨（M3）

> 结论：功能体验已到 M2-C1，但界面仍偏“工程师工具的第一版”，需要独立 M3
> 视觉收口；不要靠零散调色。详细计划见
> [`web-visual-polish-plan.md`](web-visual-polish-plan.md)。
>
> 当前截图：[桌面](shots/m3-desktop-current.png)、
> [移动端状态面板](shots/m3-mobile-status.png)。

从截图看，最明显的是：

- 顶栏仍以完整路径为主，390 宽会换行；`#where` 需要单行省略（M2-C2a）。
- 左栏密度高、层级弱：工作区/会话/临时目录/移除按钮权重接近，没有搜索
  （M2-C2a + M3）。
- 助手回复和背景边界弱，长段落缺少节奏；`思考过程` 开关过轻（M3）。
- 状态面板桌面偏大，移动端几乎盖住对话且无 scrim；应做桌面 popover /
  移动端底部 sheet（M3）。
- 输入条容器感弱，字号/间距/圆角/阴影没有体系（M3-A/B）。
- 暗色整体偏平，选中态、浮层、输入区缺少一致的 elevation 语言（M3-A）。

M3 工作包：token/排版 → 壳层 → 对话流 → 状态面板/移动端 → 视觉验收；
不引 UI 框架，不牺牲信息密度和键盘路径。建议顺序：**M2-C2a → M3 → M2-C2b → P1-15 → P2**。

---

# 六、证据清单

## 当前版本（已落地改造）

| 文件 | 内容 |
|---|---|
| `shots/new2-desktop.png` | 桌面：标题两行、id 次级、当前会话高亮、设置数量 |
| `shots/new2-mobile-drawer.png` | 移动端：抽屉、两行会话、设置入口 |
| `shots/new2-mobile-settings.png` | 移动端：设置底部 sheet、已归档会话与恢复 |
| `shots/new-desktop-settings.png` | 桌面设置弹层（两行布局前版本，弹层结构相同） |

## 仍待优化（旧版本截图，相关区域未改动）

| 文件 | 内容 |
|---|---|
| `shots/01-desktop-empty.png` | 桌面空态 |
| `shots/04-status-down.png` | 状态面板 |
| `shots/05-toasts.png` | 通知堆叠 |
| `shots/06-pending-diff.png` | 待确认 diff |
| `shots/07-command-ask.png` | 命令授权 |
| `shots/11-mobile-drawer.png` | 移动端抽屉 |
| `shots/12-mobile-overlays.png` | 状态面板盖住通知 |
| `shots/13-composer-collision.png` | 多行输入 + 浮钮 |
| `shots/16-mobile-conversation.png` | 移动端对话/代码 |
| `shots/17-mobile-long-path.png` | 长路径压状态点 |
| `shots/18-1024-composer.png` | 1024 浮钮压输入框/停止键 |
| `shots/18-crop.png` | 上图的右下角放大 |
| `shots/19-mobile-disconnected.png` | 断线横幅压输入框 |
| `shots/20-mobile-pending.png` | 移动端待确认 |
| `shots/21-markdown-raw.png` | Markdown 原样显示 |
| `shots/22-light-conversation.png` | 浅色助手气泡同色 |
| `shots/23-mobile-toast-stack.png` | 4 条通知时最新一条不可见 |

## 行为回归

```powershell
$env:PYTHONPATH="D:\workSpace\agents_dev\src;D:\workSpace\agents_dev\scripts"
$env:PYTEST_ADDOPTS="-p pytest_sandbox_compat"
python -m pytest -q -p no:cacheprovider -p pytest_sandbox_compat `
  tests/web tests/kernel/test_sessions.py tests/mcp/test_server.py
```

结果：**393 passed**（当前工作树；`python scripts/web_ci.py` 全权限跑出）。
沙箱内单独跑 pytest 可能因 Windows 符号链接权限多一个 skip，不算产品失败。

> 注意：正在运行的 `spool web` 进程是在本轮改造前启动的，HTML 内嵌在 Python 内存里；看到新侧栏/设置/标题逻辑前需要重启一次 `spool web`。
