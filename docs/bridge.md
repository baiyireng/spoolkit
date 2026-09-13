# 消息通道桥：用聊天驱动 agent

在手机上发一条消息 → agent 在你的工作区里跑一轮 → 把结果发回来。

```
你（手机）→ [通道] → Bridge → AgentRunner → spool run --events（工作区）
                     ↑                                  ↓
                     └──────────── 回复 ← 结局/待确认 ←──┘
```

## 三条通道

| 通道 | 收消息 | 发消息 | 今天能不能用 |
|---|---|---|---|
| `fake` | stdin 一行 | stdout | **能**（不需要任何凭据，也是测试用的那条） |
| `qqbot`（**QQ 官方机器人**） | WebSocket 网关（长连接，**不需要公网入口**） | `POST /v2/users|groups/.../messages` | 能，只要 AppID + AppSecret |
| `telegram` | 长轮询 `getUpdates`（**不需要公网入口**） | `sendMessage` | 能，只要一个 bot token（国内要自备代理） |
| `wecom`（企业微信自建应用） | **回调推送**（需要公网 URL + 解密） | `message/send` | 发消息本机可用；收消息要先把回调接出去 |

## 凭据从哪来

**环境变量 → 项目根 `.env`**（后者优先不了一点：环境变量是"这次 shell 的意图"）。
`.env` 里就写成平台给的名字，常见的几种拼写都认：

```ini
# QQ 官方机器人
QQ_AppID=102000000
QQ_AppSecret=xxxxxxxx
```

配置完先跑一次 **`--check`**：它把"认到没有、从哪认到的、连不连得上"直接说出来，
不起通道、不跑 agent——

```
$ spool bridge --channel qqbot --check
工作区：D:\workSpace\spoolkit
凭据文件：D:\workSpace\spoolkit\.env
AppID：102***66（D:\workSpace\spoolkit\.env 里的 QQ_AppID）
AppSecret：C73***Kj（D:\workSpace\spoolkit\.env 里的 QQ_AppSecret）
换到了 access_token：uhp***Cw
连不上或凭据不对：取网关地址 失败（code=11298）：接口访问源IP不在白名单
```

（上面最后一行是真实的：**QQ 机器人开放平台有"IP 白名单"**，把调用方的公网
IP 填进那个应用的白名单里才行。凭据本身是对的——否则换不到 access_token。）

### IP 白名单：借一台云主机出去（推荐）

家里那条宽带的出口 IP 会变，填进白名单就得天天改。**借一台固定 IP 的云主机
出去**更省事——白名单只填主机那一个 IP：

```bash
# 在云主机上（或经它的 Cloudflare Tunnel）拿到一条 SSH 通路
ssh -D 1080 root@<云主机>          # 本地 1080 就是一个 SOCKS5

# 本机这条命令让**通道**走那条 SOCKS，agent 与工作区都还在本机
spool bridge --channel qqbot --bridge-proxy socks5://127.0.0.1:1080 --policy ask
```

于是腾讯看到的是云主机的 IP（填它进白名单），本机的 agent 照常读写本地工作区。

两点注意：

- **`--bridge-proxy` 和 `--proxy` 不是一回事**：前者管通道自己出网，后者管模型
  供应商（比如 Gemini 要走代理）。两个都可能在用，所以分开。
- HTTP 那一半走 SOCKS 要 httpx 的 socks 可选依赖，按你的装法选一条：
  **虚拟环境**：`uv pip install "httpx[socks]"`；**uv 工具装的**（在仓库里跑）
  `uv tool install --reinstall --editable ".[socks]"`。
  缺了它会报 `No module named 'socksio'`——我们把这句话换成了带装法的提示。
  网关那一半（WebSocket）用的是我们自己的 SOCKS5 握手，不需要额外依赖。
- **`--check` 走的是真正要跑的那条路**（包括 `--bridge-proxy`）。早先它没把代理
  传下去，于是测的是直连——而直连和借云主机出去是两条路，症状是"检查通过、
  真跑失败"。现在检查报告里会明写"通道出网走代理：…"。
