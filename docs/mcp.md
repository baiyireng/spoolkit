# 让别的 agent 用它（MCP）

用途：市面上的 agent（Codex、Claude Code、Cursor 等）已经会说话，缺的是一个
它们能调的"手"。把它们接到这个服务上，它就能**扮演用户下发编排任务**，
由这个 agent 在工作区里实施，再把事件与结论交回去。

## 挂上去

在对方 agent 的 MCP 配置里加一条（以 Codex / Claude Code 的 stdio 形式为例）：

```json
{
  "mcpServers": {
    "agents-dev": {
      "command": "agents-dev",
      "args": ["mcp", "--root", "D:\\你的项目", "--scope", "src", "--policy", "auto"]
    }
  }
}
```

`--root` 是工作区；`--scope` 是允许**自动落盘**的范围（越界照样退回确认）；
`--policy` 决定子进程的授权策略。供应商/地址走用户级配置（`agents-dev config`），
也可以在 args 里给 `--provider` / `--base-url`。

## 信任模型

**外部 agent 在这个协议里扮演用户**：`confirm_changes(apply=true)` 等于用户
按了"应用"。所以边界不设在协议层，设在作用域上——子进程仍旧按 `--policy`
与 `--scope` 跑，越界一律退回确认。不放心就：

- 只让它读（不给它调 `confirm_changes` 的机会，或把 `--policy` 设成 `ask`
  并在你这边人工确认）；
- 或者把 `--scope` 收窄到具体目录。

## 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `delegate_task` | `goal`（必填）、`mode`（`task`/`autonomous`/`plan`） | 下发任务，**立刻**返回 task_id——长任务不会阻塞这次调用 |
| `task_status` | — | 快照：是否在跑、结局、用量、待确认改动、`last_seq` |
| `task_events` | `since` | 按序号拉事件（step / tool / diff / await / final…），首次传 0 |
| `confirm_changes` | `apply`（布尔） | 回答审批请求：落盘或丢弃 |
| `workspace_status` | — | 计划进度、工作区默认策略、当前供应商配置 |

## 一次典型的调用序列

```
→ initialize            （协议握手）
→ tools/list
→ tools/call delegate_task {"goal": "把 43_nested_get 修好，验收是跑 pytest 通过"}
← {"task_id": 1, "status": "running"}
→ tools/call task_events {"since": 0}      ← 轮询，直到出现 final 或 await
← {"events": [...], "last_seq": 12}
→ tools/call task_status                   ← 看结局与是否要审批
← {"finished": true, "final": {...}, "diffs": [...]}
→ tools/call confirm_changes {"apply": true}   ← 只在你看过 diffs 之后
```

## 为什么是异步的

MCP 的一次 `tools/call` 是请求/响应，而这里的任务可以跑几十分钟。做成同步，
调用方不是干等就是超时——两样都不对。所以下发立刻返回，进度靠
`task_status` / `task_events` 轮询；审批靠 `confirm_changes` 回答。

事件在服务端留最近 400 条（带递增序号），所以驱动者只要说清"上次看到第几条"，
断线重连也不会漏。

**也可以不轮询**：`delegate_task` 的 `_meta.progressToken` 给了之后，服务端会把
每条事件折成一句进度通知推回来——

```json
{"method": "notifications/progress",
 "params": {"progressToken": "tok-1", "progress": 3, "message": "工具 read_file → 成功"}}
```

（没给令牌就不推：没要进度还一直推，对方只是多收一堆噪音。）

## 为什么用 MCP 而不是自己定协议

对方已经会说 MCP。自己定一套 JSON，等于要求每个接入方先写适配层——
那这件事就不会发生。stdio 上的 JSON-RPC 2.0 只用到 `initialize` /
`tools/list` / `tools/call` 三个方法，实现没有依赖，也不需要联网。

---

# 反过来：让它也能调别的 MCP 服务

上面是"别人调我们"。反方向同样有用：文件系统、浏览器、数据库、公司内部系统
——市面上已经有现成的 MCP 服务，没必要每个都自己写一遍工具。

## 怎么配

写进用户配置（`agents-dev config --path` 打印的就是它）：

```toml
[[mcp]]
name = "filesystem"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", 'D:\work']
```

> **Windows 路径注意**：TOML 里反斜杠要写双份（`"D:\\work"`），或者用单引号
> 字面字符串（`'D:\work'`）。写错一个反斜杠整份配置都读不出来——所以
> `agents-dev config` / `mcp-servers` 现在会把解析错误直接说出来，而不是
> 假装"没有配置"。

配好之后：

```powershell
agents-dev mcp-servers --check     # 真连一遍：连得上吗、提供哪些工具、挂成什么名字
agents-dev run --goal "..."        # 正常跑就是，外挂工具自动在工具表里
agents-dev run --no-mcp --goal "..."   # 怀疑是外挂在捣乱时，临时排除
```

## 挂进来之后长什么样

- 名字带出处：`mcp__<服务名>__<工具名>`（照 Claude Code 那套惯例）。工具重名
  是迟早的事，名字里有出处才一眼看得出是谁提供的。
- 在工具索引里单独一组「外挂」，模型按名字+一句说明就能找到它；
  `tool_help("mcp__filesystem__read_text_file")` 看完整参数。
- **只给主循环**。角色裁剪按白名单走，审查者这类只读角色拿不到外挂工具——
  外部工具的副作用范围我们无从判断，让它出现在只读角色里迟早出事。
- 连不上就**跳过并说出来**（`外部工具 X 连不上：…（已跳过）`），不让它挡住
  整次运行，但也不悄悄降级——悄悄降级的结果是模型找不到工具开始自己造轮子。

## 信任边界（和上面不一样）

外挂工具**不在本项目的工作区授权模型里**。workspace 的读写闸门、命令白名单
管的都是它自己的手；外挂服务自己有什么权限，由那个服务决定。
所以挂载本身就是一次授权决定：**只挂你信得过的服务**，需要收紧就在服务那一侧
收紧（例如文件系统服务只挂某个目录）。
