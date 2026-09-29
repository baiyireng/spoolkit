# 消息通道桥：用聊天驱动 agent

在手机上发一条消息 → agent 在你的工作区里跑一轮 → 把结果发回来。

```
你（手机）→ [通道] → Bridge → AgentRunner → spool run --events（工作区）
                     ↑                                  ↓
                     └──────────── 回复 ← 结局/待确认 ←──┘
```

## 四条通道

| 通道 | 收消息 | 发消息 | 今天能不能用 |
|---|---|---|---|
| `fake` | stdin 一行 | stdout | **能**（不需要任何凭据，也是测试用的那条） |
| `qqbot`（**QQ 官方机器人**） | WebSocket 网关（长连接，**不需要公网入口**） | `POST /v2/users|groups/.../messages` | 能，只要 AppID + AppSecret |
| `telegram` | 长轮询 `getUpdates`（**不需要公网入口**） | `sendMessage` | 能，只要一个 bot token（国内要自备代理） |
| `wecom`（企业微信自建应用） | **回调推送**（需要公网 URL + 解密） | `message/send` | 发消息本机可用；收消息要先把回调接出去 |

## 凭据从哪来

**环境变量 → 工作区 `.env` → 用户级 `.env`**（环境变量优先：它是"这次 shell 的意图"，
文件是长期设置；"更具体的赢"，所以工作区那份盖住用户级那份）。这条链只有一处实现
（`settings.resolve_secret`），通道凭据、Tavily、Gemini 三处共用——三处各写一套的下场是
"这条命令能读到、那条读不到"，而报错还都写着"没配"。

**不给 `--root` 的桥（机器级）没有"工作区那一层"**，凭据只从**用户级 `.env`**与环境变量来：
Windows `%APPDATA%\spoolkit\.env`，其它平台 `~/.config/spoolkit/.env`（`SPOOLKIT_ENV_FILE`
可指到别处）。为什么这么定——2026-09-27 的事故是这样的：在 `C:\Users\32615` 敲
`spool bridge --channel qqbot …`，报"缺少 AppID 与 AppSecret"并建议写进
`C:\Users\32615\.env`，而凭据好好地在 `D:\workSpace\agentWorkSpace\.env` 里。根因是 `--root`
缺省取了 cwd，而 `cwd/.env` 还**盖住**了用户级那份——于是"照着提示在脚下建一个文件"会把
正确配置静默压掉。现在机器级通道不认 cwd（与 `serve` / `hub` / `web` / `mcp` 同一条契约）。

`.env` 里就写成平台给的名字，常见的几种拼写都认：

```ini
# QQ 官方机器人
QQ_AppID=102000000
QQ_AppSecret=xxxxxxxx
```

写进哪一层，一句话：**只在这一个项目里当通道用 → 工作区 `.env`；想在这台机器上任何目录
都能起桥 → 用户级 `.env`**。后者不必记路径：

```powershell
spool env set QQ_AppID=102xxxxxx QQ_AppSecret=xxxxxx   # 写进用户级 .env（不回显你敲的值）
spool env                                             # 看它写在哪、里面有哪些键（只印键名与形状）
spool env import --from D:\workSpace\agentWorkSpace\.env  # 把工作区那份搬上来（来源文件不动）
```

配置完先跑一次 **`--check`**：它把"认到没有、从哪认到的、连不连得上"直接说出来，
**还印一份"找过这些位置"**（只印键名，不印值），不起通道、不跑 agent。
**机器级（不给 `--root`）也能用**——这一条原先做不到：`--check` 直接按 `root/.env` 算，
于是这台机器上唯一能回答"凭据从哪来"的命令，恰恰在最该工作的场合一敲就崩。

输出**形状**（键名与遮罩后的值，形状照 `bridge/credentials.mask`）：

