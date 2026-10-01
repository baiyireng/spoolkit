# 消息通道桥：用聊天驱动 agent

在手机上发一条消息 → agent 在你的工作区里跑一轮 → 把结果发回来。通道本身只是"收发"那一层：它
把消息变成一次 agent 运行，授权、工作区、待确认清单都与 CLI 和网页壳共用同一套。

## 四条通道

| 通道 | 收消息 | 发消息 | 是否可用 |
|---|---|---|---|
| `fake` | stdin 一行 | stdout | **可用**（无需凭据，也是测试用的那条）|
| `qqbot`（**QQ 官方机器人**）| WebSocket 网关（长连接，**不需要公网入口**）| `POST /v2/users\|groups/.../messages` | 可用，仅需 AppID + AppSecret |
| `telegram` | 长轮询 `getUpdates`（**不需要公网入口**）| `sendMessage` | 可用，仅需一个 bot token（国内需自备代理）|
| `wecom`（企业微信自建应用）| **回调推送**（需要公网 URL + 解密）| `message/send` | 发消息本机可用；收消息需先把回调接出去 |

## 凭据从哪来

**环境变量 → 工作区 `.env` → 用户级 `.env`**（更具体的优先）。**不给 `--root` 的机器级桥没有"工作区那一层"**，凭据只从**用户级 `.env`** 与环境变量来：Windows `%APPDATA%\spoolkit\.env`，其它平台 `~/.config/spoolkit/.env`（`SPOOLKIT_ENV_FILE` 可指到别处）。机器级为何不认 cwd，见 [`rationale.md#cwd-契约`](rationale.md#cwd-契约)。

`.env` 里写成平台给的键名（QQ 为 `QQ_AppID` / `QQ_AppSecret`）。写进哪一层：**只在这一个项目里当通道用 → 工作区 `.env`；要在这台机器上任意目录起桥 → 用户级 `.env`**——后者无需记录路径：

```powershell
spool env set QQ_AppID=102xxxxxx QQ_AppSecret=xxxxxx   # 写进用户级 .env（不回显输入的值）
spool env import --from D:\workSpace\agentWorkSpace\.env  # 把工作区那份搬上来（来源文件不动）
```

配完先跑一次 **`--check`**：它输出"是否认到、从哪认到、能否连上"，**并印出"找过这些位置"**（只印键名与遮罩后的值），不起通道、不跑 agent。**机器级（不给 `--root`）也能用。** 输出形状：

```
$ spool bridge --channel qqbot --check
工作区：机器级（没给 --root，服务整台机器）
找过这些位置：
  环境变量 —— 都没设置（认这些名字：SPOOLKIT_QQ_APPID、…）
  [用户级] …\spoolkit\.env —— 里面有：QQ_AppID、QQ_AppSecret
AppID：102***66（…\.env 里的 QQ_AppID）  AppSecret：******（…）  换到了 access_token：uhp***Cw
```

### IP 白名单：借一台云主机出网

QQ 机器人开放平台有 **IP 白名单**，要把调用方的公网 IP 填进那个应用（否则平台报 `接口访问源IP不在白名单`，而凭据本身是对的）。家用宽带出口 IP 会变，借一台固定 IP 的云主机更省事——白名单只需填该 IP：

```bash
ssh -D 1080 root@<云主机>          # 本地 1080 即为 SOCKS5；也可写成 [[service]] name = "tunnel"
spool bridge --channel qqbot --bridge-proxy socks5://127.0.0.1:1080 --policy ask
```

于是平台看到的是云主机的 IP，本机的 agent 照常读写本地工作区。三点注意：

- **`--bridge-proxy` 与 `--proxy` 不是同一个设置**：前者管**通道自己**出网，后者管**模型供应商**。
- HTTP 那一半走 SOCKS 需要 httpx 的 socks 可选依赖（虚拟环境 `uv pip install "httpx[socks]"`；uv 工具安装的 `uv tool install --reinstall --editable ".[socks]"`），缺失时报 `No module named 'socksio'`；网关那一半（WebSocket）用自己的 SOCKS5 握手，不需要额外依赖。
- `ssh -D` 需要**服务端允许端口转发**（`AllowTcpForwarding` 改成 `local`），否则连上后本地端口不通。

常驻入口更省事：`spool hub --channel qqbot`（网页壳 + 桥）。`--channel fake` 是 stdin/stdout 本地回环，用于测桥；**不要**写成 `--provider fake`（那是只回放 `--script` 的假模型，不能写代码）。

```powershell
spool bridge --channel fake --provider llamacpp --policy auto --scope "**" --user me --allow-user me  # 本地跑通
$env:SPOOLKIT_TELEGRAM_TOKEN = "123456:ABC..."    # @BotFather 给的凭据（也可写进 .env）
spool bridge --channel telegram --allow-user 123456789 --policy auto --scope "src"
spool bridge --channel qqbot --policy auto --scope "src"     # 沙箱加 --sandbox
```

## 用户配置：`[channel.<名字>]`

起一条通道要敲的那几个参数（`--bridge-proxy` / `--policy` / `--scope` / `--timeout` / `--web`）**没有一个属于"这一次"**，因此可以写进用户配置文件的子表：

```toml
[channel.qqbot]
proxy   = "socks5://127.0.0.1:1080"   # 通道自己出网；等价于 --bridge-proxy
policy  = "ask"                       # 子进程的授权策略，只认 ask / auto / deny
scope   = "**"                        # 允许自动落盘的范围
timeout = 240                         # 一轮最多等多少秒，要大于 0
web     = true                        # 联网取用
```

| 层 | 长什么样 |
|---|---|
| 命令行 | `spool qq --policy ask --timeout 240`。**给了即到此为止**，包括给空串（`--scope ""` = 不许自动落盘）|
| 环境变量 | `SPOOLKIT_CHANNEL_QQBOT_PROXY` / `..._POLICY` / `..._SCOPE` / `..._TIMEOUT` / `..._WEB`（旧前缀 `AGENTS_DEV_CHANNEL_*` 也认，新的优先）|
| 用户配置 | 上面那个 `[channel.qqbot]` |
| 内置默认 | `proxy=""`、`policy=""`、`scope=""`、`timeout=900`、`web=true` |

这个子表**只有短命令 `spool qq` 读取**（`spool web` 没有通道名，读不了它）；`spool bridge` / `hub` 的参数默认值无变化。这一节里还认一个 `root`，但**短命令不读它**：工作区在 `main()` 那一步已解析完，那时"没给 `--root`"（机器级）与"给了空串"已是同一个值——要钉住工作区就在命令行给 `spool qq --root D:\某个工作区`。

起手第一屏会印出生效值与来源（stderr），**这一节里写错的键名与取值都会被指出**——`proxi` 这类拼写或 `policy = "AUTO"`、`timeout = -5` 的症状正是"设置了却没生效"，而配置文件这条路原先无人校验：

```
[channel.qqbot] 本次生效：proxy=…(用户配置)、policy=ask(用户配置)、scope=…(用户配置)、timeout=…(用户配置)、web=false(用户配置)
[channel.qqbot] 里这些键不认识（等于没配）：proxi    可用的键：proxy、policy、scope、timeout、web、root
[channel.qqbot] policy 的值不在认得的取值里（现在写的是 'AUTO'；可用：ask、auto、deny），这一层跳过
```

"这一层跳过"即字面含义：那一层作废、退回下一层，**但绝不静默**。`timeout` 必须大于 0（0 或负数意味着每一轮当场到点）；`web` 认 `true` / `false`（环境变量里也认 `yes` / `no` / `on` / `off` / `1` / `0`）。`spool config` 会印出每一节当前的值与每个值的来源层，并把 `root` 那行标注为「短命令不读取此项」。

> `proxy` 指**通道自己出网**那条路（`--bridge-proxy`），**不是**模型供应商的 `--proxy`。写错会让代理一路传进 agent 子进程、把模型请求也塞进那条隧道。

## 安全模型

聊天通道等于把 agent 对外挂出，因此三件事必须同时成立：

1. **默认配对，未放行者不可用**：陌生发送者会拿到一个一次性配对码，你在机器上执行
   `spool approve <码>`（等价于 `spool bridge --approve <码>`，**任意目录都能敲**）之后他才被放行。
   报"没有这个配对码"时先看它写的是哪个文件——多半找错了工作区。**配对码会出现在**：桥的终端 / 日志
   （`有人要配对：<用户>（码 XXXXXX，工作区 <路径>）`）、**`spool chat` 开局**（可以就地 `/approve 5R3CVG`）、
   `spool run`（散文模式）开局提示；`spool approve --list` 看现状。**批准与撤销即时生效，不需重启桥。**
   三档准入由你显式选：`--access pairing`（默认）/ `allowlist`（只放行 `--allow-user` 里的人）/
   `open`（谁都行，**要显式选**，启动时会警告）。
2. **agent 自己的授权模型不变**：桥把 `--policy` / `--scope` 原样转给子进程，"自动落盘"仍只覆盖 scope
   划定的范围，越界照样退回确认；桥会把待确认的改动列出来并提示回 `y`/`n`。
3. **单条消息有长度上限**（默认 4000 字，`--max-chars` 改），超长直接回一句说明、不交给 agent——一条
   一万字的消息会把这一步的上下文预算占满。

### 管理已放行的人（和换工作区）

**放行是身份级的**（谁 = 哪条通道 + 哪个用户），**与工作区解耦**：一条放行记录的是"此人能使用这几个
工作区"，因此换工作区**不需重新配对**。状态落在**机器级** `<状态目录>/channel-trust.json`。

| 想做什么 | 怎么做 |
|---|---|
| 看这台机器放行了谁、还有谁在等 | `spool approve --list`（机器级 + 各工作区旧文件一起列）|
| 放行一个人 | `spool approve <码>`（或在 `spool chat` 里 `/approve <码>`）|
| 放行到这个工作区（旧式的码） | `spool approve <码> --ws <名字>`（登记表里多个工作区时**必须**点名）|
| **解除某人的放行** | `spool approve --revoke <用户>`；他再发消息会重新拿码 |
| 丢掉一个还没用的码 | `spool approve --forget <码>` |
| **换到另一个工作区** | 聊天里发 `/ws use <名字\|序号>`——**不重启、不重新配对**（"每条聊天一个指向"只在机器级桥下存在）|
| 让两个群各干各的 | 不用跑两个桥：**每个聊天一个指向**（`chat:<通道>:<用户>`），互不影响 |
| 收紧成"只服务一个工作区" | 启动时给 `--root <路径>`（不给 = 服务整台机器）；给了之后这条桥的凭据也从那个工作区的 `.env` 读 |

换工作区**确实是换了一整套上下文**：记忆、索引、检查点、授权范围都挂在工作区上，因此每条聊天有**自己
的指向**（一对"工作区 + 会话"），切换时旧会话原样保留（`/sessions` 列出、`/use <序号>` 切回）。
旧记录由当前通道在启动时**自动认领**（`.agent/bridge-pairings.json` 不删不改）。

### 通道侧的斜杠命令

只有被放行的人可用（处理在准入判定之后），且**只改这条通道自己的状态**：指向、会话。

| 命令 | 做什么 |
|---|---|
| `/ws` · `/ws use <名字\|序号\|路径>` | 列出登记过的工作区（标出当前指向哪个）· 换工作区，下一条消息在新工作区处理 |
| `/sessions` · `/use <序号\|id\|latest>` | 列出这个工作区的会话（已归档的也列）· 切到另一条 |
| `/new [标题]` · `/status` · `/help` | 开一条新会话 · 当前指向的工作区、会话、文件边界与已关联目录 · 这一页 |
| `/allow-read` | 说明远程通道的文件边界（聊天里加不了目录）|

**配对请求不在这条通道上处理**（该通道不开放授权口）：在本机任意目录执行 `spool approve <码>`，或在
`spool chat` 里 `/approve <码>`。`/use` 只在**当前工作区**里挑会话（换工作区要用 `/ws use`，再 `/use`
挑回原来那条）。**授权、关联目录、回滚仍然是人的动作，聊天里不开放。**

### 用它指挥干活时会遇到的几件事

| 情形 | 当前行为 |
|---|---|
| **一轮跑很久**（超过 `--timeout`，默认 900 秒）| 先回「这一轮已超过 N 秒仍未结束，不再等待」，并在结束时补发结论 |
| **连发两条消息** | 第二条回「上一轮仍在运行，等它结束后再发。」——同一工作区**不会**同时跑两个 agent |
| **结论很长** | 自动**分条发**（按字节切，取 3000 字节/条），每条带递增的 `msg_seq`（否则平台按重复消息丢弃）|
| **越界改动要你点头** | 回 `y` 应用、`n` 丢弃（中英文均认："是/好/可以"、"不/否/丢弃"）|
| **它想跑一条白名单外的命令** | 会把那条命令与原因发给你：回 `y` 允许这一次、`a` 始终允许（记在本工作区）、`n` 拒绝、`b` 本轮拒绝该类命令 |
| **想让它读工作区外的目录** | 聊天里加不了。在跑桥的机器上执行 `spool extra add D:\某目录`；已关联的目录可读，写要在聊天里回 `y` 才落盘 |

一条提醒：**闲聊也会真的跑一轮 agent**（它是 agent 的入口，不是客服机器人），因此"你好"这类消息会占一次完整的运行时间。

### 手机上看到的文字是"处理过"的

各通道**渲染能力不一样**，因此发送侧按能力处理，**三种模式**：

| 通道 | 模式 | 手机上看到 |
|---|---|---|
| **QQ** | `fix` | **只去掉不被渲染的强调标记**（`**粗体**` 在手机 QQ 上原样显示成星号），标题、列表、代码块、链接都保留 |
| 企业微信 / 不带 `parse_mode` 的 Telegram | `plain` | 折成纯文本并按窄屏排版：标记全去、表格变逐行「· 文件：改动」、代码块加 `┌─`/`└─` 边界、长段落按标点软折行 |
| 真渲染 Markdown 的通道 | `markdown` | 一个字不动 |

这是**代码里做的**，不靠提示词。两种模式同守一条底线：**只动标记，不改内容**——代码块里的内容、
`sum_to` 这类标识符、`a * b` 的乘法、`parts[0](args)` 这类代码，一个字都不会被动。这一设计的原因见
[`rationale.md#长连接与渲染`](rationale.md#长连接与渲染)。

## 已知边界

- **QQ 官方机器人的两处平台规矩**：事件里 `author` 给的是 **openid**（不是 QQ 号，同一个用户在不同机器人
  下不同），白名单按这个 openid 配；"被动回复"要带 `msg_id`（+`msg_seq`），只在收到消息后 5 分钟内
  有效，桥已自动带上，超时的长任务会走主动推送（那条有配额限制）。
- **QQ 的 `resume` 尚未实现**：网关断开后 5 秒重连，但不带 `op 6 RESUME` 续上 `seq`。平台会把未确认的
  事件重发一段时间，因此短抖动不丢消息，长时间断开可能漏。
- **只处理文本消息**：图片 / 语音 / 事件暂不接入。
- **企业微信回调的解密未实现**：正式回调是 AES 加密的，这一版只保证"明文形状"的解析正确，解密与公网
  入口留在部署层。
- **一轮一条消息、顺序处理**：桥不并发——聊天场景里并发只会让"它在做哪件事"变得说不清。
- **超时是回执而不是失败**：长任务超过 `--timeout` 会回上表那句回执（子进程继续跑），结束后补发结论。
- **回消息时隧道抖动**：网络类失败（握手超时、连不上）会把答复留下，通了再补发；平台业务错误
  （如 `11001`）不留。
- **个人微信与个人 QQ 没有公开的官方接口**。第三方 hook（itchat / wechaty / NapCat 之类）违反服务条款、
  有封号风险，而且等于绕过上面那三件事，因此本包只做官方通道；想在手机上用，最接近的选择是
  **企业微信**（官方、免费档够用、消息可转到微信）或 **Telegram**。

## 加一条新通道

实现 `Channel` 协议即可：`send(text, to)`、`poll() -> list[Incoming]`（没有消息就返回空列表，不要
阻塞）、`close()`，然后在 `cli/commands/bridge.py::_build_channel` 里加一支。装载途径有两条：正式发布
的适配器包走 entry point 组 `spoolkit.channels`，自用的可设
`SPOOLKIT_CHANNEL_PLUGINS=my_channel:build`（工厂签名 `build(spec) -> Channel`）。**内置的 fake /
telegram / wecom / qqbot 优先**，认不出的名字才走插件。测试照着 `tests/bridge/test_channels.py` 写：给假
transport，验请求形状与解析。这一设计的原因见 [`rationale.md#通道与凭据`](rationale.md#通道与凭据)。
