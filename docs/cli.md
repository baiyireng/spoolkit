# 命令速查

`spool --help` 是总入口；这一页把每条命令、每个参数讲清（内容与 `--help` 对齐，
改了参数就该回来改这一页）。

**所有带 `--root` 的命令**：默认自动找工作区（显式 → 往上找 `.agent/` → 全局
登记表 → 当前目录），见 [`getting-started.md`](getting-started.md#4-在任意目录起手)。

## 入口

```bash
spool                # 进对话；不是工作区就先问一句要不要初始化（非交互时打帮助）
spool --version      # spool 0.0.1 (spoolkit)
spool --help
```

## spool init

把一个目录做成工作区（幂等）。它就是"建 `.agent/` 并登记"，`spool approve`、
`spool session` 之类因此能在任意目录用。

| 参数 | 说明 |
|---|---|
| `path`（可选） | 目录，默认当前目录 |

已经在某个工作区**里面**时不会再套一层（记忆分叉很难看出来）：只登记，不新建。
目录里有 `.gitignore` 而里面没有 `.agent/` 时，会追加一行。

## spool approve

批准一个聊天通道的配对码，等价于 `spool bridge --approve <码>`。
手机收到码之后随手敲的那一下，所以放在顶层。

| 参数 | 说明 |
|---|---|
| `配对码`（可选） | 聊天里收到的那个码；批准后这个人就能驱动这个工作区 |
| `--root` | 工作区路径（默认自动找） |
| `--list` | 看这个工作区放行了谁、还有哪些码在等 |
| `--revoke <用户>` | 撤销某人的放行（解绑）。他再发消息会重新拿码 |
| `--forget <码>` | 丢掉一个还没批准的码（谁都不放行） |

配对记录挂在**工作区**上（`.agent/bridge-pairings.json`），所以"换绑工作区"
= 用 `spool bridge --root <另一个工作区>` 重启桥，并在那个工作区里重新放行。
在 `spool chat` 里可以就地做同样的事：`/approve <码>`、`/approve --list`、
`/approve --revoke <用户>`。

## spool run

跑一次任务。最常用的几条：

| 参数 | 说明 |
|---|---|
| `--goal` | 要做的事（自然语言） |
| `--provider {fake,gemini,llamacpp}` | 供应商（也可写 `--engine`）；留空用配置 |
| `--model` / `--base-url` / `--proxy` | 覆盖模型名 / llama.cpp 地址 / 代理 |
| `--root` | 工作区路径 |
| `--policy {ask,auto,deny}` | 本次授权策略；留空用已保存的 |
| `--scope` | `auto` 下允许**自动落盘**的路径（逗号分隔）；必须由你给 |
| `--autonomous` | 自己拆解目标并逐步做完（要配 `--scope`） |
| `--plan` | 只推进已有计划的下一个待办步骤 |
| `--resume` | 接着上次未完成的检查点继续 |
| `--limit` | 自主模式最多拆几步；`--batch N` 分批推进（每批 N 步，做完回头看一眼再排下一批）|
| `--cover` | 拆解后要覆盖的清单（目标明说"这几件事都要做完"时才给）|
| `--window` | 上下文窗口；`0` = 向供应商问真实值（默认）|
| `--max-steps` / `--step-ceiling` | 单轮步数 / 总步数上限（含督导续期）；`0` = 自动 |
| `--no-supervise` | 关掉督导：撞上限就停，由你决定要不要 `--resume` |
| `--delegate` | 先判断是否派发；派发则由实现者做、审查者验 |
| `--max-targets` / `--review-limit` / `--subagent-steps` | 一次派发带几件 / 超过几件提醒审查吃力 / 子智能体步数上限 |
| `--no-memory` | 关闭记忆读写 |
| `--session` | 会话名（不同时段/目的的活分开记）|
| `--history N` / `--no-history` | 启动时显示最近几轮记录 / 不显示 |
| `--events` | 以 JSON 行输出事件（供 Web UI 消费）；此模式不打印散文 |
| `--allow-read 目录` | 授权额外可读目录（可重复）|
| `--no-mcp` | 不挂用户配置里的外挂 MCP（排查"是不是外挂在捣乱"）|
| `--no-setup` | 第一次运行不弹向导（脚本 / CI 用）|

## spool plan

只拆解、把计划落盘，不执行。参数同上里相关的那些：`--goal`（必给）、`--limit`、
`--cover`、`--session`、供应商与 `--root`。

计划是**会话级**的（`.agent/sessions/<会话>/plan.json`），`--session` 默认 `cli`，
与 `spool run` 一致——`spool run --plan` 读的就是同一个会话那份。

## spool chat

对话式使用：一行一句，多轮共用同一个会话。`/history` 看记录，`/exit` 退出。
参数与 `run` 基本一致（少了自主编排那一组）。

## spool serve

起 Web UI 壳：发目标、看步骤与工具调用、看 diff、点"应用/拒绝"。

| 参数 | 说明 |
|---|---|
| `--host` / `--port` | 监听地址与端口（默认 `127.0.0.1:8765`）|
| `--session` | 会话名 |
| `--provider` `--model` `--script` `--base-url` `--proxy` | 转发给子进程 |
| `--policy` / `--scope` / `--allow-read` | 同上 |
| `--root` | 工作区路径 |

## spool session

列出这个工作区里的会话（哪次干了什么、什么时候、用哪个模型）。

## spool policy

看/改授权策略。

| 参数 | 说明 |
|---|---|
| `--set {ask,auto,deny}` | 改一档。`ask` 每次写都问；`auto` 在 `--scope` 内自动落盘、越界仍问；`deny` 一律问 |

## spool revert

回滚**上一次写入**的改动（写之前留了底）。

## spool config

用户级默认配置（跨工作区、属于这台机器）。

| 参数 | 说明 |
|---|---|
| `--set 键=值` | 可重复。键：`provider` `model` `base_url` `proxy` `script` |
| `--reset 键` | 清除一项 |
| `--path` | 只打印配置文件路径 |

## spool limits

能力标定值：看现在是多少、从哪来；按工作区覆盖（写进 `.agent/limits.json`）。

| 参数 | 说明 |
|---|---|
| `--set 名字=值` | 覆盖一项（可重复）|
| `--reset 名字` | 恢复默认（可重复）|

## spool diagnose

Agent 判断"像是环境坏了"时登记的诊断请求，由**你**（或你跑的外部会话）来处理。

| 参数 | 说明 |
|---|---|
| `--list` | 列出全部请求 |
| `--show <id>` | 看某条请求的完整内容（问题、假设、证据）|
| `--report <id>` | 给某条请求写回报告 |
| `--verdict` / `--findings` / `--evidence` | 结论 / 发现了什么 / 证据 |
| `--init-key` | 生成签名密钥（放在项目外，Agent 的工具够不到）|

## spool bench

跑回归任务集（可复现的测量）。

| 参数 | 说明 |
|---|---|
| `--tasks` | 任务集目录 |
| `--filter` / `--limit` | 只跑名字含片段的 / 只跑前 N 道 |
| `--together` | 第二条仪器：整批题铺进一个工作区，全交给一条会话（量长任务与派发能力）|
| `--verbose` | 打印每个任务的轨迹与验收输出 |

## spool mcp

以 MCP（stdio）服务的方式运行，**供别的 agent 调用**（Codex / Claude Code /
Cursor 扮演用户下发任务）。工具、信任模型、调用序列见 [`mcp.md`](mcp.md)。

## spool mcp-servers

看自己配了哪些外挂 MCP 服务；`--check` 会真连一遍，列出它们提供哪些工具。
配置写在用户配置文件的 `[[mcp]]` 段里。

## spool bridge

把工作区接到一条聊天通道上（消息驱动的 agent）。

| 参数 | 说明 |
|---|---|
| `--channel {fake,telegram,wecom,qqbot}` | 通道种类；`fake` 本地可跑、不需要凭据 |
| `--access {pairing,allowlist,open}` | 准入：`pairing`（默认，陌生人拿码）/ `allowlist`（只放行名单）/ `open`（谁都能用，会警告）|
| `--allow-user 用户` | 白名单（可重复）；给了就切到名单模式 |
| `--approve <码>` | 批准一个配对码然后退出 |
| `--check` | 只看凭据与连通性（认到没有、从哪认到的、能不能连上）|
| `--bridge-proxy 地址` | **通道自己**出网走哪个代理（如 `socks5://127.0.0.1:1080`）|
| `--token` / `--appid` / `--secret` / `--sandbox` | 各通道凭据（也可写在 `.env`）|
| `--user` / `--max-chars` / `--timeout` | 假通道的用户名 / 单条消息长度上限 / 一轮最多等多少秒 |
| `--policy` / `--scope` / `--session` | 原样转给 agent |

详见 [`bridge.md`](bridge.md)。
