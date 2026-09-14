# spoolkit

**面向本地小模型的编程特化 Agent**，命令叫 `spool`。

在 4K–8K 的有效上下文预算下，用 7B–27B 级本地模型完成真实的中小型编程任务。
小模型做 Agent 的瓶颈不是"不够聪明"，而是三件事：上下文一塞满质量就断崖式下跌、
多步规划能力弱、以及爱编造。所以整套架构只有一个目标——**让模型每一步面对的
上下文都尽可能小、精确、可承载**。

手段是把状态外置（计划、进度、记忆都在文件与 SQLite 里，历史可以随时丢），
再用代码索引保证"精确取用"而不是"整份塞入"。

现状：**可用但仍是实验品**（0.0.1）。同一批 50 道题（本机 Qwen3.8-27B IQ2_S、
8K 窗口、冷启动服务）：自主编排（自己拆解、自己执行、自己验收）**50/50，
12.7 分钟、263k 输入 token**（第三到六轮：15.6 → 13.8 → 12.5 → 12.7 分钟）；
同协议下"一题一会话"的对照是 50/50、6.5 分钟、141k token。逐轮数字与测量方法在
[`docs/benchmarking.md`](docs/benchmarking.md)——README 不复述，因为**数会随改动变**。

## 为什么用它

- **把本地模型的潜力榨出来**：整套脚手架就是为"小模型 + 小上下文"设计的。
  索引负责精确取用、记忆负责跨任务积累、子智能体负责分开上下文、验收负责兜住
  编造——7B–27B 的本地模型因此能干成真实的中小型任务。不必等更大的模型，
  先把喂给它的上下文弄准。
- **省下付费 Agent 的额度**：边界清楚的活（改一个函数、补一组测试、整理一个
  目录、跑一遍回归）交它在本机做，不占 Cursor / Claude Code 那类按量计费的调用；
  真正需要大模型判断力的活再交给它们。
- **国内通讯软件直接驱动**：内置消息通道，**官方 QQ 机器人**、企业微信、
  Telegram 都能接——手机上发一句话，它在工作区里干完把结果发回来。默认**配对**
  （陌生发送者拿码，你放行才生效），越界改动照样退回确认。
- **和别的 Agent 协同**：两个方向都通。它**能被** Cursor / Codex / Claude Code
  通过 MCP 调用（对方扮演用户下发编排任务，它负责实施与验收）；也**能调用**
  外面现成的 MCP 工具（挂进来长这样 `mcp__服务名__工具名`）。

## 30 秒上手

```powershell
# 装成全局命令（推荐）：装完在任何目录都能敲 spool
uv tool install --editable .
# 想固定一份不改动的：去掉 --editable（升级用 uv tool upgrade spoolkit）

spool init                                # 把当前目录做成工作区（幂等）
spool config --set provider=llamacpp --set base_url=http://127.0.0.1:8080
spool                                     # 什么都不带就进对话；不是工作区会先问一句
```

只想在虚拟环境里跑（开发用）：`uv venv --python 3.12 && uv pip install -e ".[dev]"`，
那样得先激活虚拟环境才有 `spool`。

也可以直接给一件事：

```powershell
spool run --goal "修好 calc.py 里 sum_to 少算一个的问题，不要改测试"
```

改动默认只产出 diff；`--policy auto` 才在授权范围内自动落盘。详细走法见
[`docs/getting-started.md`](docs/getting-started.md)。

## 它能做什么

- **本地小模型驱动**：llama.cpp（含多模态模型的接口位）、Gemini 两个供应商，
  按供应商提供专属模型装载器；上下文窗口与分词都向服务端问，不写死。
- **长任务自主编排**：把大目标拆成带验收标准的步骤（排不完就分批续排），
  逐步执行、逐步验收、可断点续跑（`plan.json` + `progress.md`）。
- **子智能体**：一步可以派给独立上下文的实现者，再由另一个独立上下文审查。
- **代码索引（多语言）**：Python 走 AST，JS/TS/Go/Rust/Java/C/C++/C#/Ruby/
  PHP/Lua/Shell/PowerShell 等走声明扫描；符号表 + 引用图 + 邻域检索，
  用来做"精确取用"；预取按步骤声明的范围锚定。
- **分级授权**：写操作默认只产出 diff，按 `ask / auto / deny` 三档处理；
  越界自动退回确认，白名单外的命令可以申请（无人值守时不会静默放行）。
- **自我验证**：改完自动跑验收并把结果顶回给模型；它报"做完了"不算数，
  验收命令的退出码才算数。
- **能被别的 agent 调用**：挂成 MCP 服务，Codex / Claude Code / Cursor 可以
  扮演用户下发编排任务（[`docs/mcp.md`](docs/mcp.md)）。
- **能被聊天驱动**：手机发一条消息 → 在工作区里跑一轮 → 结果发回来
  （[`docs/bridge.md`](docs/bridge.md)）。

## 常用命令

