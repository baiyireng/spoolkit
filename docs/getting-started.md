# 上手

这一页只讲"怎么从零跑到第一个任务"，以及**卡住时先查哪一条**。每条命令的完整参数在
[`cli.md`](cli.md)，它怎么工作在看 [`manual.md`](manual.md)。

## 最短路径

```powershell
uv tool install --editable .                           # 装成全局命令（Python ≥ 3.12）
llama-server.exe -m <模型.gguf> --host 127.0.0.1 --port 8080 -c 8192 -ngl 99 --reasoning off
spool config --set provider=llamacpp --set base_url=http://127.0.0.1:8080
spool config                                           # 看生效值 + 每一项是从哪来的
cd D:\我的项目
spool init                                             # 建 .agent/（记忆、索引、检查点都放这）
spool                                                  # 什么都不带 → 进对话；不是工作区会先问一句
spool run --goal "修好 calc.py 里 sum_to 少算一个的问题，不要改测试"
```

装法有两种，这条最容易踩：`uv tool install --editable .` 装到 `~\.local\bin`（已在 PATH 上，任意目录可用，改代码立刻生效，代价是别删或移动这个仓库）；`uv venv` + `uv pip install -e .` 只在 `.venv\Scripts\`、**不在 PATH 上**，得先 `activate`。
卸载 `uv tool uninstall spoolkit`；要一份不跟随源码的用 `uv tool install --reinstall .`；从 GitHub 装用 `uv tool install git+https://github.com/baiyireng/spoolkit`。

## 密钥放哪

**密钥只放 `.env`，非密钥默认值只放 `config.toml`。** 解析顺序是 **环境变量 → 工作区 `.env` →
用户级 `.env`**（更具体的赢）。用户级那份在 `%APPDATA%\spoolkit\.env`（其它平台
`~/.config/spoolkit/.env`，与 `config.toml` 同目录，`SPOOLKIT_ENV_FILE` 可改）。只在这一个项目里
用就写工作区 `.env`；想在这台机器上任何目录都能用——**包括通道、网页壳、MCP 这些机器级入口**——写用户级：

```powershell
spool env                                    # 它在哪、有哪些键（只印键名与遮罩形状）
spool env set GEMINI_API_KEY=...             # 写进用户级 .env（不回显你敲的值）
spool env import --from .env --only GEMINI_API_KEY   # 从工作区那份搬指定的键；来源文件不动
```

