# 上手

这一页只讲"怎么从零跑到第一个任务"，以及**卡住时先查哪一条**。
每条命令的完整参数在 [`cli.md`](cli.md)，内部怎么工作在看 [`manual.md`](manual.md)。

## 1. 装

```powershell
uv venv --python 3.12
uv pip install -e .          # 只装来用
uv pip install -e ".[dev]"   # 开发（带 pytest）
spool --version              # → spool 0.0.1 (spoolkit)
```

装好会有三个命令，指同一份代码：**`spool`**（手敲这个）、`spoolkit`、`agents-dev`
（改名前的旧名，留着免得你写好的脚本失效）。要求 Python ≥ 3.12。

## 2. 起一个模型服务

本机用 llama.cpp 的话：

```powershell
llama-server.exe -m <模型.gguf> --host 127.0.0.1 --port 8080 -c 8192 -ngl 99 --reasoning off
```

两个坑都踩过：**带思考的模型必须加 `--reasoning off`**，否则推理内容会把输出
预算吃光，症状是"模型返回空内容"；`-c` 是服务端真实窗口，Agent 会**向服务端
问**这个值（不写死），所以换模型不用改配置。

云端供应商（验证 agent 循环够用了）：项目根 `.env` 里写 `GEMINI_API_KEY=...`。

## 3. 配一次，以后不用再打

第一次运行会**弹一个向导**问你用哪个供应商（llama.cpp / Gemini / 假模型），
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

`.agent/` 就是"工作区"的标记。三种找法，按顺序：

1. `--root` 显式给了 → 就是它；
2. 从当前目录**往上找** `.agent/` —— 所以在项目的任何子目录里敲命令都对；
3. 全局登记表 `%APPDATA%\spoolkit\workspaces.json`（`spool init` 会登记）——
   所以**在别的地方也能敲** `spool approve <码>`、`spool session` 这类命令。

> 工作区的身份是 `.agent/`，不是"当前在哪"。要读工作区之外的东西，用
> `--allow-read <目录>` 显式授权（只放开读，写入仍限工作区里），**不要**为了
> 看一个目录就把工作区换掉——记忆、索引、检查点都挂在工作区上。

## 5. 第一个任务

```powershell
# 一题一跑
spool run --goal "修好 calc.py 里 sum_to 少算一个的问题，不要改测试"

# 长任务：自己拆解、逐步做完、逐步验收（--scope 是允许自动落盘的范围）
spool run --autonomous --scope "**" --policy auto --limit 20 `
    --goal "把这个工作区里的题目都做对"

# 接着上次没做完的检查点继续
spool run --resume
```

默认**不动你的文件**：写操作先产出 diff，按 `ask / auto / deny` 三档处理。
`--policy auto` 才在 `--scope` 范围内自动落盘，越界仍退回确认；`spool revert`
可以回滚上一次写入。

想看着它做，用网页壳：

```powershell
spool serve                  # 打开 http://127.0.0.1:8765/
```

## 6. 代理

网络默认**走系统代理**（环境里的 `HTTP_PROXY`/`HTTPS_PROXY` 之类，本机设置也读）。
要指定就用 `--proxy http://127.0.0.1:7890` 或 `SPOOLKIT_PROXY`。

注意区分两个代理：**`--proxy` 管模型供应商**出网，**`--bridge-proxy` 管聊天通道**
自己出网（比如 QQ 平台有 IP 白名单，借云主机的 SOCKS5 出去，白名单就固定了）。

## 7. 卡住了先查这里

| 症状 | 多半是什么 | 怎么办 |
|---|---|---|
| 命令说"当前目录不是工作区" | 这里确实没有 `.agent/`，登记表里也没有 | `spool init`，或用 `--root <路径>` |
| `没有这个配对码` | **看错了工作区**（不是码错） | 报错里会写它看的是哪个文件；用 `--root` 指对，或先 `spool init` 登记 |
| 模型返回空内容 | 思考内容吃光了输出预算 | llama-server 加 `--reasoning off` |
| 输出被截断 | 输出预算不够 | 看 `.agent/limits.json`；它支持续写与分轮，真要调就 `spool limits --set 名字=值` |
| 改不动文件，一直问 | 策略是 `ask` | `spool policy --set auto` 并给 `--scope`，或运行时 `--policy auto` |
| 命令被拒 | 不在白名单里 | 它会**申请权限**（本轮允许 / 工作区始终允许 / 拒绝 / 本轮全拒），按提示选 |
| 它说"环境有问题"却查不动 | 工具受限 | 它会登记一条诊断请求：`spool diagnose --list` 看，`--report` 写回结论 |
| 外挂 MCP 工具不见了 | 连不上被跳过 | `spool mcp-servers --check` 真连一遍看报错 |

## 下一步

- 每条命令的参数：[`cli.md`](cli.md)
- 权限、记忆、长任务、子智能体、聊天通道怎么运作：[`manual.md`](manual.md)
- 两个方向的外部接口：[`mcp.md`](mcp.md)（被别的 agent 调用）、[`bridge.md`](bridge.md)（被聊天驱动）
