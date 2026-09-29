# 上手

这一页只讲"怎么从零跑到第一个任务"，以及**卡住时先查哪一条**。
每条命令的完整参数在 [`cli.md`](cli.md)，内部怎么工作在看 [`manual.md`](manual.md)。

## 1. 装

```powershell
# 装成全局命令：装完**任意目录**都能敲 spool（uv 把启动器放进 ~/.local/bin，
# 那目录通常已经在 PATH 上；不在的话 uv 会提示你加）
uv tool install --editable .

spool --version              # → spool 0.0.1 (spoolkit)
```

装好会有三个命令，指同一份代码：**`spool`**（手敲这个）、`spoolkit`、`agents-dev`
（改名前的旧名，留着免得你写好的脚本失效）。要求 Python ≥ 3.12。

两种装法的区别，**这条最容易踩**：

| 装法 | 命令在哪 | 改代码后 |
|---|---|---|
| `uv tool install --editable .` | `~\.local\bin`，已在 PATH 上 → 任意目录可用 | 立刻生效（指向本仓库源码）|
| `uv venv` + `uv pip install -e .` | 只在 `.venv\Scripts\`，**不在 PATH 上** | 要先 `activate`，否则只能写完整路径 |

`--editable` 的代价是**别删或移动这个仓库**（全局那条 `spool` 指着它）。
要一份独立的：`uv tool install --reinstall .`（不带 `--editable`）。
卸载：`uv tool uninstall spoolkit`。

从 GitHub 装（还没发 PyPI）：

```powershell
uv tool install git+https://github.com/baiyireng/spoolkit
pipx install git+https://github.com/baiyireng/spoolkit   # 用 pipx 也行
```

## 2. 起一个模型服务

本机用 llama.cpp 的话：

```powershell
llama-server.exe -m <模型.gguf> --host 127.0.0.1 --port 8080 -c 8192 -ngl 99 --reasoning off
```

两个坑都踩过：**带思考的模型必须加 `--reasoning off`**，否则推理内容会把输出
预算吃光，症状是"模型返回空内容"；`-c` 是服务端真实窗口，Agent 会**向服务端
问**这个值（不写死），所以换模型不用改配置。

密钥分两层，**更具体的赢**：项目专用写工作区 `.env`（`<工作区>/.env`，跟着仓库走、
由 `.gitignore` 排除），机器级/个人默认写**用户级 `.env`**——Windows
`%APPDATA%\spoolkit\.env`，其它平台 `~/.config/spoolkit/.env`，与 `config.toml` 同目录
（`SPOOLKIT_ENV_FILE` 可指到别处）。解析顺序只有一条，写在
`spoolkit/settings.py::resolve_secret`：**环境变量 → 工作区 `.env` → 用户级 `.env`**。

云端供应商（验证 agent 循环够用了）：`GEMINI_API_KEY=...`。只在这一个项目里用就写工作区
`.env`；想在这台机器上任何目录都能用——**包括通道、网页壳、MCP 这些机器级入口**——就写
用户级：

```powershell
spool env set GEMINI_API_KEY=...      # 写进用户级 .env（只印键名与形状，不回显你敲的值）
spool env                             # 它在哪、里面有哪些键；顺带印 config.toml 的路径
spool env import --from .env          # 把工作区那份搬上去（来源文件不动）
```

网页搜索（`research` 先搜再读）要 Tavily key：`TAVILY_API_KEY=...`，两层都认（环境变量里的
`SPOOLKIT_TAVILY_API_KEY` 也认）。旧位置是用户配置里的 `tavily_api_key = "..."`——写在那儿的
不用搬家。没有 key 时只能直接给 URL，不会去抓搜索结果页。

## 3. 配一次，以后不用再打

第一次运行会**弹一个向导**问你用哪个供应商（llama.cpp / Gemini；假模型只用于测试回放，不能写代码）。
每一条都可以回车跳过。想自己设也行：

```powershell
spool config --set provider=llamacpp --set base_url=http://127.0.0.1:8080
spool config                 # 看生效值 + 每一项是从哪来的
```

优先级是**命令行 > 环境变量 > 用户配置 > 内置默认**。用户配置在
`%APPDATA%\spoolkit\config.toml`（其它平台 `~/.config/spoolkit/config.toml`）。

配置分三层，**越具体越赢**：

| 层 | 放什么 | 在哪 |
|---|---|---|
| 命令行 / 环境变量 | **这一次运行**的意图 | `--provider …` / `SPOOLKIT_PROVIDER`（改名前的 `AGENTS_DEV_*` 也认）|
| 工作区级 | 这个项目自己的东西 | `.agent/limits.json`、`.agent/policy.json`，以及密钥的 `<工作区>/.env` |
| 用户级 | **这台机器上的你** | `%APPDATA%\spoolkit\config.toml` + `%APPDATA%\spoolkit\.env`（其它平台 `~/.config/spoolkit/`）|

两条分工别混：**密钥只放 `.env`，非密钥默认值只放 `config.toml`**。`config.toml` 的键是白名单
（写错一个字母的症状是"设置了却没生效"，而那种问题最难查），密钥得能随手加一行、能被
`.gitignore` 一句话排除——所以两者不同居一份文件。用户级 `.env` 与 `config.toml` 同目录，
因为它们是同一层，分两处只会让人记不住哪个在哪。

于是"从哪来"有两套顺序，都由 `spoolkit/settings.py` 一处决定，不再各写一套：

- **非密钥默认值**（`spool config` 管的那些）：命令行 > 环境变量 > 用户配置 > 内置默认；
- **密钥**：环境变量 > 工作区 `.env` > 用户级 `.env`（通道凭据、Tavily、Gemini 三处共用）。

用户级 `.env` 不用去记那个路径，三条命令够了：

```powershell
spool env                                    # 路径 + 里面有哪些键（只印键名与遮罩形状，不印值）+ config.toml 路径
spool env path                               # 只印路径（给脚本用）
spool env set QQ_AppID=102… QQ_AppSecret=…    # 写进去：同键替换，注释/空行/别人的键原样保留
spool env import --from .env --only GEMINI_API_KEY   # 只搬指定的键；来源文件不动
```

`set` 与 `import` **都不回显你敲进来的值**：密钥进过一次终端，就等于进过一次滚动缓冲、
一次截图、一次粘贴——不合形状的参数只报个数，`--only` 没命中的也只报个数并列出"文件里真有
哪些键"。写入是原子的（同目录临时文件 + 替换），失败或 Ctrl-C 不会留下含密钥的临时文件。

只在这个 shell 生效的话用环境变量：`SPOOLKIT_PROVIDER`、`SPOOLKIT_BASE_URL`、
`SPOOLKIT_MODEL`、`SPOOLKIT_PROXY`（旧的 `AGENTS_DEV_*` 名字仍然认）。`spool config` 的页脚
印的就是这条顺序。

## 4. 在任意目录起手

```powershell
cd D:\我的项目
spool init                   # 建 .agent/（记忆、索引、检查点、策略都放这）
spool                        # 什么都不带 → 进对话
```

也可以省掉 `init`：**直接敲 `spool`**，它会问一句"当前目录还不是工作区，
在这里初始化吗？"，答完就进对话。

`.agent/` 就是"工作区"的标记。**CLI 认当前目录**（往上找 `.agent/`），顺序是：

1. `--root` 显式给了 → 就是它；
2. 从当前目录**往上找** `.agent/` —— 所以在项目的任何子目录里敲命令都对；
3. **客户端指向**（`spool ws use <名字>` 写的那一份）——所以"上次在这儿干过活"
   本身就是一条线索；
4. 全局登记表 `%APPDATA%\spoolkit\workspaces.json`（`spool init` 会登记）——
   只有一个时用它；有多个又说不出是哪个就报错让你点名，**不猜**；
5. 都不成立时退回当前目录（交给具体命令报它自己的错）。

**机器级入口不看当前目录**（内核里的 `resolve_target(use_cwd=False)`）：
`spool serve` / `spool hub` / `spool web` / `spool bridge` / `spool qq` / `spool mcp`
不给 `--root` 时，它们服务**整台机器**，
"你碰巧站在哪个目录"与"它服务谁"毫无关系。为什么这条要钉死：真实事故是在
`C:\Users\32615` 敲 `spool bridge --channel qqbot …`，它去找 `C:\Users\32615\.env`，而凭据在
`D:\workSpace\agentWorkSpace\.env` 里；更坏的是 `cwd/.env` 会**盖住**用户级那份，于是"照着
提示在脚下建一个 .env"反而把正确配置压掉了。

| 入口 | 认 cwd 吗 | 没工作区可指时 |
|---|---|---|
| CLI（`run` / `chat` / `plan` / `session` …）| **认**（往上找 `.agent/`）| 退回当前目录，由命令报它自己的错 |
| `spool serve` / `spool hub` / `spool web` | **不认** | 以"还没有目标"起来，让你点名 |
| `spool bridge` / `spool qq` | **不认** | 报错，并回一句让主人先指定工作区（**一处例外**：登记表一个都没有时退回启动目录，否则装机第一天没人能完成第一次配对，见 [`bridge.md`](bridge.md)）|
| `spool mcp` | **不认** | 每次 `delegate_task` 点名一个工作区 |

说不清的时候它们**让你点名，不猜**：宁可要你打一个 `--root` 或 `/ws use`，也不拿 cwd 顶数——
猜错的症状是"这个命令的记忆跟别的命令不是同一份"，那是最难查的一类问题。

**如果你的老习惯是 `cd 某个项目; spool serve`，升级后要给它加 `--root .`**（或先 `spool ws add`
把那个项目登记一次）：`serve` / `hub` 不给 `--root` 时**不再认 cwd**，老写法现在会以"还没有目标"
起来（服务整台机器、页面先空着），而不是服务你脚下那个项目。

两条**短命令**就是给"在任意目录敲一下就能起手"这件事准备的。它们是上面两条的短写，
参数面与行为完全一致，`serve` / `hub` / `bridge` **原样保留、语义不变**：

```powershell
spool web        # = spool hub 不带通道：机器级网页壳，进去再选工作区与会话
spool qq         # = spool bridge --channel qqbot：参数来自用户配置，不认 cwd
spool qq --check # 先看一眼凭据认到没有、从哪认到的（不起通道）
```

`spool qq` 那几个参数（通道出网的代理、授权策略、落盘范围、超时、要不要联网）默认从用户配置
的 `[channel.qqbot]` 小节来，按 **命令行 > 环境变量 > 用户配置 > 内置默认** 算，命令行永远
压得住；凭据走**用户级 `.env`**（`spool env set QQ_AppID=… QQ_AppSecret=…`）。所以"在
`C:\Users\你` 直接敲一下"真的能起来。怎么写见 [`bridge.md`](bridge.md)，命令参数见
[`cli.md`](cli.md)。

登记过多个工作区之后，给常用的起个短名，之后在哪儿都能按名字用：

```powershell
spool ws                     # 看有哪些、现在指着哪个
spool ws alias 我的项目 proj  # 起个短名
spool ws use proj            # 切过去；聊天通道里是 /ws use proj
```

> 工作区的身份是 `.agent/`，不是"当前在哪"。要碰工作区之外的目录，在本机
> `spool extra add <目录>`（读放开，写要确认；聊天里加不了），**不要**为了
> 看一个目录就把工作区换掉——记忆、索引、检查点都挂在工作区上。切换是运行时
> 的事（`spool ws use`），信任边界不再跟着工作区重来一遍。

## 5. 第一个任务

```powershell
# 一题一跑
spool run --goal "修好 calc.py 里 sum_to 少算一个的问题，不要改测试"

