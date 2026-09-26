# 命令速查

`spool --help` 是总入口；这一页把每条命令、每个参数讲清（内容与 `--help` 对齐，
改了参数就该回来改这一页）。

**所有带 `--root` 的命令**：默认自动找工作区（显式 → 往上找 `.agent/` → 客户端指向
→ 全局登记表 → 当前目录），见 [`getting-started.md`](getting-started.md#4-在任意目录起手)。

`--session` 留空（默认）表示"用**指向**里那条会话"，没有就回到该命令一直以来的
默认名：`run` / `plan` → `cli`，`chat` → `chat`，`bridge` → `bridge`，`mcp` → `mcp`，
`serve` / `hub` → `cli`。指向由 `spool ws use` / `spool session use` 写。

## 入口

```bash
spool                # 进对话；不是工作区就先问一句要不要初始化（非交互时打帮助）
spool --version      # spool 0.0.1 (spoolkit)
spool --help
```

## spool ws

工作区管理。"在哪个工作区干活"现在是**运行时状态**，不是启动参数。

```bash
spool ws                              # 列登记过的工作区（标出当前指向哪个）
spool ws use 我的项目                  # 按名字 / 序号 / 路径 / 别名切
spool ws add D:\workSpace\新项目       # 建 .agent/ 并登记（等价 spool init）
spool ws alias 我的项目 proj           # 起个短名
spool ws forget 我的项目               # 从登记表摘掉（目录和 .agent/ 都不动）
```

`add` 就是 `spool init`：**工作区只能由人建**（`.agent/` 里装着授权策略，
让 agent 自己开辟信任边界不行）。`ws` 自己**不带 `--root`**——它是用来选工作区的，
不是被解析的对象。

## spool session

会话有了生命周期：列、建、切、改名、归档。

```bash
spool session                         # 列这个工作区的会话（标出当前那条）
spool session --all                   # 连归档的一起列
spool session new 修 calc.py           # 建一条（标题 = 你给的那句话）
spool session use 2                    # 按序号 / id / latest 切
spool session rename <id> 新标题
spool session archive <id>             # 归档：默认不列出，数据都在，id 仍能直接进
```

**序号空间是一份**：列表里显示的编号就是 `use <序号>` 认的编号（两边都按含归档的
完整列表编号，默认只打印没归档的行，所以序号可能有空档）。标题来自第一句目标，
已经有标题就不覆盖。

## spool init

把一个目录做成工作区（幂等）。它就是"建 `.agent/` 并登记"，`spool approve`、
`spool session` 之类因此能在任意目录用。等价于 `spool ws add <目录>`。

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
| `配对码`（可选） | 聊天里收到的那个码 |
| `--root` | 工作区路径（默认：当前目录向上找 → 登记表） |
| `--ws <名字>` | 批准**旧式**的码时点名工作区（那种码没有工作区维度；登记表里多个时**必须**点名）|
| `--list` | 看放行了谁、还有哪些码在等（机器级 + 各工作区旧文件一起列）|
| `--revoke <用户>` | 撤销某人的放行（解绑）。他再发消息会重新拿码 |
| `--forget <码>` | 丢掉一个还没批准的码（谁都不放行） |

放行是**身份级**的（哪条通道 + 哪个用户 → 能碰哪几个工作区），存在机器级的
`channel-trust.json` 里，所以**换工作区不用重新配对**——聊天里 `/ws use` 就行。
旧的 `.agent/bridge-pairings.json` 由当前通道启动时自动认领（不删不改）。
在 `spool chat` 里可以就地做同样的事：`/approve <码>`、`/approve --list`、
`/approve --revoke <用户>`。

## spool extra

在**本机**关联工作区外的目录。读放开，写要确认后才落盘；聊天里加不了。
名单在工作区 `.agent/read-roots.json`。

```bash
spool extra add D:\某目录
spool extra remove D:\某目录
spool extra              # 列出
```

| 参数 | 说明 |
|---|---|
| `add` / `remove` / `list` | 写入配置、删掉、查看（不带 action 就是 list） |
| `path` | `add` / `remove` 时要给的目录 |
| `--root` | 工作区路径（默认自动找） |

关联目录不是换绑工作区：记忆、索引、检查点仍挂主工作区。`run_command` 的
工作目录仍锁在主工作区。即使 `--policy auto --scope **`，关联目录也不自动落盘。
一次性叠加仍可用 `spool run --allow-read`（不写进配置）。

## spool run

跑一次任务。最常用的几条：

| 参数 | 说明 |
|---|---|
| `--goal` | 要做的事（自然语言） |
| `--provider {fake,gemini,llamacpp}` | 供应商（也可写 `--engine`）；留空用配置。**fake 只回放 `--script`，不能生成代码；没有 LLM 时不要用它当兜底**。这和 `--channel fake`（聊天通道本地回环）不是一回事 |
| `--script` | 仅测试回放：假模型的应答脚本。真实任务不要走这条 |
| `--model` / `--base-url` / `--proxy` | 覆盖模型名 / llama.cpp 地址 / 代理 |
| `--root` | 工作区路径 |
| `--policy {ask,auto,deny}` | 本次授权策略；留空用已保存的。`--autonomous` 且未指定时会**提权为 auto**（边界仍由 `--scope` 守） |
| `--scope` | `auto` 下允许**自动落盘**的路径（逗号分隔）；必须由你给 |
| `--autonomous` | 自己拆解目标并逐步做完（要配 `--scope`）。未写 `--policy` 时提权为 auto；显式给了就尊重 |
| `--web` / `--no-web` | 联网取用（**默认开**）。完整参数见文末「联网取用」 |
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
参数与 `run` 基本一致（少了自主编排那一组）。最近几轮往来会截断后带进本轮
提示（理解指代）；工具往返不塞回去。

## spool serve

起 Web UI 壳：发目标、看步骤与工具调用、看 diff、点"应用/拒绝"。

| 参数 | 说明 |
|---|---|
| `--host` / `--port` | 监听地址与端口（默认 `127.0.0.1:8765`）|
| `--session` | 会话名。留空 = 用网页壳的**指向**（`web`），没有就 `cli` |
| `--provider` `--model` `--script` `--base-url` `--proxy` | 转发给子进程 |
| `--policy` / `--scope` / `--allow-read` | 同上 |
| `--web` / `--no-web` | 联网取用（**默认开**，与 CLI / QQ 同一套）；不要就 `--no-web` |
| `--root` | 工作区路径。不给就按"当前目录 → 指向 → 登记表"解析 |

页面会探当前生效的模型（llama.cpp 打 `/props`；假模型/云端不探本机端口），
不通时顶部出红横幅。只起网页壳、不拉聊天通道时继续用这一条。

## spool hub

产品内核的常驻入口：起网页壳；**声明了 `--channel` 才再拉一条 bridge**。
不启 llama-server，也不启 SOCKS 隧道——那些是本机外接，用现成的
`provider` / `base_url` 探活即可。退出时只杀 hub 自己拉起的 bridge。

用户配置 `[[service]]` 常见两段（名字可改，路径/主机不进仓库）：

- **llama**：本机语言模型。hub 按 `provider` / `base_url` 探活；配方只方便
  `spool service start/stop`。
- **tunnel**：聊天通道出网的本机 SOCKS（通常 `ssh -D`）。QQ 等平台有 IP
  白名单时借跳板固定出口。`--bridge-proxy socks5://127.0.0.1:1080` 去连它，
  **不是** `--proxy`（那是模型供应商）。

```bash
spool hub                          # 等价于 serve：只起网页
spool hub --channel qqbot --bridge-proxy socks5://127.0.0.1:1080
```

| 参数 | 说明 |
|---|---|
| `--host` / `--port` / `--session` / `--root` | 与 `serve` 相同 |
| `--provider` `--model` `--script` `--base-url` `--proxy` | 转发给网页壳子进程，也转给 bridge |
| `--policy` / `--scope` / `--allow-read` / `--web*` | 同上 |
| `--channel {fake,telegram,wecom,qqbot}` | 可选。不给就只起网页 |
| `--access` / `--allow-user` / `--bridge-proxy` | 只在给了 `--channel` 时有意义。`--bridge-proxy` 是通道出网（对准 `tunnel` 那条 SOCKS），不是模型 `--proxy` |
| `--token` / `--appid` / `--secret` / `--sandbox` | 通道凭据（也可写在工作区 `.env`） |
| `--notify-disconnect` | 通→断时给最近聊天发一句（默认关） |
| `--web` / `--no-web` | 联网取用（默认开，与 `run` / 网页壳同一套）；`--no-web` 关掉 |

模型从通变断（或通道子进程退出）时，hub 终端打一行；网页同一条横幅。
本机 `[[service]]` 配方不通时也走同一条横幅（hub **不**替你启动它们）。
桥给最近聊天发一句：默认关；`--notify-disconnect` 或
`spool config --set notify_disconnect=true` 打开。只在通→断时发，冷却 5 分钟。

## spool service

本机外接配方的快捷入口：按用户配置里的 `[[service]]` 拉起 / 停掉 / 列出。
**不写进仓库**，也不写死 gguf 路径或 ssh 主机。hub 只探活，不启这些进程。

常见两段（名字可改）：**llama** 是本机语言模型；**tunnel** 是通道出网的
SOCKS（`ssh -D`），给 `hub --bridge-proxy` 用，不是模型 `--proxy`。

```bash
spool service                         # 列出（没表就打印写法）
spool service start llama
spool service start tunnel
spool service stop tunnel
```

| 参数 | 说明 |
|---|---|
| `list` / `start` / `stop` | 查看、拉起、停掉（不带 action 就是 list） |
| `name` | 配方名；`start`/`stop` 不给则对全部（start 只动有 `command` 的） |

配方写在用户配置文件（`spool config --path` 那份）里：

```toml
# llama：本机语言模型。hub 按 provider / base_url 探活；已经通了不重复拉起。
[[service]]
name = "llama"
command = ["llama-server", "-m", "你的模型.gguf", "--port", "8080"]
health = "http://127.0.0.1:8080/props"

# tunnel：通道出网的本机 SOCKS（ssh -D），不是模型代理。
# QQ 有 IP 白名单时借跳板固定出口；hub --bridge-proxy socks5://127.0.0.1:1080。
[[service]]
name = "tunnel"
command = ["ssh", "-N", "-D", "1080", "你的跳板"]
health = "tcp:127.0.0.1:1080"
```

`command` 可空：空就只探测、不替你启动。`health` 认 `http(s)://…`（GET）和
`tcp:host:port`。停掉只杀 `spool service start` 写下 pid 的孩子。
`spool config --set` 不会抹掉 `[[service]]` / `[[mcp]]`。

## spool session（速查）

列出这个工作区里的会话（哪次干了什么、什么时候、用哪个模型）。
完整子命令（`new` / `use` / `rename` / `archive` / `--all`）见前面
[`spool session`](#spool-session) 那一节。

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
| `--set 键=值` | 可重复。键：`provider` `model` `base_url` `proxy` `script` `notify_disconnect` |
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

`--root` 可选：不给就**服务整台机器**——每次 `delegate_task` 可以带 `workspace`
点名一个已登记的工作区（`list_workspaces` 先看有什么），不给就用 `mcp` 的指向；
给了 `--root` 就把整条 MCP 会话钉在那一个工作区里（想收紧时用），这时点名别的工作区
**会报错**（它得真的压得住，不然"钉住"是句空话）。
`--session` 留空 = 用 `mcp` 的指向，单次调用还能用 `session` 参数覆盖。

## spool mcp-servers

看自己配了哪些外挂 MCP 服务；`--check` 会真连一遍，列出它们提供哪些工具。
配置写在用户配置文件的 `[[mcp]]` 段里。

## spool bridge

把工作区接到一条聊天通道上（消息驱动的 agent）。

| 参数 | 说明 |
|---|---|
| `--channel {fake,telegram,wecom,qqbot}` | 通道种类；`fake` 是 stdin/stdout 本地回环，**不是** `--provider fake` |
| `--access {pairing,allowlist,open}` | 准入：`pairing`（默认，陌生人拿码）/ `allowlist`（只放行名单）/ `open`（谁都能用，会警告）|
| `--allow-user 用户` | 白名单（可重复）；给了就切到名单模式 |
| `--approve <码>` | 批准一个配对码然后退出 |
| `--check` | 只看凭据与连通性（认到没有、从哪认到的、能不能连上）|
| `--bridge-proxy 地址` | **通道自己**出网走哪个代理（如 `socks5://127.0.0.1:1080`）。对准本机 `[[service]]` 里的 `tunnel`（`ssh -D`），不是模型 `--proxy` |
| `--token` / `--appid` / `--secret` / `--sandbox` | 各通道凭据（也可写在 `.env`）|
| `--user` / `--max-chars` / `--timeout` | 假通道的用户名 / 单条消息长度上限 / 一轮最多等多少秒 |
| `--policy` / `--scope` / `--session` | 原样转给 agent；`--session` 留空 = 用这条聊天的指向 |
| `--notify-disconnect` | 通→断时给最近一次放过的聊天发一句（默认关） |

`--root` **可选**（而且"不给"是有意义的取值）：给了 = 这条通道**只**服务那一个
工作区（收紧，向后兼容）；不给 = **服务整台机器**——落到哪由"这条聊天的指向"
决定（`chat:<通道>:<用户>`），聊天里 `/ws use <名字>` 就能换，**不用重新配对**。
启动时会把旧 `.agent/bridge-pairings.json` 里已批准的人认领成机器级放行（不删不改）。

详见 [`bridge.md`](bridge.md)。

## 联网取用参数（`run` / `chat` / `mcp` / `serve` / `bridge` 都有）

| 参数 | 说明 |
|---|---|
| `--web` | 打开联网取用：`research` / `web_fetch` / `web_read`。**默认开**（QQ、网页壳、CLI 同一套） |
| `--no-web` | 关掉联网取用 |
| `--web-allow 域名` | 只允许抓这些域名（可重复）。不给 = 允许任何公网域名 |
| `--web-deny 域名` | 禁止抓这些域名（可重复，优先于 allow） |
| `--proxy` | 抓取也走它；不给就用系统代理 |
| `--web-search-api 名字` | 网页搜索后端。现在只接 `tavily`。不给但环境/工作区 `.env`/用户配置里有 Tavily key，也当 tavily |

搜索要一把 **Tavily** key（[申请](https://tavily.com)）。来源按这个顺序：环境变量 `TAVILY_API_KEY`（或 `SPOOLKIT_TAVILY_API_KEY`）、工作区 `.env`、用户配置里的 `tavily_api_key`（手写进 `%APPDATA%\\spoolkit\\config.toml`，**不要**用 `spool config --set` 写密钥——那个命令只动白名单，会把密钥打到终端）。没 key 时研究会话会说「没配搜索，只能直接给 URL」，不会去抓搜索结果页。

配了之后：`research` 先搜，把标题+网址+摘要交给一轮会话；摘要够（天气这类）就停，不够再抓最相关的一页。条数 `search_result_limit`，默认 3。

内网/环回/保留地址**默认硬拦**，与 allow 无关。细节与已知短板见
[`manual.md`](manual.md#联网取用--web默认开)。
