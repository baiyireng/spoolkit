# 命令速查

`spool --help` 是总入口；这一页把每条命令、每个参数讲清（与 `--help` 对齐，改了参数就该回来改这一页）。为什么这么设计见 [`rationale.md`](rationale.md)。

`--root` 缺省找工作区的顺位：显式给了 → 往上找 `.agent/` → 客户端指向 → 全局登记表 `%APPDATA%\spoolkit\workspaces.json` → 当前目录。**机器级入口**（`serve` / `hub` / `web` / `bridge` / `qq` / `mcp` 不给 `--root`）**不看当前目录**，它们服务整台机器。
`--session` 留空 = 用客户端指向里那条，没有就回到老默认名：`run` / `plan` / `serve` / `hub` / `web` → `cli`，`chat` → `chat`，`bridge` / `qq` → `bridge`，`mcp` → `mcp`。

`spool` 进对话（不是工作区就先问一句要不要初始化，非交互时打帮助）；`spool --version` → `spool 0.0.1 (spoolkit)`；`spool --help` 是总入口。子命令：`init` `ws` `run` `plan` `policy` `revert` `bench` `session` `serve` `hub` `web` `qq` `diagnose` `limits` `config` `env` `chat` `mcp` `mcp-servers` `bridge` `approve` `extra` `service` `status` `help`。

## spool ws

"在哪个工作区干活"是**运行时状态**，不是启动参数；`ws` 自己**不带 `--root`**（它是用来选工作区的，不是被解析的对象）。

| 命令 | 说明 |
|---|---|
| `spool ws` | 列登记过的工作区（标出当前指向哪个）；子命令 `list` 是默认值 |
| `spool ws use <名字\|序号\|路径\|别名>` | 切过去（写的是 `cli` 这一份指向）|
| `spool ws add <目录>` | 建 `.agent/` 并登记（等价 `spool init`）|
| `spool ws alias <名字> <短名>` | 起个短名 |
| `spool ws forget <名字>` | 从登记表摘掉（目录和 `.agent/` 都不动）|

## spool session

| 命令 | 说明 |
|---|---|
| `spool session` | 列这个工作区的会话（标出当前那条）|
| `spool session new <标题>` | 建一条。标题来自第一句目标（截断 40 字），已有标题就不覆盖 |
| `spool session use <序号\|id\|latest>` | 切过去 |
| `spool session rename <id> <标题>` | 改名 |
| `spool session archive <id>` | 归档：默认不列出，数据都在，id 仍能直接进 |
| `spool session search <关键词>` | 检索**历史消息**（聊天往来）：给片段 + 会话/时间/角色/命中位置；**默认只查当前会话**（指针里那条，没有就用最近活跃的那条）。没命中印一句"没找到"并给退出码 `1` |
| `--all` | 连归档的一起列 |
| `--all-sessions` / `--from-session <会话>` | `search` 跨会话：查本工作区的**全部**会话（结果里每条标出来源会话）/ 只看点名的那一条。两个都不给 = 当前会话 |
| `--limit N` / `--since` / `--until` | `search` 的最多条数（`0` = 按标定值 `history_search_limit`）/ 只看这个时间起 / 只看这个时间止（`2026-09-27` 或 `2026-09-27 14:03`；只给日期按当天 `00:00` 算）|

**序号空间是一份**：列表显示的编号就是 `use <序号>` 认的编号（两边都按含归档的完整列表编号，默认只打印没归档的行，所以序号可能有空档）。