- `ssh -D` 需要**服务端允许端口转发**。有些加固过的机器上
  `/etc/ssh/sshd_config.d/99-hardening.conf` 里写着 `AllowTcpForwarding no`，
  症状是 `ssh -D` 连上、但本地 1080 端口谁也连不通。改成 `local` 即可
  （`local` 只允许本地转发，比 `yes` 收得紧），改完 `sshd -t && systemctl reload sshd`。

```powershell
# 本地跑通（不需要任何外部服务）
spool bridge --channel fake --provider llamacpp --policy auto --scope "**" --user me --allow-user me

# 接 Telegram
$env:SPOOLKIT_TELEGRAM_TOKEN = "123456:ABC..."     # @BotFather 给的
spool bridge --channel telegram --allow-user 123456789 --policy auto --scope "src"

# 接 QQ 官方机器人（QQ 机器人开放平台建的应用）
$env:SPOOLKIT_QQ_APPID  = "102xxxxxx"
$env:SPOOLKIT_QQ_SECRET = "xxxxxxxx"
spool bridge --channel qqbot --policy auto --scope "src"     # 沙箱加 --sandbox

# 借云主机出口（白名单只填那台主机）——2026-09-13 真机验证过这条命令
spool bridge --channel qqbot --bridge-proxy socks5://127.0.0.1:1080 --policy ask --scope "src"

# 接企业微信（自建应用）
$env:SPOOLKIT_WECOM_CORP_ID = "ww...."
$env:SPOOLKIT_WECOM_SECRET  = "..."
$env:SPOOLKIT_WECOM_AGENT_ID = "1000002"
spool bridge --channel wecom --allow-user zhangsan --policy ask
```

> 环境变量也可以写在项目根 `.env` 里（推荐，不用每次开 shell 都设），
> QQ 那两个键写成 `QQ_AppID` / `QQ_AppSecret` 就行。改名前的 `AGENTS_DEV_*`
> 名字**仍然认**，所以已有的 .bat 不用改。

## 安全模型（用之前先看这一段）

聊天通道等于把 agent 挂出去了，所以三件事必须同时成立：

1. **默认配对，不认识的人不能用**。陌生发送者会拿到一个一次性配对码，
   你在机器上执行 `spool approve <码>`（等价于 `spool bridge --approve <码>`）
   之后他才被放行。**这条命令在任意目录都能敲**——工作区靠 `.agent/` 或全局
   登记表定位；报"没有这个配对码"时先看它说的是哪个文件，多半是找错了工作区：

   ```
   陌生人：把项目删了
   → 这条通道还不认识你。把这个配对码给机器的主人，他在命令行执行
     `spool approve 5R3CVG` 之后你就能用了：5R3CVG
   ```

   **配对码会出现在哪里**（原先只出现在桥自己的终端上，你人在另一个窗口跑
   `spool chat` 就完全看不到——这条是用户问出来的）：

   - 桥的终端/日志里：`有人要配对：<用户>（码 XXXXXX）`
- **`spool chat` 开局**：有等待中的请求时打一行提示，并且**可以就地批准**
  `/approve 5R3CVG`
- `spool run`（散文模式）开局同样提示；`--events` 模式不打散文明，免得弄脏
  JSON 事件流
- 随手查：`spool approve --list`

**批准/撤销是即时生效的，不需要重启桥**——这一条曾经不成立：桥拿着启动那一刻的
内存快照，你在另一个进程里批准它不认账，得重启才行（而"批准了还不认识我"这种
现象极难自己查出来）。现在读之前会先看文件变没变，变了就重读。

   三档准入由你显式选：`--access pairing`（默认）/ `allowlist`（只放行
   `--allow-user` 里的人）/ `open`（谁都行，**要显式选**，启动时会警告）。
   状态在 `.agent/bridge-pairings.json`：这是工作区级的东西（这台机器上谁
   能驱动这个工作区），和 `.agent/policy.json` 同类。
2. **agent 自己的授权模型不变**。桥把 `--policy` / `--scope` 原样转给子进程，
   所以"自动落盘"仍然只覆盖 scope 划定的范围，越界照样退回确认；桥会把待确认的
   改动列出来并提示回 `y`/`n`。
