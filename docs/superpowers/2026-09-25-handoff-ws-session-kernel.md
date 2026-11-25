# 交接：工作区与会话内核（批次一施工中）

日期：2026-09-25　接手方：任何 agent（DeepSeek harness / Cursor / Codex / 人）
状态：**批次一 8 个任务，已完成 2 个（都 review clean），第 3 个未开工**

本文按仓库惯例**不进版本库**（见 `7c49891 chore: 内部交接备忘不再进仓库`）。同类前例：`docs/superpowers/2026-09-24-handoff-heartbeat-weft-t5.md`。

---

## 1. 你要接手的是什么

给 spoolkit（命令行 `spool`）加一层**工作区/会话内核**：让"在哪个工作区、哪条会话里干活"从**启动参数**变成**运行时状态**，四个入口（CLI / 聊天通道 / 网页壳 / MCP）都改成它的客户端；放行从工作区级升到身份级（换工作区不再重新配对）。

- 设计：`docs/superpowers/specs/2026-09-24-workspace-session-kernel-design.md`
- 计划（8 个任务，含可直接抄的代码）：`docs/superpowers/plans/2026-09-24-workspace-session-kernel.md`

别搞混：**批次一**（内核 + 入口接线）就是那份计划；**批次二**（网页壳换成 ChatGPT 那样对话面）**还没写计划**，等批次一落地再按设计文档 §5.3 另写。

## 2. 东西都在哪

```
仓库      D:\workSpace\agents_dev                 主 checkout，在 main（745934c）
施工处    D:\workSpace\agents_dev\.worktrees\ws-session-kernel
          分支 feat/ws-session-kernel，HEAD 647cb10
账本      D:\workSpace\agents_dev\.superpowers\sdd\2026-09-24-workspace-session-kernel\progress.md
任务书    …\task-1-brief.md / task-2-brief.md / task-3-brief.md（从计划抽出来的）
报告      …\task-2-report.md（task-1-report.md 缺失，见 §7）
diff 包   <worktree>\.superpowers\sdd\2026-09-24-workspace-session-kernel\review-*.diff
```

已提交（**全部只在 worktree 分支上，main 一行没动**）：

```
647cb10 feat(kernel): 客户端指向与唯一解析入口 resolve_target    ← Task 2，评审 clean
575ccf0 docs(dogfooding): Task 1 落地记录（内核底座 + 命名层）    ← Task 1 收尾
7cb2e1c feat(kernel): 工作区登记与命名（别名/序号/显示名）         ← Task 1
745934c docs: 工作区与会话内核（批次一）实施计划                   ← 基线，也是 main
```

产物：`src/spoolkit/kernel/{__init__,jsonstore,workspaces,pointers,target}.py`、`tests/kernel/test_{jsonstore,workspaces,pointers,target}.py`、`workspace.py`（已变转出壳）。

## 3. 怎么跑（每条命令都能踩坑，照抄）

```powershell
$env:PYTHONIOENCODING='utf-8'
# ① PYTHONPATH：本仓库是 editable 装到**主 checkout** 的。不设这条，在 worktree 里跑的
#    测试 import 到的是 main 那份 spoolkit——测的是别人的代码，而且会全绿。
$env:PYTHONPATH='D:\workSpace\agents_dev\.worktrees\ws-session-kernel\src'
cd D:\workSpace\agents_dev\.worktrees\ws-session-kernel
# ② 先验证指路生效，再跑测试
& 'D:\workSpace\agents_dev\.venv\Scripts\python.exe' -c "import spoolkit; print(spoolkit.__file__)"
& 'D:\workSpace\agents_dev\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests\kernel
```

另外两坑：

- **别用 `--basetemp="$env:TEMP\..."`**：这台机器的沙箱里会 `PermissionError`，1397 条用例全部 error（看着像天塌了，其实只是临时目录写不进去）。要么不加，要么放进仓库内（`--basetemp=.tmp\pytest`）。
- **沙箱里 `.git` 只读**：`git add/commit/worktree` 要提权（`require_escalated`）才跑得动。

**全套基线**：`1423 passed / 3 failed`。那 3 条是既有的、与本次改动无关的沙箱失败——`tests/cli/test_service_command.py`×2、`tests/tools/test_js_verify.py`×1（沙箱禁止起子进程，`node` 又解析到 Codex 运行时目录）。**除此之外出现任何失败都算回归。**

## 4. 下一件事：Task 3（会话登记）

任务书：`.superpowers\sdd\2026-09-24-workspace-session-kernel\task-3-brief.md`（359 行，含完整代码）。产出：新增 `src/spoolkit/kernel/sessions.py` 与 `tests/kernel/test_sessions.py`，BASE `647cb10`。

已定口径（别自由发挥）：

- 标题与归档标记写 `.agent/sessions/<id>/meta.json`；**`memory.db` 表结构一个字不改**（那库里有真数据）。
- `create_session` 同时 `record_session(...)` 落一行到 `memory.db`，新会话才会立刻出现在列表里。
- `resolve_session` 遇到不认识的名字**报错并列出候选，不许偷偷新建**。
- 复用现成的：`session_state.safe/dir_for`、`memory.store.{init_memory_schema,list_sessions,record_session}`、`store.db.open_db`。
- **只动那两个文件**：`cli/commands/session.py` 是 Task 5 的地盘，改了会撞车。

