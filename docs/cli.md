# 命令速查

`spool --help` 是总入口。本页逐条列出命令与参数，文本与 `--help` 对齐；参数变更后本页需同步修改。设计理由见 [`rationale.md`](rationale.md)。

`--root` 缺省时的工作区解析顺位：显式给出 → 自当前目录向上查找 `.agent/` → 客户端指向 → 全局登记表 `%APPDATA%\spoolkit\workspaces.json` → 当前目录。**机器级入口**（`serve` / `hub` / `web` / `bridge` / `qq` / `mcp`，不给 `--root` 时）**不读取当前目录**，服务整台机器。
`--session` 留空时取客户端指向中的会话；无指向时用该入口的默认名：`run` / `plan` / `serve` / `hub` / `web` → `cli`，`chat` → `chat`，`bridge` / `qq` → `bridge`，`mcp` → `mcp`。

`spool` 不带子命令时进入对话：当前目录不是工作区时先询问是否在此初始化，非交互环境下改为打印帮助。`spool --version` 输出 `spool 0.0.1 (spoolkit)`。子命令：`init` `ws` `run` `plan` `policy` `revert` `bench` `session` `serve` `hub` `web` `qq` `diagnose` `limits` `config` `env` `chat` `mcp` `mcp-servers` `bridge` `approve` `extra` `service` `status` `help`。

## spool ws

当前工作区属于**运行时状态**，不是启动参数；`ws` 自身**不接受 `--root`**（它的作用是选择工作区，不是被解析的对象）。

| 命令 | 说明 |
|---|---|
| `spool ws` | 列出已登记的工作区，并标出当前指向；子命令 `list` 为默认值 |
| `spool ws use <名字\|序号\|路径\|别名>` | 切换工作区（写入 `cli` 这一份指向）|
| `spool ws add <目录>` | 创建 `.agent/` 并登记（等价于 `spool init`）|
| `spool ws alias <名字> <短名>` | 为工作区设置短名 |
| `spool ws forget <名字>` | 从登记表移除（目录与 `.agent/` 均不变）|

## spool session

| 命令 | 说明 |
|---|---|
| `spool session` | 列出本工作区的会话，并标出当前一条 |
| `spool session new <标题>` | 新建会话。标题取自第一句目标（截断至 40 字），已有标题时不覆盖 |
| `spool session use <序号\|id\|latest>` | 切换会话 |
| `spool session rename <id> <标题>` | 重命名会话 |
| `spool session archive <id>` | 归档：默认不再列出，数据保留，id 仍可直接使用 |
| `spool session search <关键词>` | 检索**历史消息**（聊天往来）：给出片段与会话 / 时间 / 角色 / 命中位置；**默认只查当前会话**（指向中的那一条，无指向时用最近活跃的一条）。无命中时打印"没找到"并返回退出码 `1` |
| `--all` | 连归档的一并列出 |
| `--all-sessions` / `--from-session <会话>` | `search` 跨会话：检索本工作区的**全部**会话（结果中每条标注来源会话）/ 只查指定的一条。两者都不给时为当前会话 |
| `--limit N` / `--since` / `--until` | `search` 的最大条数（`0` = 使用标定值 `history_search_limit`）/ 只查该时刻之后 / 只查该时刻之前（`2026-09-27` 或 `2026-09-27 14:03`；只给日期时按当天 `00:00` 计算）|

**序号空间只有一份**：列表中显示的编号即 `use <序号>` 接受的编号（两者均按含归档的完整列表编号；默认只打印未归档的行，因此编号可能出现空档）。