# 长任务：自己拆解、逐步做完、逐步验收（--scope 是允许自动落盘的范围）
# 没写 --policy 时会自动提权为 auto；边界仍由 --scope 守
spool run --autonomous --scope "**" --limit 20 `
    --goal "把这个工作区里的题目都做对"

# 接着上次没做完的检查点继续
spool run --resume
```

默认**不动你的文件**：写操作先产出 diff，按 `ask / auto / deny` 三档处理。
`--policy auto` 才在 `--scope` 范围内自动落盘，越界仍退回确认。`--autonomous`
若没写 `--policy`，会提权为 auto（免得无人值守时写操作全被拒、白跑一趟）。
`spool revert` 可以回滚上一次写入。

想看着它做，用常驻入口（联网默认开，和 CLI / QQ 同一套）：

```powershell
spool hub                    # 打开 http://127.0.0.1:8765/ （网页壳）
spool hub --channel qqbot    # 网页壳 + 聊天桥；本机 llama / 隧道用 spool service
```

**这两条在任意目录都能起**：不给 `--root` 时它是**机器级**的——一个进程服务整台机器，
"当前目标"可以换。初始目标按内核顺位定（网页壳的指向 → 登记表），**不认当前目录**；
定不出来就以"还没有目标"起来（`/state` 给形状相同的空态与一句人话，`/run` / `/confirm`
返回 409，`/events` 先等着），终端那行会打印"机器级网页壳：目标是 …"或"还没定，在页面上选"。
给了 `--root` 就是**钉住**那一个工作区（脚本、`.bat` 靠这个），此时 `POST /target` 返回 409
并告诉你去掉 `--root` 重启就是机器级；页面上那两条选择栏也**灰着**并写明原因。