后续（4 放行 / 5 CLI / 6 聊天通道 / 7 MCP / 8 端到端+文档）按计划顺序做；每做完一个都用下面的命令抽下一份任务书，**别让实现者读整份计划**。

## 5. 施工纪律（这个仓库的规矩）

1. 注释与文档用中文，标识符用英文；**不加新依赖**。
2. 每个改动带测试；提交前跑全套（§3 那条，约 2 分钟）。
3. **只 stage 本次改的文件，禁止 `git add -A`**（工作区里有别人的未跟踪文件）。
4. 改完在 `docs/dogfooding.md` 补一条：做了什么、量到什么、**哪条被证伪**。
5. `tests/test_workspace_root.py`、`tests/test_rename_compat.py` **一条都不许改**——它们是"老行为没变"的凭据。
6. 提交信息写清"为什么"（这个仓库的 commit message 普遍是长篇解释）。

## 6. 评审怎么走（实现者不审自己的活）

```powershell
# PowerShell 5.1 会把 UTF-8 的 .ps1 读成乱码，必须这样调（直接 -File 会报语法错）
$sb = [scriptblock]::Create((Get-Content -Raw -Encoding UTF8 'D:\workSpace\agents_dev\.superpowers\sdd\tools.ps1'))
& $sb -Action brief   -Plan docs/superpowers/plans/2026-09-24-workspace-session-kernel.md -N 3
& $sb -Action package -Plan <同上> -Base <派活前的 HEAD> -Head <做完的 HEAD>   # 在 worktree 里跑
```

评审者要同时给**两个结论**：规格合规（✅/❌）与任务质量（通过 / 问题清单，按 Critical / Important / Minor 分级）。Minor 不进修复轮，记进账本延后处理；Critical/Important 进修复轮（最多 5 轮，前 3 轮找原实现者，第 4 轮起换更强的模型）。

## 7. 已经踩过、别重踩

| 坑 | 现象 | 处理 |
|---|---|---|
| **派活通道不稳** | 代理回一句"你想让我做什么 / I'm ready to help" = 指令根本没送进去 | 已出现 **3 次**（`task1_review`、`task3_impl`、`task3_impl_r2`），成功 5 次。**别靠重派碰运气**：实现类任务改成本会话内直接做（计划里带着完整代码，那是转录 + 跑绿），评审再派并当场核对它有没有开工 |
| **brief 样例可能有笔误** | Task 1 的 `resolve_workspace` 按显示名 `item.name` 匹配，重名时敲 `app` 永远匹配不上、自带用例必挂；Task 2 的 `items[1] == second` 与"最近使用在前"矛盾 | 两处**都已在计划文本里改正并加注释**。再遇到"实现者说 brief 错了"，交评审判，别自己拍板 |
| **报告没落成文件** | Task 1 的报告只在返回里，评审读不到 | 派活时必须写明报告路径；Task 2 起已守住 |
| **`resolve_root` 的交互被摘掉了** | 新壳只在显式传 `choose`/`interactive` 时才弹选单，而 CLI 是"什么都不传" | **Task 5 必须把交互 chooser 在 CLI 层接回来**，否则登记表有多个工作区时，终端从"弹选单"变成"报错退 2"——真回归 |

## 8. 待还的债（账本里有明细）

- Task 1：列表显示名 `left/app` 是单向的（敲回去解析不了）→ Task 5 的 CLI 提示别指一个敲不回去的名字。
- Task 1：`jsonstore.read_json` 的 docstring 说"不是 JSON 对象一律给默认值"，实际非对象 JSON 会原样返回（调用方有 `isinstance` 兜住）。
- Task 1：计划接口清单写 `set_alias(root_or_name, alias)`，实现按 `set_alias(root, alias)`（与设计 §4.1 一致）。
- Task 2：**设计文档 §4.1 的指针接口草图（`get -> Target | None` / `set_pointer`）与最终实现（`get -> Pointer | None` / `set`）不一致** → Task 8 回写文档时对齐。

## 9. 环境事实

- `spool` 已装成全局命令，**editable 指向主 checkout**（`D:\workSpace\agents_dev`），所以 worktree 里跑测试必须设 `PYTHONPATH`。
- **llama-server 现在是停的**；启动命令在 `docs/roadmap.md` 第 0 节（该文件是 git-ignored 的本地备忘）。
- 全局登记表里有两个工作区（`D:\workSpace\agentWorkSpace`、`D:\workSpace\agents_dev`）；状态目录 `%APPDATA%\spoolkit\`（`SPOOLKIT_STATE` 可覆盖，测试靠它隔离）。
- 凭据（QQ / Tavily / Gemini）在 `D:\workSpace\agentWorkSpace\.env`。

## 10. 做完怎么收

8 个任务全 review clean 后，跑最终的全分支评审（MERGE_BASE 用 `git merge-base main HEAD`），把账本里所有 `minor (deferred)` / `parked` 交它裁定哪些必须合并前修。然后**由人决定**怎么并回 main（本仓库纪律：未要求不推远程）。批次二在并回去之后单独立计划。