```
$ spool bridge --channel qqbot --check
工作区：机器级（没给 --root，服务整台机器）
找过这些位置：
  环境变量 —— 都没设置（认这些名字：SPOOLKIT_QQ_APPID、AGENTS_DEV_QQ_APPID、QQ_AppID、QQ_APPID）
  [用户级] C:\Users\你\AppData\Roaming\spoolkit\.env —— 里面有：QQ_AppID、QQ_AppSecret
AppID：102***66（用户级 C:\Users\你\AppData\Roaming\spoolkit\.env 里的 QQ_AppID）
AppSecret：C73***Kj（用户级 C:\Users\你\AppData\Roaming\spoolkit\.env 里的 QQ_AppSecret）
换到了 access_token：uhp***Cw
网关地址：wss://…
```

真实的失败长这样（**QQ 机器人开放平台有"IP 白名单"**，把调用方的公网 IP 填进那个应用的
白名单里才行；凭据本身是对的——否则换不到 access_token）：

```
连不上或凭据不对：取网关地址 失败（code=11298）：接口访问源IP不在白名单
```

### IP 白名单：借一台云主机出去（推荐）

家里那条宽带的出口 IP 会变，填进白名单就得天天改。**借一台固定 IP 的云主机
出去**更省事——白名单只填主机那一个 IP：

```bash
# 在云主机上（或经它的 Cloudflare Tunnel）拿到一条 SSH 通路
ssh -D 1080 root@<云主机>          # 本地 1080 就是一个 SOCKS5

# 这条 ssh -D 也可以写成用户配置里的 [[service]] name = "tunnel"，
# 用 spool service start tunnel 拉起（hub 不替你启）。探活用 tcp:127.0.0.1:1080。

# 本机这条命令让**通道**走那条 SOCKS，agent 与工作区都还在本机
spool bridge --channel qqbot --bridge-proxy socks5://127.0.0.1:1080 --policy ask
# 或：spool hub --channel qqbot --bridge-proxy socks5://127.0.0.1:1080
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

常驻入口更省事：`spool hub --channel qqbot`（网页壳 + 桥）。`spool bridge` 只起通道、不起网页。
`--channel fake` 是 stdin/stdout 本地回环，用来测桥；**不要**写成 `--provider fake`
（那是只回放 `--script` 的假模型，不能写代码）。

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

> 凭据也可以写在 `.env` 里（推荐，不用每次开 shell 都设），QQ 那两个键写成
> `QQ_AppID` / `QQ_AppSecret` 就行。**放在哪一层看用途**：这条桥只服务一个工作区 →
> 工作区 `.env`；机器级（不给 `--root`）→ 用户级 `.env`（`spool env set`）。
> 改名前的 `AGENTS_DEV_*` 名字**仍然认**，所以已有的 .bat 不用改。

## 用户配置：`[channel.<名字>]`

起一条通道要敲的那几个参数（`--bridge-proxy` / `--policy` / `--scope` / `--timeout` /
`--web`）**没有一个属于"这一次"**，它们是这台机器上这条通道的默认值。所以它们可以写进
用户配置文件（`spool config --path` 那份）的一个子表里：

```toml
[channel.qqbot]
proxy   = "socks5://127.0.0.1:1080"   # 通道自己出网；等价于 --bridge-proxy
policy  = "ask"                       # 子进程的授权策略，只认 ask / auto / deny
scope   = "**"                        # 允许自动落盘的范围
timeout = 240                         # 一轮最多等多少秒（与 --timeout 同一个单位：秒），要大于 0
web     = true                        # 联网取用
```

这五个键就是 `spool qq` 会补的那五个。这一节里还认一个 `root`（`spool config` 会把它
一起印出来，并在那一行注明"短命令不读这一项"），但**短命令不从配置里读它**：工作区是在
`main()` 那一步解析的，那时"没给 `--root`"（机器级）与"给了空串"已经是同一个值，
回过头再拿配置去补就等于把"服务整台机器"这条契约改掉。要钉住工作区，在命令行上给：
`spool qq --root D:\某个工作区`。

这个子表**只有短命令 `spool qq` 用**（`spool web` 没有通道名，读不了它）。
`spool bridge` / `spool hub` 的参数默认值一个字没变——改长命令等于改既有行为。

优先级 **命令行 > 环境变量 > 用户配置 > 内置默认**：

| 层 | 长什么样 |
|---|---|
| 命令行 | `spool qq --policy ask --timeout 240`。**给了就到此为止**，包括给空串（`--scope ""` = 不许自动落盘）|
| 环境变量 | `SPOOLKIT_CHANNEL_QQBOT_PROXY` / `..._POLICY` / `..._SCOPE` / `..._TIMEOUT` / `..._WEB`（旧前缀 `AGENTS_DEV_CHANNEL_*` 也认，新的优先）|
| 用户配置 | 上面那个 `[channel.qqbot]` |
| 内置默认 | `proxy=""`、`policy=""`、`scope=""`、`timeout=900`、`web=true` |

```powershell
spool qq                    # 参数全来自上面那张表
spool qq --check            # 只看凭据与连通性，不起通道
spool qq --policy auto      # 这一次改一个
```

起手第一屏会印出生效值与来源（在 stderr 上）——**只印这一层真的用上了的那五个键**：

```
[channel.qqbot] 本次生效：proxy=socks5://127.0.0.1:1080(用户配置 [channel.qqbot] …)、
policy=ask(用户配置 …)、scope=**（用户配置 …）、timeout=240.0(用户配置 …)、web=false(用户配置 …)
```

为什么要印：这几个值生效在**别的进程**里（桥、网页壳），出问题时唯一能回答"它到底读了
哪一份"的就是这一行。同理，**这一节里写错的键名会在起手时被指出来**——`proxi` 这种拼写
的症状是"设置了却没生效"，不说出来就只能靠复现去猜：

```
[channel.qqbot] 里这些键不认识（等于没配）：proxi
    可用的键：proxy、policy、scope、timeout、web、root