历史消息**默认不进模型上下文**（短上下文正是靠不塞历史才成立的）：模型判断「这个问题是从哪来的」——指代、旧决定、上次聊到哪——时，它自己调 `search_history` 按关键词取回片段与出处。这一条与 `session search` 走的是同一套检索（同一份排序、片段长度、脱敏口径、**作用域口径**）。作用域**默认只到当前会话**，跨会话要显式（`session="*"` / `--all-sessions`），那时结果里每条都标出来源会话；工作区边界不动（跨工作区依旧不查）。通道/网页那条路上，**更早那几轮**还会压成一段有界摘要自动带进本轮提示（`history_digest_turns` / `history_digest_chars`，`spool limits` 看得到来源），消息含指代词时会多一行提醒——理由见 [`rationale.md`](rationale.md#索引与记忆的口径)。

## spool init

| 命令 | 说明 |
|---|---|
| `spool init [路径]` | 把目录做成工作区（幂等）：建 `.agent/` 并登记，默认当前目录。等价 `spool ws add <目录>` |

已经在某个工作区**里面**时不会再套一层（记忆分叉很难看出来）：只登记，不新建。目录里有 `.gitignore` 而里面没有 `.agent/` 时会追加一行。**工作区只能由人建**——`.agent/` 里装着授权策略，让 agent 自己开辟信任边界不行。

## spool approve

批准聊天通道的配对码，等价于 `spool bridge --approve <码>`；**任意目录都能敲**，在 `spool chat` 里可以就地做（`/approve <码>`）。

| 参数 | 说明 |
|---|---|
| `配对码`（可选） | 聊天里收到的那个码 |
| `--list` | 看放行了谁、还有哪些码在等（机器级 + 各工作区旧文件一起列）|
| `--revoke <用户>` | 撤销某人的放行（解绑）。他再发消息会重新拿码 |
| `--forget <码>` | 丢掉一个还没批准的码（谁都不放行）|
| `--ws <名字>` | 批准**旧式**的码时点名工作区（那种码没有工作区维度；登记表里多个时**必须**点名）|
| `--root` | 工作区路径（默认自动找）|

放行是**身份级**的（哪条通道 + 哪个用户 → 能碰哪几个工作区），存在机器级 `channel-trust.json` 里，所以**换工作区不用重新配对**（聊天里 `/ws use`）；旧的 `.agent/bridge-pairings.json` 由当前通道启动时自动认领。批准与撤销**即时生效，不用重启桥**。

## spool extra

在**本机**关联工作区外的目录，名单在工作区 `.agent/read-roots.json`（不带 action 就是 list）。

| 参数 | 说明 |
|---|---|
| `add <目录>` / `remove <目录>` / `list` | 写入配置 / 删掉 / 查看 |
| `--root` | 工作区路径（默认自动找）|

关联目录不是换绑工作区：记忆、索引、检查点仍挂主工作区，`run_command` 的工作目录仍锁在主工作区。**即使 `--policy auto --scope **`，关联目录也不自动落盘**（读放开，写要确认；聊天里加不了）。一次性叠加用 `spool run --allow-read`。

## spool run

| 参数 | 说明 |
|---|---|
| `--goal` | 要做的事（自然语言）|
| `--provider {fake,gemini,llamacpp}` | 供应商（也可写 `--engine`）；留空用配置，什么都没配时内置默认是 `fake`。**fake 只回放 `--script`，不能生成代码；没有 LLM 时不要用它当兜底**。这和 `--channel fake`（聊天通道本地回环）不是一回事 |
| `--script` / `--model` / `--base-url` / `--proxy` | 仅测试回放的假模型脚本 / 模型名 / llama.cpp 地址 / 代理 |
| `--root` | 工作区路径 |
| `--policy {ask,auto,deny}` | 本次授权策略；留空用已保存的。`--autonomous` 且未指定时会**提权为 auto** |
| `--scope` | `auto` 下允许**自动落盘**的路径（逗号分隔）；必须由你给，边界由它守 |
| `--autonomous` | 自己拆解目标并逐步做完（要配 `--scope`）|
| `--plan` / `--resume` | 只推进已有计划的下一个待办步骤 / 接着上次未完成的检查点继续 |
| `--limit` / `--batch N` | 自主模式最多拆几步（默认 10）/ 分批推进，每批 N 步、做完回头看一眼再排下一批（`0` = 一次排完，`--no-roll` 强制一次排完）|
| `--cover` | 拆解后要覆盖的清单（目标明说"这几件事都要做完"时才给）|
| `--window` | 上下文窗口；`0` = 向供应商问真实值（默认，问不到才退回 8192）|
| `--max-steps` / `--step-ceiling` | 单轮步数 / 总步数上限（含督导续期）；`0` = 自动 |
| `--no-supervise` | 关掉督导：撞上限就停，由你决定要不要 `--resume` |
| `--delegate` | 先判断是否派发；派发则由实现者做、审查者验 |
| `--max-targets` / `--review-limit` / `--subagent-steps` | 一次派发带几件（默认 10）/ 超过几件提醒审查吃力（默认 5）/ 子智能体步数上限（默认 20）|
| `--no-memory` / `--session` | 关闭记忆读写 / 会话名（不同时段、不同目的的活分开记）|
| `--history N` / `--no-history` | 启动时显示最近几轮记录（默认 6）/ 不显示 |
| `--events` | 以 JSON 行输出事件（供 Web UI 消费）；此模式不打印散文 |
| `--from-channel` / `--ask-on-stdin` | 桥与网页壳内部用：只限制文件读写 / events 模式下允许在 stdin 上问授权（不给就是无人值守，一律拒绝）|
| `--allow-read <目录>` | 授权额外可读目录（可重复，一次性，不写进配置）|
| `--no-mcp` / `--no-setup` | 不挂外挂 MCP / 第一次运行不弹向导（脚本、CI 用）|
| `--web*` | 联网取用，见文末 |

## spool plan

只拆解、把计划落盘，不执行。参数：`--goal`（**必给**）、`--session`、`--limit`、`--cover`，以及供应商那一组与 `--root` / `--no-setup` / `--no-mcp`。

计划是**会话级**的（`.agent/sessions/<会话>/plan.json`），`--session` 默认与 `spool run` 同一条——`spool run --plan` 读的就是同一个会话那份。

## spool chat

对话式使用：一行一句，多轮共用同一个会话。`/history` 看记录，`/exit` 退出。参数与 `run` 基本一致（少了自主编排那一组）。最近几轮往来会截断后带进本轮提示（理解指代）；工具往返不塞回去。

## spool serve

起 Web UI 壳：发目标、看步骤与工具调用、看 diff、点"应用/拒绝"。**在任意目录都能起**：不给 `--root` 时它服务整台机器（目标可以换），给了就是钉住那一个。

| 参数 | 说明 |
|---|---|
| `--host` / `--port` | 监听地址与端口（默认 `127.0.0.1:8765`）|
| `--session` | 会话名。留空 = 用网页壳的**指向**（`web`），没有就 `cli` |
| `--provider` `--model` `--script` `--base-url` `--proxy` | 转发给子进程 |
| `--policy` / `--scope` / `--allow-read` / `--web` `--no-web` | 同 `run`（联网**默认开**）|
| `--root` | **给了 = 钉住**这一个工作区（老行为逐字保留，脚本、`.bat` 靠它）；不给 = 机器级：初始目标走内核顺位（网页壳指向 → 登记表），**不认 cwd**，定不出来就以"还没有目标"起来，在页面顶上的选择栏里选（或 `POST /target`）|

| 端点 | 说明 |
|---|---|
| `GET /` | 页面 |
| `GET /state` | 快照，**只增不改**地追加 `target`（`root`/`session`/`pinned`/`workspace_source`/`session_source`/`note`）、`workspaces`、`sessions`、`running_targets`（谁在跑，带 `current`）、`readonly`、`host`。`health` 里也**只增**了 `state`（`up`/`slow`/`down`/`unknown`）、`probed_at`（epoch 秒）、`age_s`（离现在多久）、`url`：探活在**后台**，这里给的是最近一次结果与"它是什么时候探的"。没有目标时给形状相同的空态 |
| `GET /events` | 事件流。目标变了会**退订旧的 → 订阅新的 → 补一帧 `state`**，页面据此整屏重绑，不必重连 `EventSource` |
| `POST /target` | body `{"workspace": …, "session": …}`（都可选）：工作区认名字 / 序号 / 路径，会话不点名就沿用当前那条。**切换不杀任务、不阻塞**；显式选一次会写进网页壳指向（`clients.json` 的 `web`），刷新与重启都停得住 |
| `POST /session/new` | body `{"session": …, "title": …}`（都可选）：**建**一条会话（复用 `kernel_sessions.create_session`，存储位置与 `spool session new` 一模一样）。`session` 给了就按它建——只用中文 / 字母 / 数字 / `_` / `-` / `.`，**撞名 409**、**名字非法 400**（空串、斜杠、超长…），都说人话；不给就照既有约定自动生成 id（时间戳 + 两位随机）。**不写指针**：建一条不等于选了它，页面接着用它返回的名字发 `POST /target` 切过去 |
| `POST /run` / `POST /run/stop` / `POST /confirm` | body `{"goal": …}` 起一轮 / **手动中断这一轮**（复用 `Runner.stop()` 的温和停止语义：先 terminate 请它自己收尾、到点才 kill；已产出的内容保留） / body `{"apply": …}` 回答待确认的改动 |

返回码：`/target` 在钉住的壳里 **409**、名字对不上 **400**、还没有目标又没给 `workspace` **400**；`/session/new` 名字非法 **400**、撞名 **409**、还没有目标 **409**、建好了 **200**（响应体 `{"ok": true, "session": <新名字>, "title": …}`）；`/run` 没有目标 **409**、没有 `goal` **400**、已在跑 **409**、起好了 **202**；`/run/stop` 没有目标或没有在跑的任务 **409**、停好了 **200**（`{"ok": true, "stopped": true}`）；`/confirm` 没有目标或没有待确认的改动 **409**。**绑了非回环地址时写端点一律 403**（`/run`、`/run/stop`、`/confirm`、`/session/delete`、`/session/restore`、`/session/new`——同一张 `WRITE_PATHS` 表，响应体里 `readonly: true`，判据排在业务分支**之前**，所以连 goal 都不给也是 403）；只读壳里 `POST /target` **照常可用但不写任何东西**（它改的只是"这个进程现在看谁"）。判据是**实际绑上的地址**：`127.0.0.1` / `::1` / `localhost` 的各种写法算回环，`0.0.0.0` / `::` 不算，认不出来的名字按远程办。页面会探当前生效的模型（llama.cpp 打 `/props`），不通时**右上角那一列的探活圆点变红并带字**（点开看状态 / 地址 / 探活时间 / 可能原因 / 下一步）——探活在**后台线程**里按 3 秒的节拍做，所以 `/state` 与 `/target` 不等它（见 `rationale.md#网页壳与切换`）。`/state` 里**只增**了 `trace`（这一轮的思考轨迹：`open`/`goal`/`lines`）、`stopped`（这一轮是不是被手动停掉的），以及每条消息的 `trace`（那一轮的步骤 / 工具调用 / 失败详情，刷新与切回之后页面靠它回看）与 `created_at`。

## spool hub

常驻入口：起网页壳；**声明了 `--channel` 才再拉一条 bridge**。不启 llama-server，也不启 SOCKS 隧道——那些用 `spool service` 按配方启停，hub 只探活。退出时只杀 hub 自己拉起的 bridge。

| 参数 | 说明 |
|---|---|
| `--host` / `--port` / `--session` | 与 `serve` 相同（**含"绑非回环地址就只读"那条**）|
| `--root` | 与 `serve` 相同：**给了 = 钉住**那一个；不给 = 机器级（不认 cwd）。钉住时桥子进程也只服务这一个 |
| `--provider` `--model` `--script` `--base-url` `--proxy` | 转发给网页壳子进程，也转给 bridge |
| `--policy` / `--scope` / `--allow-read` / `--web*` | 同上 |
| `--channel {fake,telegram,wecom,qqbot}` | 可选。不给就只起网页 |
| `--access` / `--allow-user` / `--bridge-proxy` | 只在给了 `--channel` 时有意义。`--bridge-proxy` 是**通道出网**，不是模型 `--proxy` |
| `--token` / `--appid` / `--secret` / `--sandbox` | 通道凭据（转给拉起的桥；**推荐写用户级 `.env`**）|
| `--notify-disconnect` | 通→断时给最近聊天发一句（默认关，冷却 5 分钟；也可 `spool config --set notify_disconnect=true`）|

模型从通变断（或通道子进程退出）时，hub 终端打一行、网页那枚状态圆点同样变色带字；本机 `[[service]]` 配方不通时也走同一条。

## spool web

机器级网页壳的短命令：**等价于 `spool hub` 不带 `--channel`**——参数面与 `hub` 逐字相同（共用同一份参数定义）。在**任意目录**都能起；不给 `--root` 就不钉工作区，进去再选。`hub` **原样保留、语义不变**（老脚本、`.bat`、文档都靠它）；要连聊天通道用 `spool hub --channel …` 或 `spool qq`。

```bash
spool web                      # 任意目录：起网页壳，进去再选工作区与会话
spool web --host 0.0.0.0       # 给别的机器看：只读（/run、/run/stop 与 /confirm 403）
spool web --root D:\某个工作区  # 给了就钉住这一个（老行为）
```

## spool qq

按用户配置起 **QQ 官方机器人**通道：**等价于 `spool bridge --channel qqbot …`**，只是 `--timeout` / `--policy` / `--scope` / `--bridge-proxy` / `--web` 这五个全都有默认值了（`--root` 与其余参数和 `bridge` 同名同义）。

```bash
spool qq                                  # 参数全来自用户配置与用户级 .env
spool qq --check                          # 只看凭据与连通性（不起通道）
spool qq --policy ask --scope "**" --timeout 240 --no-web
spool qq --root D:\某个工作区              # 给了就钉住这一个；不给 = 机器级
```

默认值按 **命令行 > 环境变量 > 用户配置 `[channel.qqbot]` > 内置默认** 算出来，**命令行永远压得住**（包括"给了空串"，例如 `--scope ""` = 不许自动落盘），起手第一屏会把生效值与"每个值是从哪一份来的"一起印在 stderr 上。**凭据走用户级 `.env`**（`spool env set QQ_AppID=… QQ_AppSecret=…`）：这是机器级入口，**不看 cwd**；缺凭据时它**在建通道那一步就停住**，把三种给法、"找过哪些位置"以及能照着敲的下一步一起印出来。`bridge` 的参数默认值一个字没动——`[channel.*]` 只给短命令当默认值。

## spool bridge

把工作区接到一条聊天通道上（消息驱动的 agent）。详见 [`bridge.md`](bridge.md)。

| 参数 | 说明 |
|---|---|
| `--channel {fake,telegram,wecom,qqbot}` | 通道种类；`fake` 是 stdin/stdout 本地回环，**不是** `--provider fake` |
| `--access {pairing,allowlist,open}` | 准入：`pairing`（默认，陌生人拿码）/ `allowlist`（只放行名单）/ `open`（谁都能用，会警告）|
| `--allow-user <用户>` | 白名单（可重复）；给了就切到名单模式 |
| `--approve <码>` / `--check` | 批准一个配对码然后退出 / 只看凭据与连通性：**认到没有、从哪认到的、连不连得上**，并印"找过这些位置"（只印键名与遮罩后的值）。`--check` **机器级也能用** |
| `--bridge-proxy <地址>` | **通道自己**出网走哪个代理（如 `socks5://127.0.0.1:1080`）。和模型 `--proxy` 不是一回事 |
| `--token` / `--appid` / `--secret` / `--sandbox` | 各通道凭据（**推荐写用户级 `.env`**）|
| `--user` / `--max-chars` / `--timeout` | 假通道的用户名 / 单条消息长度上限（默认 4000 字）/ 一轮最多等多少秒（默认 900）|
| `--policy` / `--scope` / `--session` | 原样转给 agent；`--session` 留空 = 用这条聊天的指向 |
| `--notify-disconnect` | 通→断时给最近一次放过的聊天发一句（默认关）|

`--root` **可选**（"不给"是有意义的取值）：给了 = 这条通道**只**服务那一个工作区（收紧，凭据也从那个工作区的 `.env` 读）；不给 = **服务整台机器**——落到哪由"这条聊天的指向"（`chat:<通道>:<用户>`）决定，聊天里 `/ws use <名字>` 就能换，**不用重新配对**。启动时会把旧 `.agent/bridge-pairings.json` 里已批准的人认领成机器级放行。

**不给 `--root` 时它不看当前目录**，凭据也就只从**用户级 `.env`**（与环境变量）来。**一处例外**：登记表里一个工作区都没有时，内部会退回启动目录，否则装机第一天没人能完成第一次配对；说得出登记表却说不清是哪个，就报错不猜。

## spool mcp

以 MCP（stdio）服务的方式运行，**供别的 agent 调用**。工具、信任模型、调用序列见 [`mcp.md`](mcp.md)。

`--root` 可选：不给就**服务整台机器**——每次 `delegate_task` 可以带 `workspace` 点名一个已登记的工作区（`list_workspaces` 先看有什么），不给就用 `mcp` 的指向；给了 `--root` 就把整条 MCP 会话钉在那一个工作区里，这时点名别的工作区**会报错**（它得真的压得住，不然"钉住"是句空话）。`--session` 留空 = 用 `mcp` 的指向，单次调用还能用 `session` 参数覆盖。其余参数（供应商、`--policy` / `--scope` / `--web*` / `--allow-read`）同 `run`。

## spool mcp-servers

看自己配了哪些外挂 MCP 服务。`--check` 会真连一遍，列出它们提供哪些工具。配置写在用户配置文件的 `[[mcp]]` 段里。

## spool service

本机外接配方的快捷入口：按用户配置里的 `[[service]]` 拉起 / 停掉 / 列出。**不写进仓库**，也不写死 gguf 路径或 ssh 主机。子命令 `list`（默认）/ `start` / `stop`；`name` 是配方名，`start`/`stop` 不给则对全部（start 只动有 `command` 的）。

```toml
[[service]]
name = "llama"                       # 本机语言模型：hub 按 provider / base_url 探活，通了不重复拉起
command = ["llama-server", "-m", "你的模型.gguf", "--port", "8080"]
health = "http://127.0.0.1:8080/props"

[[service]]
name = "tunnel"                      # 通道出网的本机 SOCKS（ssh -D），不是模型代理
command = ["ssh", "-N", "-D", "1080", "你的跳板"]
health = "tcp:127.0.0.1:1080"
```

`command` 可空：空就只探测、不替你启动。`health` 认 `http(s)://…`（GET）和 `tcp:host:port`；停掉只杀 `spool service start` 写下 pid 的孩子。`spool config --set` 不会抹掉 `[[service]]` / `[[mcp]]`。

## spool status

一眼看清这台机器上的 spoolkit 服务：一行一个服务，给**状态 / 地址或说明 / 怎么起 / 怎么停**。**只打本机地址**（不联网），每个探测点短超时，探不通给一句人话＋非零退出码，不冒回溯。

| 行 | 探什么 |
|---|---|
| `provider` | 生效的模型供应商（命令行 > 环境变量 > 用户配置）；没配过或生效值是 `fake` 都算关键，并给出该敲的那一条 |
| `model` | 本机模型（`base_url`，默认 `http://127.0.0.1:8080`）打 `/props`：**没起 / 起来了但慢**（`503` 或端口通而 HTTP 不应答）/ 正常 |
| `web:<端口>` | 网页壳（默认探 `8765`，别的端口用 `--web-port`）：读 `GET /state`，报**监听端口、是不是只读（绑了非回环地址）、当前目标**；端口上有别的东西也会说出来 |
| `bridge:<通道>` | 用户配置 `[channel.*]` ＋ 放行记录里的通道：凭据**从哪个文件 / 哪个环境变量来**（只印来源，不印值）；桥是前台进程，"在不在跑"说不出就说不出 |
| `mcp:<名字>` | 外挂 MCP 登记了哪些；`--probe` 才真连一次（会拉起它们的子进程，超时另算） |
| `workspace` | 登记了几个工作区、网页壳与命令行的指针各指向谁 |
| `config` / `env:user` / `env:workspace` | `config.toml`、用户级 `.env`、当前工作区 `.env` 的**路径与存在性**（只印键数，不印值） |
| `service:<配方>` | 用户配置 `[[service]]` 里的每条配方（与 `spool service` 同一套探活） |

| 参数 | 说明 |
|---|---|
| `--json` | 给机器读：`ok` / `critical_down` / `rows[]{name,label,state,critical,target,detail,start,stop}`；`state` 取 `up` / `slow` / `down` / `unknown` / `none` |
| `--timeout 秒` | 每个探测点的超时（默认 `0.5`：探的都是回环地址，正常监听时毫秒级就有回音）|
| `--web-port 端口` | 除 `8765` 外还要看哪个端口上的网页壳（可重复）|
| `--probe` / `--mcp-timeout 秒` | 真连一次外挂 MCP（默认只列登记了哪些）/ 连它的超时（默认 `5`：要起子进程）|

**关键项**（表里带 ★，没起 → 退出码 `1`）只有三处：模型供应商没配过或生效值是 `fake`；生效 `provider` 是 `llamacpp` 时的本机模型；`[[service]]` 里写了 `command` 的配方（你声明了要拉起它）。表里其余各行只报不判（网页壳、通道桥按需起，外挂 MCP 与密钥落点不影响退出码）。退出码：`0` 关键都在、`1` 有关键没起、`2` 用法错或配置读不出来。

## spool help

`spool help [子命令]` 等价于 `spool --help` / `spool <子命令> --help`（退出码 `0`）；认不出的子命令还是 argparse 那套报错与退出码 `2`。它不新造帮助系统：实现就是一次带 `--help` 的重新解析，所以两处文本逐字相同。

## spool policy

| 参数 | 说明 |
|---|---|
| `--set {ask,auto,deny}` | 改一档。`ask`（默认）每次写都问；`auto` 在 `--scope` 内自动落盘、越界仍问；`deny` 一律问 |
| `--root` | 工作区路径（默认自动找）|

## spool revert

回滚**上一次写入**的改动（写之前留了底）。只有 `--root`。

## spool config

用户级默认配置（跨工作区、属于这台机器）。

| 参数 | 说明 |
|---|---|
| `--set 键=值` | 可重复。**白名单只有 7 个键**：`provider` `model` `base_url` `proxy` `script` `notify_disconnect` `web_search_api` |
| `--reset 键` | 清除一项 |
| `--path` | 只打印配置文件路径 |

**密钥不写这里**（`--set` 只动白名单，而且值会打到终端）：密钥走 `spool env`。页脚印的优先级是"命令行 > 环境变量 `SPOOLKIT_<键名大写>` > 用户配置 > 内置默认"，改名前的 `AGENTS_DEV_<键名大写>` 也认（新前缀优先）。这份文件里还能有**子表**：`--set` 管不到它们，但**不会把它们写丢**。

- `[channel.<名字>]`：短命令 `spool qq` 的默认值，认 `proxy`("")/ `policy`("")/ `scope`("")/ `timeout`(900)/ `web`(true)（括号里是内置默认），环境变量是 `SPOOLKIT_CHANNEL_<名字>_<键>`。这一节里还认一个 `root`，但**短命令不读它**（工作区在更早的一步就解析完了），所以 `spool config` 会把那一行标注出来。`spool config` 会把每一节现在是什么、每个值来自哪一层印出来，**并把认不出的键名指出来**；取值写错的也一样（`policy = "AUTO"`、`timeout = -5`、`web = "大概"` 会在 `spool qq` 起手时说明并**跳过那一层**、退回下一层）。小节怎么写见 [`bridge.md`](bridge.md#用户配置channel名字)。
- `[[mcp]]` / `[[service]]`：外挂工具与本机外接配方。

## spool env

用户级 `.env` 的看 / 写 / 搬。管的是**这台机器上的你**，所以**不带 `--root`**、与在哪个工作区无关。子命令 `list`（默认）/ `path` / `set` / `import`。

```bash
spool env                                    # 看：路径 + 有哪些键（只印键名与遮罩形状）+ config.toml 路径
spool env path                               # 只印路径（给脚本用）
spool env set KEY=VALUE [KEY=VALUE …]        # 写进去：同键替换，注释/空行/分节/别人的键原样保留
spool env import --from <工作区的 .env>       # 搬上来，来源文件不动；要的键一个都没有时不建目标文件
```

位置是 `%APPDATA%\spoolkit\.env`（其它平台 `~/.config/spoolkit/.env`，与 `config.toml` 同目录），`SPOOLKIT_ENV_FILE` 可覆盖（改名前的 `AGENTS_DEV_ENV_FILE` 也认）。解析顺序 **环境变量 → 工作区 `.env` → 用户级 `.env`**（更具体的赢），通道凭据 / Tavily / Gemini 三处共用。四条规矩：

- **不回显值**：不合 `KEY=VALUE` 形状的参数只报个数，`--only` 的值一律不印。密钥进过一次终端，就等于进过一次滚动缓冲、一次截图，**连报错也不例外**。
- **不动别人的行**：只替换同名键；编码（UTF-8 / BOM / UTF-16）与换行（CRLF / LF）按原文还原，符号链接**顺着写**（换掉链接本身＝"报成功、实际没生效"）。
- **写入是原子的**：先写同目录临时文件再替换；失败（含 Ctrl-C）删掉临时文件，下次写入顺手扫掉超过 1 小时的**陈旧**临时文件——里面是明文密钥。
- **权限**：POSIX 上收紧到 600；Windows 上**故意什么都不做**（那边改权限要另一套 API，多半改不对——假装保护过了比不保护更坏）。

`.env` 的读法：一行一个 `KEY=VALUE`（`export KEY=VALUE` 也认），`#` 开头是注释，值两边的引号去掉，**不做变量展开**。编码认 UTF-8 / BOM / UTF-16——Windows 记事本存 UTF-8 常带 BOM，存"Unicode"则是 UTF-16，不认它们的症状是"文件里明明有这一行，它说没有"。文件不存在就当空。

## spool limits

| 参数 | 说明 |
|---|---|
| `--set 名字=值` / `--reset 名字` | 覆盖一项 / 恢复默认（都可重复，写进 `.agent/limits.json`）|
| `--root` | 工作区路径（默认自动找）|

## spool diagnose

Agent 判断"像是环境坏了"时登记的诊断请求，由**你**（或你跑的外部会话）来处理。

| 参数 | 说明 |
|---|---|
| `--list` / `--show <id>` | 列出全部请求 / 看某条请求的完整内容（问题、假设、证据）|
| `--report <id>` | 给某条请求写回报告 |
| `--verdict` / `--findings` / `--evidence` | 结论 / 发现了什么 / 证据 |
| `--init-key` | 生成签名密钥（放在项目外，Agent 的工具够不到）|

## spool bench

跑回归任务集（可复现的测量）。

| 参数 | 说明 |
|---|---|
| `--tasks` / `--filter` / `--limit` | 任务集目录 / 只跑名字含片段的 / 只跑前 N 道 |
| `--together` | 第二条仪器：整批题铺进一个工作区，全交给一条会话（量长任务与派发能力）|
| `--verbose` / `--window` / `--max-steps` / `--step-ceiling` | 打印每条轨迹与验收输出 / 同 `run` |

## 联网取用参数（`run` / `chat` / `mcp` / `serve` / `hub` / `web` / `bridge` / `qq` 都有）

| 参数 | 说明 |
|---|---|
| `--web` / `--no-web` | 打开 / 关掉联网取用：`research` / `web_fetch` / `web_read`。**默认开** |
| `--web-allow <域名>` / `--web-deny <域名>` | 只允许 / 禁止抓这些域名（可重复）。allow 不给 = 允许任何公网域名（内网仍拦）；deny 优先于 allow |
| `--proxy` | 抓取也走它；不给就用系统代理 |
| `--web-search-api 名字` | 网页搜索后端，现在只接 `tavily`。不给但有 Tavily key 也当 tavily |

搜索要一把 **Tavily** key（[申请](https://tavily.com)）。来源按这个顺序：环境变量 `TAVILY_API_KEY`（或 `SPOOLKIT_TAVILY_API_KEY`）、工作区 `.env`、**用户级 `.env`**、用户配置里的 `tavily_api_key`（旧位置，写在那儿的不用搬家）。**不要**用 `spool config --set` 写密钥。没 key 时研究会话会说「没配搜索，只能直接给 URL」，不会去抓搜索结果页。配了之后 `research` 先搜，摘要够就停，不够再抓最相关的一页；条数 `search_result_limit` 默认 3。内网 / 环回 / 保留地址**默认硬拦**，与 allow 无关。细节与已知短板见 [`manual.md`](manual.md#联网取用)。