历史消息**默认不进模型上下文**（短上下文以此为前提）：模型判断指代、旧决定或上次交谈位置时，自行调用 `search_history` 按关键词取回片段与出处。该工具与 `session search` 共用同一套检索（排序、片段长度、脱敏口径与**作用域口径**相同）。作用域**默认限于当前会话**，跨会话须显式指定（`session="*"` / `--all-sessions`），此时每条结果标注来源会话；工作区边界不变（跨工作区不检索）。通道与网页壳路径下，**更早的若干轮**会压缩为有界摘要带入本轮提示（`history_digest_turns` / `history_digest_chars`，来源见 `spool limits`）；消息含指代词时额外打印一行。理由见 [`rationale.md`](rationale.md#索引与记忆的口径)。

## spool init

| 命令 | 说明 |
|---|---|
| `spool init [路径]` | 把目录做成工作区（幂等）：创建 `.agent/` 并登记，默认当前目录。等价于 `spool ws add <目录>` |

已在某个工作区**内部**时不再新建一层（避免难以察觉的记忆分叉）：只登记，不新建。目录中存在 `.gitignore` 且其中没有 `.agent/` 时追加一行。**工作区只能由人创建**：`.agent/` 中存放授权策略，不允许 agent 自行开辟信任边界。

## spool approve

批准聊天通道的配对码，等价于 `spool bridge --approve <码>`；**任意目录均可执行**，在 `spool chat` 中可用 `/approve <码>` 就地完成。

| 参数 | 说明 |
|---|---|
| `配对码`（可选） | 聊天中收到的配对码 |
| `--list` | 查看已放行的用户与仍在等待的码（机器级与各工作区旧文件一并列出）|
| `--revoke <用户>` | 撤销该用户的放行（解绑）；他再发消息会重新获得配对码 |
| `--forget <码>` | 丢弃一个尚未批准的码（不放行任何人）|
| `--ws <名字>` | 批准**旧式**配对码时指定工作区（该类码没有工作区维度；登记表中有多个工作区时**必须**指定）|
| `--root` | 工作区路径（默认自动找）|

放行是**身份级**的（通道 + 用户 → 可访问的工作区），存放于机器级 `channel-trust.json`，因此**切换工作区无需重新配对**（聊天中 `/ws use`）；旧的 `.agent/bridge-pairings.json` 由当前通道启动时自动认领。批准与撤销**即时生效，无需重启桥**。

## spool extra

在**本机**关联工作区以外的目录，名单位于工作区 `.agent/read-roots.json`（不带 action 时等价于 `list`）。

| 参数 | 说明 |
|---|---|
| `add <目录>` / `remove <目录>` / `list` | 写入配置 / 删除 / 查看 |
| `--root` | 工作区路径（默认自动找）|

关联目录不等于更换工作区：记忆、索引与检查点仍属于主工作区，`run_command` 的工作目录仍限制在主工作区。**即使指定 `--policy auto --scope **`，关联目录也不自动落盘**（读放开，写需确认；聊天中无法添加）。一次性叠加用 `spool run --allow-read`。

## spool run

| 参数 | 说明 |
|---|---|
| `--goal` | 任务目标（自然语言）|
| `--provider {fake,gemini,llamacpp}` | 供应商（也可写作 `--engine`）；留空时用配置，均未配置时内置默认为 `fake`。**fake 仅回放 `--script`，不能生成代码；没有可用 LLM 时不应作为兜底**。与 `--channel fake`（聊天通道本地回环）含义不同 |
| `--script` / `--model` / `--base-url` / `--proxy` | 仅供测试回放：假模型脚本 / 模型名 / llama.cpp 地址 / 代理 |
| `--root` | 工作区路径 |
| `--policy {ask,auto,deny}` | 本次运行策略；留空用已保存值。`--autonomous` 且未显式给出时**提权为 `auto`** |
| `--scope` | `auto` 下允许**自动落盘**的路径（逗号分隔）；须显式给出，越界退回确认 |
| `--autonomous` | 自行拆解目标并逐步完成（须同时给出 `--scope`）|
| `--plan` / `--resume` | 仅推进现有计划的下一个待办步骤 / 从上次未完成的检查点继续 |
| `--limit` / `--batch N` | 自主模式下最多拆解的步数（默认 10）/ 分批推进：每批 N 步，每批完成后回顾结果再排下一批（`0` = 一次排完；`--no-roll` 同样一次排完）|
| `--cover` | 拆解后需覆盖的清单（目标明说"这几件事都要做完"时才给）|
| `--window` | 上下文窗口；`0` = 向供应商查询实际值（默认，查不到时退回 8192）|
| `--max-steps` / `--step-ceiling` | 单轮步数上限（默认 10）/ 总步数上限（含督导续期）；`0` = 自动 |
| `--no-supervise` | 关闭督导：达到上限即停，是否 `--resume` 由调用方决定 |
| `--delegate` | 先判断是否派发；派发时由实现者执行、审查者验证 |
| `--max-targets` / `--review-limit` / `--subagent-steps` | 一次派发的目标数（默认 10）/ 超过该数提示审查吃力（默认 5）/ 子智能体步数上限（默认 20）|
| `--no-memory` / `--session` | 关闭记忆读写 / 会话名（不同时段、不同目的的任务分开记）|
| `--history N` / `--no-history` | 启动时显示最近若干轮（默认 6）/ 不显示 |
| `--events` | 以 JSON 行输出事件（供 Web UI 消费）；不打印散文 |
| `--from-channel` / `--ask-on-stdin` | 桥与网页壳内部用：仅限制文件读写 / events 模式下允许在 stdin 上询问授权（不给即无人值守，一律拒绝）|
| `--allow-read <目录>` | 授权额外可读目录（可重复，一次性，不入配置）|
| `--no-mcp` / `--no-setup` | 不挂外挂 MCP / 首次运行不弹向导（脚本、CI 用）|
| `--web*` | 联网取用，见文末 |

## spool plan

仅拆解并把计划落盘，不执行。参数：`--goal`（**必给**）、`--session`、`--limit`、`--cover`，以及供应商参数与 `--root` / `--no-setup` / `--no-mcp`。

计划是**会话级**的（`.agent/sessions/<会话>/plan.json`）；`--session` 默认与 `spool run` 同一条，`spool run --plan` 读同一份计划。

## spool chat

对话式使用：一行一句，多轮共用同一会话。`/history` 看记录，`/exit` 退出。参数与 `run` 基本一致（不含自主编排一组）。最近若干轮往来截断后带入本轮提示，用于理解指代；工具调用往返不带入下一轮。

## spool serve

起 Web UI 壳：发目标、看步骤与工具调用、看 diff、应用或拒绝。**任意目录可启动**：不给 `--root` 时服务整台机器（目标可切换），给了就钉住这一个工作区。

| 参数 | 说明 |
|---|---|
| `--host` / `--port` | 监听地址与端口（默认 `127.0.0.1:8765`）|
| `--session` | 会话名。留空时用网页壳的**指向**（`web`），无指向则用 `cli` |
| `--provider` `--model` `--script` `--base-url` `--proxy` | 转发给子进程 |
| `--policy` / `--scope` / `--allow-read` / `--web` `--no-web` | 同 `run`（联网**默认开启**）|
| `--root` | **给了 = 钉住**这一个工作区（原行为逐字保留，脚本与 `.bat` 依赖此项）；不给则为机器级：初始目标按内核顺位（网页壳指向 → 登记表）确定，**不读取 cwd**，定不出来时以"还没有目标"启动，在页面顶部的选择栏选（或 `POST /target`）|

| 端点 | 说明 |
|---|---|
| `GET /` | 页面 |
| `GET /state` | 快照，**只增不改**地追加 `target`（`root` / `session` / `pinned` / `workspace_source` / `session_source` / `note`）、`workspaces`、`sessions`、`running_targets`（含 `current`）、`readonly`、`host`。`health` 同样**只增** `state`（`up` / `slow` / `down` / `unknown`）、`probed_at`（epoch 秒）、`age_s`（距今秒数）、`url`：探活在**后台线程**执行，此处为最近一次结果与探测时刻。无目标时返回形状相同的空态 |
| `GET /events` | 事件流。目标变化时**退订旧目标 → 订阅新目标 → 补发一帧 `state`**，页面据此整屏重绑，无需重连 `EventSource` |
| `POST /target` | body `{"workspace": …, "session": …}`（均可选）：工作区接受名字 / 序号 / 路径；会话不指定时沿用当前一条。**切换不终止任务、不阻塞**；显式选择写入网页壳指向（`clients.json` 的 `web`），刷新与重启后仍有效 |
| `POST /session/new` | body `{"session": …, "title": …}`（均可选）：**新建**会话（复用 `kernel_sessions.create_session`，存储位置与 `spool session new` 相同）。给出 `session` 时按该名新建——名字仅接受中文 / 字母 / 数字 / `_` / `-` / `.`，**重名 409**、**非法名 400**（空串、斜杠、超长等），响应体为可读说明；不给时按既有约定自动生成 id（时间戳 + 两位随机）。**不写指向**：新建不等于选中，页面随后用它返回的名字发 `POST /target` 切换 |
| `POST /run` / `POST /run/stop` / `POST /confirm` | body `{"goal": …}` 启动一轮 / **手动中断本轮**（复用 `Runner.stop()` 的温和停止语义：先 terminate 请其收尾，到点才 kill；已产出的内容保留）/ body `{"apply": …}` 回答待确认的改动 |

返回码：`/target` 在钉住的壳里 **409**、名字无法解析 **400**、无目标且未给 `workspace` **400**；`/session/new` 非法名 **400**、重名 **409**、无目标 **409**、建成 **200**（响应体 `{"ok": true, "session": <新名字>, "title": …}`）；`/run` 无目标 **409**、缺 `goal` **400**、已有运行 **409**、启动成功 **202**；`/run/stop` 无目标或未在跑 **409**、停止成功 **200**（`{"ok": true, "stopped": true}`）；`/confirm` 无目标或无待确认改动 **409**。绑定非回环地址时写端点一律 403（`/run`、`/run/stop`、`/confirm`、`/session/delete`、`/session/restore`、`/session/new`；同一张 `WRITE_PATHS` 表，响应体 `readonly: true`，判据在业务分支**之前**，故未给 goal 也 403）；只读壳中 `POST /target` **可用但不写持久状态**（它只改变本进程当前查看的目标）。判据是**实际绑定的地址**：`127.0.0.1` / `::1` / `localhost` 各写法为回环，`0.0.0.0` / `::` 不是，认不出的名字按远程处理。页面探测当前模型（llama.cpp 打 `/props`）；不可达时右上角状态列的探活圆点变红并显示状态词（点开见状态 / 地址 / 探活时间 / 可能原因 / 下一步）。探活由后台线程按 3 秒节拍执行，`/state` 与 `/target` 不等待探测（见 [`rationale.md#网页壳与切换`](rationale.md#网页壳与切换)）。`/state` 中**只增** `trace`（本轮思考轨迹：`open` / `goal` / `lines`）、`stopped`（本轮是否被手动停止）与每条消息的 `trace`（该轮步骤 / 工具调用 / 失败详情，刷新与切回后据此回看）及 `created_at`。

## spool hub

常驻入口：起网页壳；**声明 `--channel` 时才再拉一条 bridge**。不启 llama-server，也不启 SOCKS 隧道——那些由 `spool service` 按配方启停，hub 只做探活。退出时只终止 hub 自己拉起的 bridge。

| 参数 | 说明 |
|---|---|
| `--host` / `--port` / `--session` | 与 `serve` 相同（**含"绑定非回环地址即只读"一条**）|
| `--root` | 与 `serve` 相同：**给了 = 钉住**这一个工作区；不给则为机器级。钉住时桥子进程也只服务这一个工作区 |
| `--provider` `--model` `--script` `--base-url` `--proxy` | 转发给网页壳子进程，也转发给 bridge |
| `--policy` / `--scope` / `--allow-read` / `--web*` | 同上 |
| `--channel {fake,telegram,wecom,qqbot}` | 可选。不给时只起网页壳 |
| `--access` / `--allow-user` / `--bridge-proxy` | 仅在给了 `--channel` 时有意义。`--bridge-proxy` 指**通道出网**代理，不是模型 `--proxy` |
| `--token` / `--appid` / `--secret` / `--sandbox` | 通道凭据（转发给拉起的桥；**推荐写入用户级 `.env`**）|
| `--notify-disconnect` | 通→断时给最近的聊天发一条（默认关，冷却 5 分钟；也可 `spool config --set notify_disconnect=true`）|

模型由通变断（或通道子进程退出）时，hub 终端打印一行、网页的状态圆点同样变色并带字；本机 `[[service]]` 配方不通时同上。

## spool web

机器级网页壳的短命令：**等价于不带 `--channel` 的 `spool hub`**——参数面与 `hub` 逐字相同（共用同一份定义）。**任意目录可启动**；不给 `--root` 则不钉住工作区，进入后再选。`hub` **原样保留、语义不变**（既有脚本、`.bat` 与文档依赖它）；要连聊天通道用 `spool hub --channel …` 或 `spool qq`。

```bash
spool web                      # 任意目录：起网页壳，进入后再选工作区与会话
spool web --host 0.0.0.0       # 供其他机器访问：只读（/run、/run/stop 与 /confirm 返回 403）
spool web --root D:\某个工作区  # 给了就钉住这一个工作区（原行为）
```

## spool qq

按用户配置起 **QQ 官方机器人**通道：**等价于 `spool bridge --channel qqbot …`**，区别是 `--timeout` / `--policy` / `--scope` / `--bridge-proxy` / `--web` 五项均有默认值（`--root` 与其余参数同 `bridge`）。

```bash
spool qq                                  # 参数全部来自用户配置与用户级 .env
spool qq --check                          # 只检查凭据与连通性（不起通道）
spool qq --policy ask --scope "**" --timeout 240 --no-web
spool qq --root D:\某个工作区              # 给了就钉住这一个工作区；不给则为机器级
```

默认值按 **命令行 > 环境变量 > 用户配置 `[channel.qqbot]` > 内置默认** 计算，**命令行始终优先**（含空串，如 `--scope ""` 表示不允许自动落盘）；起手把生效值与各值来源打印到 stderr。**凭据走用户级 `.env`**（`spool env set QQ_AppID=… QQ_AppSecret=…`）：该入口是机器级；缺凭据时**在建通道这一步停止**，打印三种给法、找过哪些位置与可直接执行的下一步。`bridge` 的参数默认值未改动——`[channel.*]` 仅为短命令提供默认值。

## spool bridge

把工作区接到一条聊天通道上（消息驱动的 agent）。详见 [`bridge.md`](bridge.md)。

| 参数 | 说明 |
|---|---|
| `--channel {fake,telegram,wecom,qqbot}` | 通道种类；`fake` 是 stdin/stdout 本地回环，**不是** `--provider fake` |
| `--access {pairing,allowlist,open}` | 准入：`pairing`（默认，陌生人获配对码）/ `allowlist`（只放行名单）/ `open`（谁都放行，会警告）|
| `--allow-user <用户>` | 白名单（可重复）；给出即切到名单模式 |
| `--approve <码>` / `--check` | 批准一个配对码后退出 / 只检查凭据与连通性：**是否找到、从何处找到、能否连通**，并打印"找过这些位置"（只印键名与遮蔽值）。`--check` 在**机器级也可用** |
| `--bridge-proxy <地址>` | **通道自身**出网用的代理（如 `socks5://127.0.0.1:1080`）。与模型 `--proxy` 含义不同 |
| `--token` / `--appid` / `--secret` / `--sandbox` | 各通道凭据（**推荐写入用户级 `.env`**）|
| `--user` / `--max-chars` / `--timeout` | 假通道的用户名 / 单条消息长度上限（默认 4000 字）/ 一轮最长等待秒数（默认 900）|
| `--policy` / `--scope` / `--session` | 原样转发给 agent；`--session` 留空时用该聊天的指向 |
| `--notify-disconnect` | 通→断时给最近一次放行的聊天发一条（默认关）|

`--root` **可选**（"不给"是有意义的取值）：给出时该通道**只**服务那一个工作区（收紧；凭据也从该工作区 `.env` 读）；不给时**服务整台机器**——落到哪由该聊天的指向（`chat:<通道>:<用户>`）决定，聊天中 `/ws use <名字>` 即可切换，**无需重新配对**。启动时会认领旧 `.agent/bridge-pairings.json` 中已批准的用户为机器级放行。

**不给 `--root` 时不读取当前目录**，凭据只来自**用户级 `.env`**（与环境变量）。**一处例外**：登记表中没有任何工作区时，退回桥的启动目录（否则新机器无法完成首次配对）；登记表非空却无法确定是哪一个时报错，不猜。

## spool mcp

以 MCP（stdio）服务方式运行，**供其他 agent 调用**。工具、信任模型与调用序列见 [`mcp.md`](mcp.md)。

`--root` 可选：不给时**服务整台机器**——每次 `delegate_task` 可带 `workspace` 指定已登记工作区（先用 `list_workspaces` 查看），不给则用 `mcp` 的指向；给了 `--root` 就把整条 MCP 会话钉在那一个工作区里，指定其他工作区**会报错**（否则约束不成立）。`--session` 留空时用 `mcp` 的指向，单次调用可用 `session` 覆盖。其余参数（供应商、`--policy` / `--scope` / `--web*` / `--allow-read`）同 `run`。

## spool mcp-servers

查看已配置的外挂 MCP 服务。`--check` 会实际连接一次，列出其提供的工具。配置写在用户配置文件的 `[[mcp]]` 段。

## spool service

本机外接配方的快捷入口：按用户配置 `[[service]]` 启动 / 停止 / 列出。**不入仓库**，也不写死 gguf 路径或 ssh 主机。子命令 `list`（默认）/ `start` / `stop`；`name` 是配方名，`start` / `stop` 不给时对全部（`start` 只动有 `command` 的）。

```toml
[[service]]
name = "llama"                       # 本机语言模型：hub 按 provider / base_url 探活；start 以 pid 记录为判据，不重复拉起
command = ["llama-server", "-m", "你的模型.gguf", "--port", "8080"]
health = "http://127.0.0.1:8080/props"

[[service]]
name = "tunnel"                      # 通道出网的本机 SOCKS（ssh -D），不是模型代理
command = ["ssh", "-N", "-D", "1080", "你的跳板"]
health = "tcp:127.0.0.1:1080"
```

`command` 可为空：为空时只探测、不启动。`health` 接受 `http(s)://…`（GET）与 `tcp:host:port`；停止只杀 `spool service start` 记下 pid 的子进程。`spool config --set` 不会抹掉 `[[service]]` / `[[mcp]]`。

**"要不要拉起"的判据是本命令记录的 pid，不是探针**（探针只报状态）：同一条配方最多一个进程，有存活记录时打印"已经在跑（pid …）"。`list` 的状态词按此口径解读：

| 状态词 | 解读 |
|---|---|
| 运行中（本命令启动） | pid 记录中的进程仍存活（`start` 写入）；其后是 pid 与探针结果（HTTP 状态，或 `端口开着`）|
| 端口被占用（非本命令启动） | 探针通，但没有存活的 pid 记录——该地址被其他进程占用；改用其他端口，或先停止该进程 |
| 未运行 | 无 pid 记录且探针不通；其后是探针结果（如 `timed out`）|
| 无法判定 | 该配方未配置 `health`，也没有 pid 记录——在不在跑看不出 |

无存活 pid 记录时才启动：子进程**启动后立刻退出**时，输出给出其**退出码与最后几行输出**（`ssh -D 1080` 遇到被占端口时即为此处的 `bind: Address already in use`），完整输出保存在用户状态目录的 `services\<配方>.log`。

拉起的子进程**脱离当前控制台**（Windows 给 `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`，POSIX 用新会话），在同一窗口按 Ctrl-C 不会把它带走；它的输出仍重定向到上面那份日志。

## spool status

一览本机 spoolkit 相关服务：一行一个服务，给出**状态 / 地址或说明 / 启动命令 / 停止命令**。**只访问本机地址**（不联网），各探测点短超时，失败时打印可读说明并返回非零退出码，不打印回溯。

| 行 | 探测内容 |
|---|---|
| `provider` | 生效的模型供应商（命令行 > 环境变量 > 用户配置）；未配置或生效值为 `fake` 均计为关键项，并给出应执行的命令 |
| `model` | 本机模型（`base_url`，默认 `http://127.0.0.1:8080`）打 `/props`：**没起 / 起来了但慢**（`503`，或端口通而 HTTP 不应答）/ 正常 |
| `web:<端口>` | 网页壳（默认探 `8765`，其他端口用 `--web-port`）：读 `GET /state`，报**监听端口、是否只读（绑了非回环地址）、当前目标**；端口上有别的东西也会说明 |
| `bridge:<通道>` | 用户配置 `[channel.*]` 与放行记录中的通道：凭据**来自哪个文件 / 哪个环境变量**（只印来源，不印值）；桥是前台进程，"在不在跑看不出" |
| `mcp:<名字>` | 外挂 MCP 登记了哪些；`--probe` 才真连一次（会拉起子进程，超时另算）|
| `workspace` | 已登记的工作区数量，以及网页壳与命令行的指向 |
| `config` / `env:user` / `env:workspace` | `config.toml`、用户级 `.env`、当前工作区 `.env` 的**路径与存在性**（只印键数，不印值）|
| `service:<配方>` | 用户配置 `[[service]]` 中的每条配方（与 `spool service list` 同一套：**pid 记录是判据**，探针报状态；状态列写「被占用」，说明写「非本命令启动」，不谎报为本命令启动的）|

| 参数 | 说明 |
|---|---|
| `--json` | 给机器读：`ok` / `critical_down` / `provider` / `timeout_s` / `elapsed_ms` / `rows[]`（每条含 `name,label,state,critical,target,detail,start,stop`）；`state` 取 `up` / `slow` / `down` / `unknown` / `none` |
| `--timeout 秒` | 每个探测点的超时（默认 `0.5`：探的都是回环地址，正常监听时毫秒级就有回音）|
| `--web-port 端口` | 除 `8765` 外还要看哪个端口上的网页壳（可重复）|
| `--probe` / `--mcp-timeout 秒` | 真连一次外挂 MCP（默认只列登记项）/ 连它的超时（默认 `5`：要起子进程）|

**关键项**（表中带 ★，没起则退出码 `1`）只有三处：模型供应商未配置或生效值为 `fake`；生效 `provider` 为 `llamacpp` 时的本机模型；`[[service]]` 中写了 `command` 的配方（你声明了要拉起它）。表中其余各行只报不判（网页壳与通道桥按需起，外挂 MCP 与密钥落点不影响退出码）。退出码：`0` 关键项都在、`1` 有关键项没起、`2` 用法错或配置读不出来。

## spool help

`spool help [子命令]` 等价于 `spool --help` / `spool <子命令> --help`（退出码 `0`）；无法识别的子命令仍使用 argparse 的报错与退出码 `2`。它不新建帮助系统：实现是一次带 `--help` 的重新解析，因此两处文本逐字相同。

## spool policy

| 参数 | 说明 |
|---|---|
| `--set {ask,auto,deny}` | 改一档。`ask`（默认）每次写入均先确认；`auto` 在 `--scope` 内自动落盘、越界退回确认；`deny` 只读，改动被丢弃、绝不落盘 |
| `--root` | 工作区路径（默认自动查找）|

## spool revert

回滚**上一次写入**的改动（写入前已留存副本）。只有 `--root`。

## spool config

用户级默认配置（跨工作区，属于本机）。

| 参数 | 说明 |
|---|---|
| `--set 键=值` | 可重复。**白名单只有 7 个键**：`provider` `model` `base_url` `proxy` `script` `notify_disconnect` `web_search_api` |
| `--reset 键` | 清除一项 |
| `--path` | 只打印配置文件路径 |

**密钥不写此处**（`--set` 只动白名单，且值会打印到终端）：密钥用 `spool env`。页脚印的优先级为"命令行 > 环境变量 `SPOOLKIT_<键名大写>` > 用户配置 > 内置默认"，改名前的 `AGENTS_DEV_<键名大写>` 也认（新前缀优先）。该文件还可有**子表**：`--set` 管不到，但**不会写丢**。

- `[channel.<名字>]`：短命令 `spool qq` 的默认值，识别 `proxy`("")/ `policy`("")/ `scope`("")/ `timeout`(900)/ `web`(true)（括号内为内置默认），环境变量为 `SPOOLKIT_CHANNEL_<名字>_<键>`。该节还识别 `root`，但**短命令不读它**（工作区在更早步骤已解析），`spool config` 会标注该行。`spool config` 打印每一节现状与各值来源层级，**并指出认不出的键名**；取值非法同样如此（`policy = "AUTO"`、`timeout = -5`、`web = "大概"` 会在 `spool qq` 启动时说明、**跳过该层**并退回下一层）。写法见 [`bridge.md`](bridge.md#用户配置channel名字)。
- `[[mcp]]` / `[[service]]`：外挂工具与本机外接配方。

## spool env

用户级 `.env` 的查看 / 写入 / 搬迁。管理对象为**本机上的用户**，因此**不带 `--root`**，与当前工作区无关。子命令 `list`（默认）/ `path` / `set` / `import`。

```bash
spool env                                    # 查看：路径 + 有哪些键（只打印键名与遮蔽形状）+ config.toml 路径
spool env path                               # 只打印路径（供脚本用）
spool env set KEY=VALUE [KEY=VALUE …]        # 写入：同键替换，注释 / 空行 / 分节 / 别人的键原样保留
spool env import --from <工作区的 .env>       # 搬到用户级，来源文件不动；所需键一个都没有时不建目标文件
```

位置是 `%APPDATA%\spoolkit\.env`（其他平台 `~/.config/spoolkit/.env`，与 `config.toml` 同目录），可由 `SPOOLKIT_ENV_FILE` 覆盖（改名前的 `AGENTS_DEV_ENV_FILE` 也认）。解析顺序 **环境变量 → 工作区 `.env` → 用户级 `.env`**（更具体的赢），通道凭据 / Tavily / Gemini 三处共用。四条规则：

- **不回显值**：不合 `KEY=VALUE` 形状的参数只报数量，`--only` 的值一律不印。密钥一旦进入终端输出，即进入滚动缓冲与截图，**报错信息同样适用**。
- **不改动其他行**：只替换同名键；编码（UTF-8 / BOM / UTF-16）与换行（CRLF / LF）按原文还原；符号链接**顺着写**（换掉链接本身＝"报成功、实际没生效"）。
- **写入是原子的**：先写同目录临时文件再替换；失败（含 Ctrl-C）时删掉临时文件，下次写入时清理超过 1 小时的**陈旧**临时文件（里面是明文密钥）。
- **权限**：POSIX 上收紧为 600；Windows 上**不做处理**（该平台修改权限需另一套 API，易出错；假装已保护比不保护更差）。

`.env` 的解析规则：一行一个 `KEY=VALUE`（`export KEY=VALUE` 也认），`#` 开头为注释，值两侧引号去除，**不做变量展开**。编码认 UTF-8 / BOM / UTF-16——Windows 记事本保存 UTF-8 常带 BOM，保存为"Unicode"时为 UTF-16；不认这些编码的症状是"文件中存在该行却报告没有"。文件不存在时按空处理。

## spool limits

| 参数 | 说明 |
|---|---|
| `--set 名字=值` / `--reset 名字` | 覆盖一项 / 恢复默认（均可重复，写入 `.agent/limits.json`）|
| `--root` | 工作区路径（默认自动查找）|

## spool diagnose

agent 判断"环境可能已损坏"时登记的诊断请求，由**人**（或人运行的外部会话）处理。

| 参数 | 说明 |
|---|---|
| `--list` / `--show <id>` | 列出全部请求 / 查看某条请求的完整内容（问题、假设、证据）|
| `--report <id>` | 为某条请求写回报告 |
| `--verdict` / `--findings` / `--evidence` | 结论 / 发现 / 证据 |
| `--init-key` | 生成签名密钥（存放于项目目录之外，不在 agent 文件工具的可及范围内）|

## spool bench

运行回归任务集（可复现的测量）。

| 参数 | 说明 |
|---|---|
| `--tasks` / `--filter` / `--limit` | 任务集目录 / 只运行名字含指定片段的任务 / 只运行前 N 道 |
| `--together` | 第二种仪器：整批题目放入一个工作区，全部交给一条会话（测量长任务与派发能力）|
| `--verbose` / `--window` / `--max-steps` / `--step-ceiling` | 打印每条轨迹与验收输出 / 同 `run` |

## 联网取用参数（`run` / `chat` / `mcp` / `serve` / `hub` / `web` / `bridge` / `qq` 都有）

| 参数 | 说明 |
|---|---|
| `--web` / `--no-web` | 开启 / 关闭联网取用：`research` / `web_fetch` / `web_read`。**默认开启** |
| `--web-allow <域名>` / `--web-deny <域名>` | 只允许 / 禁止抓取这些域名（可重复）。不给 allow = 允许任何公网域名（内网仍拦截）；deny 优先于 allow |
| `--proxy` | 抓取同样使用该代理；不给则使用系统代理 |
| `--web-search-api 名字` | 网页搜索后端，目前仅支持 `tavily`。不给但存在 Tavily key 时按 tavily 处理 |

搜索需要一把 **Tavily** key（[申请](https://tavily.com)）。来源顺序：环境变量 `TAVILY_API_KEY`（或 `SPOOLKIT_TAVILY_API_KEY`）、工作区 `.env`、**用户级 `.env`**、用户配置 `tavily_api_key`（旧位置，无需搬迁）。**不要**用 `spool config --set` 写密钥。没有 key 时研究会话给出「没配搜索，只能直接给 URL」，不抓搜索结果页。配置后 `research` 先搜索，摘要足够即停止，不足时抓最相关的一页；条数由 `search_result_limit` 决定，默认 3。内网 / 环回 / 保留地址**默认硬拦**，与 allow 无关。细节与短板见 [`manual.md`](manual.md#联网取用)。