```

**取值写错同样会被指出来**（键名之外的另一半）。命令行那条路上 argparse 的 `choices`
挡着拼错的取值，配置文件这条路原先谁都不看——`policy = "AUTO"` 不报任何错，然后在
`settle.py` 里落到"问"那一条路上，症状正是"我明明配了自动，它还在问我"：

```
[channel.qqbot] policy 的值不在认得的取值里（现在写的是 'AUTO'；可用：ask、auto、deny），这一层跳过
[channel.qqbot] timeout 的值要的是大于 0 的秒数（现在写的是 -5），这一层跳过
[channel.qqbot] web 的值读不成真假（现在写的是 '大概'；写 true / false），这一层跳过
```

"这一层跳过"就是字面意思：那一层作废、退回下一层（上面三条都会退回内置默认值），
**但绝不会不说**。`policy` 只认 `ask` / `auto` / `deny` 这三个小写值（与
`spool qq --policy` 的取值逐字相同，空串是"没表态"）；`timeout` 要大于 0——它是
"一轮最多等多少秒"，0 或负数意味着每一轮当场就到点；`web` 认 `true` / `false`
（也认环境变量里自然的 `yes` / `no` / `on` / `off` / `1` / `0`）。

`spool config` 也会把每一节现在是什么、每个值来自哪一层印出来。它印的 `root` 那一行
后面带着一句"短命令不读这一项"——**配了 `root` 也不会生效**，要钉工作区在命令行给
`--root`（理由就是上面那段）。`spool config --set` **不会**把这一节写丢
（它保留 `[channel.*]` / `[[mcp]]` / `[[service]]` 这些子表）。

> `proxy` 指的是**通道自己出网**那条路（`--bridge-proxy`，通常对准本机 `[[service]]`
> 里名叫 `tunnel` 的 SOCKS），**不是**模型供应商的 `--proxy`。写错会让代理一路传进
> agent 子进程、把模型请求也塞进那条隧道——那是两个完全不同的东西。

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

### 管理已放行的人（和换工作区）

**放行是身份级的**（谁 = 哪条通道 + 哪个用户），**跟工作区解耦**：一条放行记的是
"这个人能碰这几个工作区"。所以换工作区**不用重新配对**。

- 状态落在**机器级** `<状态目录>/channel-trust.json`；
- 一条放行的默认范围只有一个工作区（批准时当时那个），想多给就在新工作区里再批一次
  （`spool approve <码>`，或 `spool approve <码> --ws <名字>` 点名）；旧记录由当前通道
  在启动时**自动认领**（`.agent/bridge-pairings.json` 不删不改，老流程继续能用）。

| 想做什么 | 怎么做 |
|---|---|
| 看这台机器放行了谁、还有谁在等 | `spool approve --list`（机器级 + 各工作区旧文件一起列）|
| 放行一个人 | `spool approve <码>`（或在 `spool chat` 里 `/approve <码>`）|
| 放行到这个工作区（旧式的码） | `spool approve <码> --ws <名字>`（登记表里多个工作区时**必须**点名）|
| **解除某人的放行** | `spool approve --revoke <用户>`；他再发消息会重新拿码 |
| 丢掉一个还没用的码 | `spool approve --forget <码>` |
| **换到另一个工作区** | 聊天里发 `/ws use <名字\|序号>`——**不重启、不重新配对**（"每条聊天一个指向"只在机器级桥下存在，钉住一个工作区的桥里没得可切）|
| 让两个群各干各的 | 不用跑两个桥：**每个聊天一个指向**（`chat:<通道>:<用户>`），互不影响 |
| 想收紧成"只服务一个工作区" | 启动时给 `--root <路径>`（不给 = 服务整台机器）；给了之后这条桥的凭据也从那个工作区的 `.env` 读 |

注意换工作区**确实是换了一整套上下文**：记忆、索引、检查点、授权范围都挂在工作区
上，所以每条聊天有**自己的指向**（一对"工作区 + 会话"），切换时旧会话原样还在
（`/sessions` 列出来、`/use <序号>` 切回去）。机器级放行只是让"人还是同一个人"
这件事不用再做一遍。

不给 `--root` 时，新来的人要拿码得有地方可指，顺序是：
**这条聊天的指向 → 登记表（只有一个时）→ 登记表为空时退回桥的启动目录**。
登记表里**有**工作区却说不清是哪一个时，**不发码**，回一句让主人先指定；
登记表一个都没有（装机第一天）才用启动目录，否则没人能完成第一次配对。
最后那一条是"机器级入口不认 cwd"的**唯一例外**，刻意留着：它的场合是"这台机器上还没有
任何工作区"，而不是"你碰巧站在哪个目录"。除此之外，桥每轮解析目标、以及读凭据，都**不看
cwd**（`bridge/core.py::target_for`、`cli/commands/bridge.py::target_resolver` 传的都是
`use_cwd=False`）。

### 通道侧的斜杠命令

只有被放行的人能用（处理在准入判定之后），而且**只改这条通道自己的状态**：
指向、会话。授权、关联目录、回滚仍然是人的动作，聊天里不开放。

| 命令 | 做什么 |
|---|---|
| `/ws` | 列登记过的工作区（标出现在指向哪个）|
| `/ws use <名字\|序号\|路径>` | 换工作区——下一条消息就在那儿干活，**不用重新配对** |
| `/sessions` | 列这个工作区的会话（标出当前那条、已归档的也列）|
| `/use <序号\|id\|latest>` | 切到这个工作区里的另一条会话 |
| `/new [标题]` | 开一条新会话，接着往它写 |
| `/status` | 现在指着哪个工作区、哪条会话；文件边界与已关联目录 |
| `/allow-read` | 说明远程通道的文件边界（聊天里加不了目录）|
| `/help` | 上面这一页 |

**配对请求不在这条通道上处理**（这条通道不开放授权口）：在本机任意目录敲
`spool approve <码>`，或者在 `spool chat` 里 `/approve <码>` / `/approve --list` /
`/approve --revoke <用户>`。

`/use` 只在**当前工作区**里挑会话（会话属于工作区，不能一步跨两个）；换工作区用
`/ws use`，再 `/use` 挑回原来那条。

### 用它真的指挥干活时，会遇到的四件事

这四条都是"拿 QQ 指挥 agent 改代码"实测出来的，属于使用形状而不是 bug：

| 情形 | 现在的行为 |
|---|---|
| **一轮跑很久**（超过 `--timeout`，默认 900 秒） | 先回一句"我先不等了"，**跑完后把结论补发给你**（原先那句之后就没了下文，结论直接丢掉）|
| **连发两条消息** | 第二条回"上一轮还在跑，等它结束再发。"——同一个工作区里**不会**同时跑两个 agent（记忆/检查点/进度文件是同一份）|
| **结论很长** | 自动**分条发**（按字节切，中文一个字 3 字节，取 3000 字节/条），并且每条带递增的 `msg_seq`（不然平台按重复消息丢掉）|
| **越界改动要你点头** | 回 `y` 应用、`n` 丢弃（中英文都认：是/好/可以、不/否/丢弃）|
| **它想跑一条白名单外的命令** | 会把那条命令和原因发给你：回 `y` 允许这一次、`a` 始终允许（记在本工作区）、`n` 拒绝、`b` 这类命令本轮别再问 |
| **想让它读工作区外的目录** | 聊天里加不了。主人在跑桥的那台机器上 `spool extra add D:\某目录`（`spool extra remove` 删掉）。已关联的目录可以读，写要聊天里回 `y` / 网页点应用才落盘。没关联过的盘 / 桌面 / 下载够不到。这条限制写给模型，不主动推到聊天里；`/help`、`/status` 才说明。 |

命令的完整清单与准入规则见上一节；`/status` 会顺带打印文件边界、已关联目录与配对文件。

### 手机上看到的文字是"处理过"的

模型爱写 Markdown。各通道**渲染能力不一样**，所以发送侧按能力处理，**三种模式**：

| 通道 | 模式 | 手机上看到 |
|---|---|---|
| **QQ** | `fix` | **只去掉不被渲染的强调标记**（`**粗体**` 在手机 QQ 上原样显示成星号），标题、列表、代码块、链接**都留着**——它们本来就有格式效果 |
| 企业微信 / 不带 `parse_mode` 的 Telegram | `plain` | 折成纯文本并按窄屏排版：标记全去、表格变逐行「· 文件：改动」、列表变 `·`、代码块加 `┌─`/`└─` 边界、长段落按标点软折行、空行收敛 |
| 真渲染 Markdown 的通道 | `markdown` | 一个字不动 |

这是**代码里做的**，不靠提示词——让模型"别写 Markdown"既不可靠、又白占这个项目的
命根子（上下文预算）。两个模式都守着同一条底线：**只动标记，不改内容**——
代码块里的东西、`sum_to` 这类标识符、`a * b` 的乘法、`parts[0](args)` 这种代码，
一个字都不会被动。

（旧版 PC QQ 可能提示"不支持该格式的消息"，那是客户端老，正常。）

一句提醒：**闲聊也会真的跑一轮 agent**（它就是 agent 的入口，不是客服机器人）。
所以"你好"这类消息会占一次完整的运行时间。想要更短的反馈，用能说清目标的一句话。

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
- **回消息时隧道抖了**：网络类失败（握手超时、连不上）会把答复留下，通了再补发。
  平台业务错误（如 `11001`）不留。真机踩过：查完天气 SOCKS 握手超时，结论没发出去。

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