**绑到非回环地址时只读**：`--host 0.0.0.0`（或任何局域网地址）下这条壳只能看——页面、
`/state`、`/events` 照常，`/run` 与 `/confirm` 返回 403 并说明"远程只能看"，起手那行也会说。
要驱动它就在本机开一个，或者绑回 `127.0.0.1`。详见
[`manual.md` 的"非回环地址上只读"](manual.md#非回环地址上只读设计-37)。

换目标走页面**顶上那条选择栏**（工作区 / 会话两条下拉，原生控件、零依赖）：选一下就切，
页面顶部一直摆着当前目标的全路径——"我在哪个工作区"必须一眼看得出。下拉里的备选是
`GET /state` 的 `workspaces` / `sessions`，切换发的请求就是 `POST /target`
（body `{"workspace": …, "session": …}`，两个都可选）：工作区认名字 / 序号 / 路径，
会话不点名就沿用当前那条。**切换不杀任务、不阻塞**——正在跑的那条留在它自己的
(工作区, 会话) 里继续跑，切回去事件与历史都还在（同一个 (工作区, 会话) 复用同一个 Runner）。

页面上显式选一次，还会写进**网页壳的指向**（`clients.json` 里 `web` 那条）：刷新页面停得住，
**重启服务也停得住**。反过来，机器级启动时由内核顺位定出来的初始目标**不写**指向——
那是内核推断的，不是人选的结果（口径与 `spool ws use` 一致）。

谁在跑看 `GET /state` 的 `running_targets`（带 `current` 标记），页面会为**别处**还在跑的
那些各显示一行（工作区 / 会话 / 目标）；`/events` 在目标变了时会**退订旧的 → 订阅新的 →
补一帧 `state`**，页面据此整屏重绑（不是只更新横幅），所以不必重连 `EventSource`。

只起网页、不拉通道也可以 `spool serve`。`--channel fake` 是**聊天通道**的本地回环
（stdin/stdout），和 `--provider fake`（只回放 `--script` 的假模型）不是一回事。

## 6. 代理

网络默认**走系统代理**（环境里的 `HTTP_PROXY`/`HTTPS_PROXY` 之类，本机设置也读）。
要指定就用 `--proxy http://127.0.0.1:7890` 或 `SPOOLKIT_PROXY`。

注意区分两个代理：**`--proxy` 管模型供应商**出网，**`--bridge-proxy` 管聊天通道**
自己出网（比如 QQ 平台有 IP 白名单，借云主机的 SOCKS5 出去，白名单就固定了）。
本机这条 SOCKS 常写成用户配置 `[[service]]` 里的 `tunnel`（`ssh -D`），用
`spool service start tunnel` 拉起；hub 只探活、不启。

## 7. 卡住了先查这里

| 症状 | 多半是什么 | 怎么办 |
|---|---|---|
| 命令说"当前目录不是工作区" | 这里确实没有 `.agent/`，登记表里也没有 | `spool init`，或用 `--root <路径>` |
| `没有这个配对码` | **看错了工作区**（不是码错） | 报错里会写它看的是哪个文件；旧式的码用 `spool approve <码> --ws <名字>` 点名工作区，或先 `spool init` 登记 |
| 模型返回空内容 | 思考内容吃光了输出预算 | llama-server 加 `--reasoning off` |
| 终端几分钟没有新行 | llama.cpp 非流式，整段生成完才返回 | 满 15 秒会报「模型生成中，已等待…」；仍等到现有上限。假模型缺脚本会立刻退出，不会替你写代码 |
| 假模型立刻退出 / 不写代码 | 用了 `--provider fake` 却没给 `--script`，或把它当没 LLM 的兜底 | 真实任务用 llamacpp / gemini。fake **只回放脚本** |
| 网页起来了但手机没消息 | 用了 `serve`，或 `hub` 没加 `--channel` | 常驻带聊天：`spool hub --channel …` |
| 输出被截断 | 输出预算不够 | 看 `.agent/limits.json`；它支持续写与分轮，真要调就 `spool limits --set 名字=值` |
| 改不动文件，一直问 | 策略是 `ask` | `spool policy --set auto` 并给 `--scope`，或运行时 `--policy auto` |
| 命令被拒 | 不在白名单里 | 它会**申请权限**（本轮允许 / 工作区始终允许 / 拒绝 / 本轮全拒），按提示选 |
| 报 `No module named 'socksio'` | 通道要经 SOCKS 出去但缺可选依赖 | 虚拟环境 `uv pip install "httpx[socks]"`；uv 工具装的 `uv tool install --reinstall --editable ".[socks]"` |
| 它说"环境有问题"却查不动 | 工具受限 | 它会登记一条诊断请求：`spool diagnose --list` 看，`--report` 写回结论 |
| 外挂 MCP 工具不见了 | 连不上被跳过 | `spool mcp-servers --check` 真连一遍看报错 |
| "我明明填了密钥" | 放错了层：机器级服务读的是**用户级** `.env`，工作区那份它看不见 | `spool env` 看用户级那层有哪些键；报错里那段"找过这些位置"会印**找过哪儿、那儿有哪些键名**（只印键名，不印值）。搬上去：`spool env import --from <工作区的 .env>` |
| 报错说"`QQ_AppID=`有这些键但值是空的" | 键写了、值空着，等于没配 | 补上值（`spool env set QQ_AppID=…`）；空值最会骗过自己 |
| 机器级入口说"还没有选工作区" | 没给 `--root`，而这条聊天的指向与登记表都说不清是哪一个 | `spool ws use <名字>` 点名，或给 `--root <路径>` 起；它**不会**拿当前目录顶数 |
| 想知道桥的凭据从哪来、连不连得上 | 只看一眼，不想起通道 | `spool bridge --channel <通道> --check`（**机器级也能用**：会印凭据从哪个位置来的、遮罩后的值、以及"找过这些位置"）|
| 网页壳里切不了工作区 | 这条壳是 `--root` 起的，只有一个目标 | 去掉 `--root` 重启（机器级），再在页面顶上的选择栏里选一个 |

## 下一步

- 每条命令的参数：[`cli.md`](cli.md)
- 权限、记忆、长任务、子智能体、聊天通道怎么运作：[`manual.md`](manual.md)
- 两个方向的外部接口：[`mcp.md`](mcp.md)（被别的 agent 调用）、[`bridge.md`](bridge.md)（被聊天驱动）
