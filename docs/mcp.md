# 让别的 agent 用它（MCP）

用途：把 Codex / Claude Code / Cursor 这类对话式 agent 接到这里，它即可**扮演用户下发编排任务**，由本 agent 在工作区里实施，再把事件与结论交回。

## 挂上去

在对方 agent 的 MCP 配置里加一条（以 stdio 为例）：

```json
{
  "mcpServers": {
    "spool": {
      "command": "spool",
      "args": ["mcp", "--scope", "src", "--policy", "auto"]
    }
  }
}
```

`--scope` 是允许**自动落盘**的范围（越界照样退回确认）；`--policy` 决定子进程的授权策略；
供应商 / 地址走用户级配置（`spool config`），也可以在 args 里给 `--provider` / `--base-url`。

`--root` **可选**：不给即"服务整台机器"——每次调用可以带 `workspace` 点名一个**已登记**的
工作区（先 `list_workspaces` 查看），不给就用 `mcp` 的指向；给了 `--root` 就把这条 MCP
会话**钉死**在那一个工作区里，此时点名别的工作区会**报错**，不会静默改道——`--root` 是主人给定的
范围限制，必须生效。`--session` 不给就用 `mcp` 的指向，单次调用还能用 `session` 覆盖。

点名一个还不是工作区的目录会报错（登记表里有它不等于能干活）：按名字 / 路径 / 序号三种写法
口径一致，都要求那个目录（或它的某个祖先）有 `.agent/`，并会告诉你去 `spool ws add`。
**工作区只能由人建**：外部 agent 不能自行开辟新的信任边界。没有 `workspace` 而登记表里有多个
工作区时，`delegate_task` 会**报错并要求点名**（列出候选），不会替调用方挑一个。

**fake 不能当生成器**：这条 MCP 需要已配置的 llamacpp 或 gemini。`--provider fake` 只回放
`--script`，没有 LLM 时不要把任务交给本服务当兜底。`workspace_status` 里的 `provider` 若为
`fake`，先改配置再下发。

## 信任模型

**外部 agent 在这个协议里扮演用户**：`confirm_changes(apply=true)` 等于用户按了"应用"。因此
边界不设在协议层而设在作用域上——子进程仍旧按 `--policy` 与 `--scope` 运行，越界一律退回确认。
不放心就只让它读（不给它调 `confirm_changes` 的机会，或 `--policy ask` 由你人工确认），或者把
`--scope` 收窄到具体目录。

## 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `delegate_task` | `goal`（必填）、`workspace`、`session`、`mode`（`task`/`autonomous`/`plan`） | 下发任务，**立即**返回 task_id |
| `task_status` | — | 快照：是否在跑、是否结束、结局、用量、待确认改动、`last_seq`（另带 `workspace`）|
| `task_events` | `since` | 按序号拉事件（step / tool / diff / await / final…），首次传 0 |
| `confirm_changes` | `apply`（布尔） | 回答审批请求：落盘或丢弃 |
| `workspace_status` | — | 计划进度、工作区默认策略、当前供应商配置 |
| `list_workspaces` | — | **只读**：这台机器上登记过的工作区（名字、别名、路径、是否已是工作区）|
| `list_sessions` | `workspace`（可选） | **只读**：某个工作区的会话（id、标题、最近活跃、是否归档）|

一个 MCP 会话里可以有**多个工作区**：每个 (工作区, 会话) 一个 Runner，而 `task_status` /
`task_events` / `confirm_changes` 作用在**最近一次 `delegate_task` 的那个**上。

## 一次典型的调用序列

```
→ initialize            （协议握手）
→ tools/list
→ tools/call list_workspaces                       ← 先看有哪些（只读）
← {"workspaces": [{"name": "proj", "root": "D:\\work", "ready": true}]}
→ tools/call delegate_task {"goal": "…", "workspace": "proj"}
← {"task_id": 1, "workspace": "D:\\work", "status": "running"}
→ tools/call task_events {"since": 0}      ← 轮询，直到出现 final 或 await
← {"events": [...], "last_seq": 12}
→ tools/call task_status                   ← 看结局与是否要审批
← {"finished": true, "final": {...}, "diffs": [...]}
→ tools/call confirm_changes {"apply": true}   ← 只在你看过 diffs 之后
```

协议是 stdio 上的 JSON-RPC 2.0，只用到 `initialize` / `tools/list` / `tools/call`；事件在服务端
留最近 400 条（带递增序号），驱动者只要说明"上次看到第几条"，断线重连也不会漏。也可以不轮询：
`delegate_task` 的 `_meta.progressToken` 给出后，服务端会把每条事件折成一句进度通知推回
（不给则不推，避免对方收到无关噪音）。下发为何异步、为何用 MCP 而不是自定一套协议，
见 [`rationale.md`](rationale.md#异步与协议)。

---

# 反过来：让它也能调别的 MCP 服务

市面上的 MCP 服务（文件系统、浏览器、数据库、公司内部系统）不必逐个重写工具。

## 怎么配

写进用户配置（`spool config --path` 打印的就是它）：

```toml
[[mcp]]
name = "filesystem"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", 'D:\work']
```

**Windows 路径注意**：TOML 中反斜杠要写双份（`"D:\\work"`），或用单引号字面字符串
（`'D:\work'`）。写错一个反斜杠整份配置都读不出来——因此 `spool config` / `mcp-servers` 会把
解析错误直接说出来，而不是当作"没有配置"。

```powershell
spool mcp-servers --check         # 真连一遍：连得上吗、提供哪些工具、挂成什么名字
spool run --no-mcp --goal "..."   # 怀疑是外挂在干扰时，临时排除
```

## 挂进来之后长什么样

- 名字带出处：`mcp__<服务名>__<工具名>`（照 Claude Code 的惯例），在工具索引里单独一组
  「外挂」；`tool_help("mcp__filesystem__read_text_file")` 看完整参数。
- **只给主循环**：角色裁剪按白名单走，审查者这类只读角色拿不到外挂工具——外部工具的副作用
  范围无从判断，放进只读角色会带来风险。
- 连不上就**跳过并说出来**（`外部工具 <名字> 不可用：<原因>`），不让它挡住整次运行，也不静默降级——静默降级的后果是模型找不到工具，转而自行实现。

## 信任边界

外挂工具**不在本项目的工作区授权模型里**：workspace 的读写闸门与命令白名单管的都是本项目自己的工具，外挂服务自身有什么权限由那个服务决定。因此挂载本身就是一次授权决定——**只挂你信得过的服务**，需要收紧就在服务一侧收紧（例如文件系统服务只挂某个目录）。