3. **单条消息有长度上限**（默认 4000 字，`--max-chars` 改），超长直接回一句说明，
   不交给 agent——一条一万字的消息会把这一步的上下文预算吃光。

### 管理已放行的人（和换绑工作区）

**配对记录挂在工作区上**：`.agent/bridge-pairings.json`。桥启动时用 `--root`
决定它服务哪个工作区，所以"绑定"分两层：谁被放行（工作区级）、这条通道服务谁
（启动参数级）。

| 想做什么 | 怎么做 |
|---|---|
| 看这个工作区放行了谁、还有谁在等 | `spool approve --list` |
| 放行一个人 | `spool approve <码>`（或会话里 `/approve <码>`）|
| **解除某人的放行** | `spool approve --revoke <用户>`；他再发消息会重新拿码 |
| 丢掉一个还没用的码 | `spool approve --forget <码>` |
| **换绑到另一个工作区** | 用 `--root <另一个工作区>` 重启桥；那个人在新工作区里还没被放行，让他再发一句拿到新码，在新工作区里批准（`spool approve --list` 看得到）|
| 同时服务两个工作区 | 跑两个桥（两个机器人/两条通道），各自 `--root`，各自一份配对记录 |

注意"换绑"不是改一个设置项就完事：**记忆、索引、检查点、授权范围都挂在工作区
上**，换工作区等于换了一整套上下文。所以我们的做法是"显式重启 + 重新配对"，
而不是让运行时悄悄切换。做成"聊天里发一句就切"也不是不行，但那要先想清楚
"当前工作区"变成可变之后，记忆与信任边界跟着怎么变——那是单独一件事，不顺手加。

还有一条不是技术问题：**个人微信与个人 QQ 没有官方接口**。第三方 hook
（itchat / wechaty / NapCat 之类）违反服务条款、有封号风险，而且等于把上面
那三件事全绕过去了。所以这个包只做官方通道；想在手机上用，最接近的选择是
**企业微信**（官方、免费档够用、消息能转到微信里看）或 **Telegram**。

> 补一句事实核查（2026-09）：OpenClaw 那条路是**由通道插件**接的微信——npm
> scope 属腾讯的 `@tencent-weixin/openclaw-weixin`（文档称由腾讯微信团队维护，
> 扫码登录、走腾讯 iLink API）与 `@tencent-connect/openclaw-qqbot`（官方 QQ Bot
> API）。所以"个人微信没有官方接口"这句话要看语境：**官方接口以"平台自己的插件"
> 形态出现，不是以公开文档的 REST API 形态出现**。本机不装 OpenClaw 的前提下，
> 要用那条路就得自己写一个对接它们的适配器（见下面「加一条新通道」），
> 或者用企业微信/Telegram。

## 分层（为什么这么分）

| 层 | 管什么 | 文件 |
|---|---|---|
| `Channel` | 收与发。**每家的形状不同**（回调/长轮询/socket），差异全关在这里 | `channel.py`、`fake.py`、`telegram.py`、`wecom.py` |
| `Bridge` | 白名单、长度上限、调 runner、把回复发回去 | `core.py` |
| `AgentRunner` | 把一条消息变成一次 agent 运行 | `agent_runner.py` |

`AgentRunner` **复用网页壳那套机制**（`web.runner.Runner`：起 `run --events`
子进程、读事件流），所以聊天入口与网页入口的判定完全一致——同一个授权策略、
同一个工作区、同一份待确认清单。不是另写一套。

通道本身是**可装载的**：核心不认识任何一家通道。

| 装载途径 | 用在什么情形 |
|---|---|
| entry point 组 `spoolkit.channels` | 正经发布出去的适配器包：`[project.entry-points."spoolkit.channels"] my="my_pkg:build"` |
| 环境变量 `SPOOLKIT_CHANNEL_PLUGINS=my_channel:build` | 自己写一个先用起来（个人微信/QQ 那类第三方 hook 的适配器多半是这种形态）。改名前的 `AGENTS_DEV_CHANNEL_PLUGINS` 也认 |