| 命令 | 做什么 |
|---|---|
| `spool` | 进对话；当前目录不是工作区就先问一句要不要初始化 |
| `spool init [路径]` | 把目录做成工作区（建 `.agent/` 并登记，幂等） |
| `spool approve <码>` | 批准聊天通道的配对码（**任意目录都能敲**） |
| `spool run --goal …` | 跑一次任务 |
| `spool run --autonomous --scope … --goal …` | 自主拆解并逐步做完 |
| `spool run --plan` / `--resume` | 推进已有计划的下一步 / 接着检查点继续 |
| `spool plan --goal …` | 只拆解、落盘计划，不执行 |
| `spool chat` | 对话式使用：多轮、共用同一个会话 |
| `spool serve` | 起 Web UI（在浏览器里看步骤、diff、待确认） |
| `spool bridge --channel fake` | 用聊天消息驱动它（telegram / qqbot / wecom 见文档） |
| `spool mcp` / `spool mcp-servers --check` | 被别的 agent 调用 / 看自己配的外挂 MCP |
| `spool config` `limits` `policy` `session` `revert` | 默认值 / 标定值 / 授权 / 会话 / 回滚 |
| `spool bench --limit N` | 跑回归任务集（可复现的测量） |

逐条参数见 [`docs/cli.md`](docs/cli.md)。

## 配置

| 配什么 | 在哪 | 怎么用 |
|---|---|---|
| 供应商 / 模型 / 服务地址 | 用户级默认 | `spool config --set provider=llamacpp --set base_url=<地址>`；`spool config` 看现值与来源 |
| 同上，临时改一次 | 命令行 | `--provider llamacpp --model <名> --base-url <地址>`（优先于配置文件）|
| 同上，只在这个 shell 生效 | 环境变量 | `SPOOLKIT_PROVIDER` / `SPOOLKIT_BASE_URL` / `SPOOLKIT_MODEL` / `SPOOLKIT_PROXY` |
| Gemini 密钥 | 项目根 `.env` | `GEMINI_API_KEY=...`（本地模型不需要任何密钥）|
| 通道凭据 | 项目根 `.env` | `QQ_AppID` / `QQ_AppSecret` / `SPOOLKIT_TELEGRAM_TOKEN`… |
| 能力标定值（预算、超时、上限） | `.agent/limits.json` | `spool limits --set 名字=值`，`spool limits` 看现值与来源 |
| 授权策略 | `.agent/policy.json` | `spool policy --set auto`（三档：ask/auto/deny）|
| 额外可读目录 | 命令行 | `--allow-read D:\别的地方`（只放开读，写入仍限工作区）|

能力标定值全部可覆盖、可回退：默认值是按本机 27B + 8K 窗口实测出来的，
换模型或换机器就该改，`spool limits` 会告诉你每个值"现在是多少、从哪来"。

> **改名说明**：本项目原先叫 `agents-dev`。旧名字仍然可用——命令 `agents-dev`、
> 环境变量 `AGENTS_DEV_*`、用户配置 `%APPDATA%\agents-dev\config.toml` 都继续认，
> 已经配好的人一个字都不用改。

## 文档

| 文档 | 讲什么 |
|---|---|
| [`docs/roadmap.md`](docs/roadmap.md) | **后续开发计划与交接说明**（给接手的 agent：任务、验收、纪律、踩过的坑） |
| [`docs/getting-started.md`](docs/getting-started.md) | 装、配、第一个任务、常见故障 |
| [`docs/cli.md`](docs/cli.md) | 每条命令与参数 |
| [`docs/manual.md`](docs/manual.md) | 使用手册：工作区／权限／记忆／长任务／通道／排错 |
| [`docs/benchmarking.md`](docs/benchmarking.md) | 怎么量、量到什么 |
| [`docs/dogfooding.md`](docs/dogfooding.md) | 每次改动的原因与结果 |
| [`docs/mcp.md`](docs/mcp.md) · [`docs/bridge.md`](docs/bridge.md) | 两个方向的外部接口 |
| [`docs/superpowers/specs/`](docs/superpowers/specs/) | 设计文档 |

## 已知限制

- **索引的口径要分清**：Python 用 AST，准确；其它语言是**行首声明扫描**
  （认得 `function/class/struct/enum/fn/func/def` 等常见形态，行号是保守估计），
  够回答"这个文件里有什么、大概在第几行"，不够回答"改这里安全吗"。
  **引用图与 `find_callers` 只对 Python 成立**——别的语言上工具会明说
  "没查 ≠ 没有"。认不出的后缀会以通用扫描进索引。
- **多模态未接**：供应商抽象里留了位置，还没有可用的图文输入路径。
- **单机单人**：单 GPU、串行执行，没有并发与多用户设计。
- **模型越弱，越依赖这套脚手架**：能力标定值就是为弱模型准备的；强模型上它们
  是上限而不是保护，该往大调。

## 开发

```powershell
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest -q
```

## License

MIT
