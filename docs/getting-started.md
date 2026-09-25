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

云端供应商（验证 agent 循环够用了）：项目根 `.env` 里写 `GEMINI_API_KEY=...`。

网页搜索（`research` 先搜再读）要 Tavily key：同一份 `.env` 写 `TAVILY_API_KEY=...`，
或用户配置里 `tavily_api_key = "..."`。没有 key 时只能直接给 URL，不会去抓搜索结果页。

## 3. 配一次，以后不用再打

第一次运行会**弹一个向导**问你用哪个供应商（llama.cpp / Gemini；假模型只用于测试回放，不能写代码）。
每一条都可以回车跳过。想自己设也行：

```powershell
spool config --set provider=llamacpp --set base_url=http://127.0.0.1:8080
spool config                 # 看生效值 + 每一项是从哪来的
```

优先级是**命令行 > 环境变量 > 用户配置 > 内置默认**。用户配置在
`%APPDATA%\spoolkit\config.toml`（其它平台 `~/.config/spoolkit/config.toml`）。

只在这个 shell 生效的话用环境变量：`SPOOLKIT_PROVIDER`、`SPOOLKIT_BASE_URL`、
`SPOOLKIT_MODEL`、`SPOOLKIT_PROXY`（旧的 `AGENTS_DEV_*` 名字仍然认）。

## 4. 在任意目录起手

```powershell
cd D:\我的项目
spool init                   # 建 .agent/（记忆、索引、检查点、策略都放这）
spool                        # 什么都不带 → 进对话
```

也可以省掉 `init`：**直接敲 `spool`**，它会问一句"当前目录还不是工作区，
在这里初始化吗？"，答完就进对话。

`.agent/` 就是"工作区"的标记。五种找法，按顺序：

1. `--root` 显式给了 → 就是它；
2. 从当前目录**往上找** `.agent/` —— 所以在项目的任何子目录里敲命令都对；
3. **客户端指向**（`spool ws use <名字>` 写的那一份）——所以"上次在这儿干过活"
   本身就是一条线索；
4. 全局登记表 `%APPDATA%\spoolkit\workspaces.json`（`spool init` 会登记）——
   只有一个时用它；有多个又说不出是哪个就报错让你点名，**不猜**；
5. 都不成立时退回当前目录（交给具体命令报它自己的错）。

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
| 终端几分钟没有新行 | llama.cpp 是非流式的，整段生成完才打印 | 等它返回。假模型缺脚本会立刻退出，不会替你写代码 |
| 假模型立刻退出 / 不写代码 | 用了 `--provider fake` 却没给 `--script`，或把它当没 LLM 的兜底 | 真实任务用 llamacpp / gemini。fake **只回放脚本** |
| 网页起来了但手机没消息 | 用了 `serve`，或 `hub` 没加 `--channel` | 常驻带聊天：`spool hub --channel …` |
| 输出被截断 | 输出预算不够 | 看 `.agent/limits.json`；它支持续写与分轮，真要调就 `spool limits --set 名字=值` |
| 改不动文件，一直问 | 策略是 `ask` | `spool policy --set auto` 并给 `--scope`，或运行时 `--policy auto` |
| 命令被拒 | 不在白名单里 | 它会**申请权限**（本轮允许 / 工作区始终允许 / 拒绝 / 本轮全拒），按提示选 |
| 报 `No module named 'socksio'` | 通道要经 SOCKS 出去但缺可选依赖 | 虚拟环境 `uv pip install "httpx[socks]"`；uv 工具装的 `uv tool install --reinstall --editable ".[socks]"` |
| 它说"环境有问题"却查不动 | 工具受限 | 它会登记一条诊断请求：`spool diagnose --list` 看，`--report` 写回结论 |
| 外挂 MCP 工具不见了 | 连不上被跳过 | `spool mcp-servers --check` 真连一遍看报错 |

## 下一步

- 每条命令的参数：[`cli.md`](cli.md)
- 权限、记忆、长任务、子智能体、聊天通道怎么运作：[`manual.md`](manual.md)
- 两个方向的外部接口：[`mcp.md`](mcp.md)（被别的 agent 调用）、[`bridge.md`](bridge.md)（被聊天驱动）