工厂签名是 `build(spec) -> Channel`（spec 里带着 `name` / `root` / `token` /
`proxy`）。**内置的 fake/telegram/wecom 优先**，认不出的名字才走插件——
所以接一家新通道不必改核心，也不必把它的依赖塞进主包。

> 这一层是从 OpenClaw 抄的**唯一一件真正重要的东西**：它的核心仓库一行微信
> 代码都没有，通道全是外部插件。它还教会一件事——**默认配对而不是默认放行**
> （它自己的文档写着微信插件 2.4.8 的访问控制在名单为空时会放行任何发送者，
> 那正是这个默认要避免的）。

## 长连接的时序：两条真机上踩到的坑

两条都不是"网络不稳"，都是我们自己的时序错，而且**单测全绿、只有真机会露出来**：

1. **心跳迟了 20 秒，平台每 60 秒掐一次线。** QQ 给的心跳间隔是 41.25s，而我们
   只在两次 `recv` 之间检查"该不该发心跳"，`recv` 一阻塞就是 20s——心跳总要拖到
   60 秒才发得出去，平台据此判掉线。日志看着像"连上→断开→重连"的死循环。
   修法：读等待**服从下一次心跳的时刻**（`recv(timeout=...)` 只影响这一次调用），
   心跳还提前 1 秒发。132 秒真机挂测（跨 3 个心跳周期）不再断开。
2. **握手时多读到的字节被丢掉。** 服务端把握手响应和第一个数据帧写进同一个 TCP
   段是正常的，读握手那次 `recv` 会把两样一起读回来；当时只取了 `\r\n\r\n`
   前面的头部，后面的帧字节被扔了——`recv()` 于是去等一个**已经到了**的帧，
   直到对端关闭才返回 `None`。症状是"偶发连不上"：单跑测试必过，全量跑（机器更忙）
   才炸。修法：把多读到的字节留在缓冲区里，下次读先吐它。

第 2 条被固定成了确定性用例（`tests/bridge/test_ws.py`：让假服务端**故意**把两样
写进同一次 `sendall`）——偶发问题不固定成用例，就修不住。

## 已知边界

- **QQ 官方机器人的两处平台规矩**：一是事件里 `author` 给的是 **openid**
  （不是 QQ 号，同一个用户在不同机器人下不同），白名单就按这个 openid 配；
  二是"被动回复"要带 `msg_id`（+`msg_seq`），只在收到消息后 5 分钟内有效——
  桥已经自动把它带上（`Incoming.message_id`），超时的长任务会走主动推送
  （那条有配额限制，平台侧可查）。
- **QQ 的 `resume` 还没做**：网关断了就重连（5 秒后），但不带 `op 6 RESUME`
  续上 `seq`——平台会把未确认的事件重发一段时间，所以短抖动不会丢消息，
  长时间断开可能漏。这条留着，等真有抖动再加。
- **只处理文本消息**：图片/语音/事件先不接（`parse_callback` 对它们返回空，
  而不是塞一条读不懂的消息给桥）。
- **企业微信回调的解密没做**：正式回调是 AES 加密的，解密要用 EncodingAESKey。
  这一版只保证"明文形状"的解析正确（形状一样），解密与公网入口留在部署层；
  回调的 `echostr` URL 验证同样属于接入方的 HTTP 服务。
- **一轮一条消息、顺序处理**：桥不并发。聊天场景里并发只会让"它在做哪件事"
  变得说不清。
- **超时是回执而不是失败**：长任务超过 `--timeout`（默认 900 秒）会回一句
  "我先不等了"，子进程继续跑。

## 加一条新通道

实现 `Channel` 协议的两个方法就够了：

```python
class MyChannel:
    name = "my"
    def send(self, text: str, to: str = "") -> None: ...
    def poll(self) -> list[Incoming]: ...   # 没有消息就返回空列表，不要阻塞
    def close(self) -> None: ...
```

然后在 `cli/commands/bridge.py::_build_channel` 里加一支。测试照着
`tests/bridge/test_channels.py` 写：给假 transport，验请求形状与解析。