`set` 与 `import` **都不回显你敲进来的值**（密钥进过一次终端就等于进过一次滚动缓冲、一次截图），
其余细节见 [`cli.md#spool-env`](cli.md#spool-env)。本机模型不需要任何密钥；Gemini 用
`GEMINI_API_KEY`，网页搜索要 `TAVILY_API_KEY`（旧位置 `config.toml` 里的 `tavily_api_key` 也还认）。

## 配置三层

| 层 | 放什么 | 在哪 |
|---|---|---|
| 命令行 / 环境变量 | **这一次运行**的意图 | `--provider …` / `SPOOLKIT_PROVIDER`（旧的 `AGENTS_DEV_*` 也认）|
| 工作区级 | 这个项目自己的东西 | `.agent/limits.json`、`.agent/policy.json`，以及密钥的 `<工作区>/.env` |
| 用户级 | **这台机器上的你** | `%APPDATA%\spoolkit\config.toml` + `%APPDATA%\spoolkit\.env` |

**非密钥默认值**的顺序是**命令行 > 环境变量 > 用户配置 > 内置默认**，`spool config` 的页脚印的就是这条。**内置默认是 `fake` 假模型，它不能生成代码**，什么都没配时先 `spool config --set provider=…`。
第一次运行会弹向导问你用哪个供应商（每条都能回车跳过，脚本 / CI 用 `--no-setup`），只在这个 shell 生效用 `SPOOLKIT_PROVIDER` / `SPOOLKIT_BASE_URL` / `SPOOLKIT_MODEL` / `SPOOLKIT_PROXY`。

## 工作区与 cwd

`.agent/` 就是"工作区"的标记，**CLI 认当前目录**（往上找 `.agent/`）。顺位是：`--root` 显式给了
就是它 → 往上找 `.agent/` → **客户端指向**（`spool ws use` 写的）→ 全局登记表
`%APPDATA%\spoolkit\workspaces.json`（只有一个时用它，有多个又说不清就报错让你点名，**不猜**）→
退回当前目录。给常用的起个短名就能在哪儿都按名字用：`spool ws alias 我的项目 proj`，之后
`spool ws use proj`（聊天里是 `/ws use proj`）。

**机器级入口不看当前目录**（`serve` / `hub` / `web` / `bridge` / `qq` / `mcp` 不给
`--root` 时），它们服务**整台机器**：

| 入口 | 认 cwd 吗 | 没工作区可指时 |
|---|---|---|
| CLI（`run` / `chat` / `plan` / `session` …）| **认**（往上找 `.agent/`）| 退回当前目录，由命令报它自己的错 |
| `spool serve` / `hub` / `web` | **不认** | 以"还没有目标"起来，让你点名 |
| `spool bridge` / `qq` | **不认** | 报错，让你先指定工作区（**一处例外**：登记表一个都没有时退回启动目录，否则装机第一天没人能完成第一次配对）|
| `spool mcp` | **不认** | 每次 `delegate_task` 点名一个工作区 |

**老习惯是 `cd 某个项目; spool serve` 的话，升级后要加 `--root .`**（或先 `spool ws add` 登记一次）：
不给 `--root` 时它不再认 cwd，老写法会以"还没有目标"起来、服务整台机器，而不是服务你脚下那个
项目。为什么这条要钉死见 [`rationale.md`](rationale.md#cwd-契约)。`spool web` 与 `spool qq` 两条短命令
就是给"在任意目录敲一下就能起手"准备的，分别是 `hub` 不带通道、`bridge --channel qqbot` 的短写
（`spool qq --check` 先看凭据认到没有），`serve` / `hub` / `bridge` 原样保留。

## 第一个任务

```powershell
spool run --goal "修好 calc.py 里 sum_to 少算一个的问题，不要改测试"          # 一题一跑
# 长任务：自己拆解、逐步做完、逐步验收。--scope 是允许自动落盘的范围；
# 没写 --policy 时会自动提权为 auto，边界仍由 --scope 守
spool run --autonomous --scope "**" --limit 20 --goal "把这个工作区里的题目都做对"
spool run --resume                                                          # 接着检查点继续
spool web                    # 任意目录：起网页壳，打开 http://127.0.0.1:8765/ 进去再选工作区
spool hub --channel qqbot    # 网页壳 + 聊天桥；本机 llama / 隧道用 spool service 启停
```

默认**不动你的文件**：写操作先产出 diff，按 `ask / auto / deny` 三档处理；`--policy auto` 才在
`--scope` 范围内自动落盘，越界仍退回确认，`spool revert` 回滚上一次写入。不给 `--root` 起网页壳时
它是**机器级**的：一个进程服务整台机器，"当前目标"（工作区 + 会话）可以换，**切换不杀任务、不阻塞**。

**给了 `--root` 就是钉住**那一个工作区（脚本、`.bat` 靠这个），此时 `POST /target` 返回 409；`--host 0.0.0.0` 这类非回环地址下这条壳**只读**，`/run` 与 `/confirm` 返回 403。端点与只读口径的完整写法见 [`cli.md#spool-serve`](cli.md#spool-serve)；代理用 `--proxy <地址>` 或 `SPOOLKIT_PROXY`（默认走系统代理），注意它管的是**模型供应商**，聊天通道自己出网走的是 `--bridge-proxy`。

## 卡住了先查这里

| 症状 | 多半是什么 | 怎么办 |
|---|---|---|
| 命令说"当前目录不是工作区" | 这里确实没有 `.agent/`，登记表里也没有 | `spool init`，或用 `--root <路径>` |
| 说"假模型只回放 `--script`" | 什么都没配，内置默认是 `fake` | 配真实供应商：`spool config --set provider=llamacpp --set base_url=…` |
| 模型返回空内容 | 思考内容吃光了输出预算 | llama-server 加 `--reasoning off` |
| 终端几分钟没有新行 | llama.cpp 非流式，整段生成完才返回 | 满 15 秒会报「模型生成中，已等待…」；仍等到现有上限 |
| `没有这个配对码` | **看错了工作区**（不是码错） | 报错里会写它看的是哪个文件；旧式的码用 `spool approve <码> --ws <名字>` 点名工作区 |
| 网页起来了但手机没消息 | 用了 `serve`，或 `hub` 没加 `--channel` | 常驻带聊天：`spool hub --channel …` 或 `spool qq` |
| 输出被截断 | 输出预算不够 | 看 `.agent/limits.json`；它支持续写与分轮，真要调就 `spool limits --set 名字=值` |
| 改不动文件，一直问 | 策略是 `ask` | `spool policy --set auto` 并给 `--scope`，或运行时 `--policy auto` |
| 命令被拒 | 不在白名单里 | 它会**申请权限**（本轮允许 / 工作区始终允许 / 拒绝 / 本轮全拒），按提示选 |
| 报 `No module named 'socksio'` | 通道要经 SOCKS 出去但缺可选依赖 | 虚拟环境 `uv pip install "httpx[socks]"`；uv 工具装的 `uv tool install --reinstall --editable ".[socks]"` |
| `ssh -D` 连上但本地端口不通 | 服务端不让端口转发 | 把 `AllowTcpForwarding` 改成 `local`，`sshd -t && systemctl reload sshd` |
| 它说"环境有问题"却查不动 / 外挂 MCP 工具不见了 | 工具受限，或外挂服务连不上被跳过 | `spool diagnose --list` 看它登记的诊断请求（`--report` 写回结论）；`spool mcp-servers --check` 真连一遍看报错 |
| "我明明填了密钥" | 放错了层：机器级服务读的是**用户级** `.env`，工作区那份它看不见 | `spool env` 看用户级那层有哪些键；搬上去：`spool env import --from <工作区的 .env>`；看清来路用 `spool bridge --channel <通道> --check`（机器级也能用）|
| 报错说"`QQ_AppID=`有这些键但值是空的" | 键写了、值空着，等于没配 | 补上值（`spool env set QQ_AppID=…`）；空值最会骗过自己 |
| 机器级入口说"还没有选工作区" | 没给 `--root`，而这条聊天的指向与登记表都说不清是哪一个 | `spool ws use <名字>` 点名，或给 `--root <路径>` 起；它**不会**拿当前目录顶数 |
| 网页壳里切不了工作区、或"开始"点不动 | 前者是 `--root` 起的（只有一个目标）；后者绑了非回环地址（只读） | 前者去掉 `--root` 重启；后者绑回 `127.0.0.1` |

## 下一步

每条命令的参数：[`cli.md`](cli.md)；权限、记忆、长任务、聊天通道怎么运作：[`manual.md`](manual.md)；
两个方向的外部接口：[`mcp.md`](mcp.md)、[`bridge.md`](bridge.md)；为什么这么设计 / 踩过哪些坑：
[`rationale.md`](rationale.md)。
