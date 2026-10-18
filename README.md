# spoolkit

面向本地小模型的编程特化 Agent，命令为 `spool`。目标用户：持有 7B–27B 本地模型、
需要在真实小中型编程任务上使用它的人。安装与第一条任务在下面的「最短路径」。
排错、各入口怎么认工作区、模型服务见
[`docs/getting-started.md`](docs/getting-started.md)。

现状 **可用，但仍是实验阶段**（0.0.1）。回归集的数字与测量方法见
[`docs/benchmarking.md`](docs/benchmarking.md)：README 不复述，因为数字随改动变化。

## 最短路径

```powershell
uv tool install --editable .              # 装为全局命令；任意目录可执行 spool
spool init                                # 把当前目录做成工作区（幂等）
spool config --set provider=llamacpp --set base_url=http://127.0.0.1:8080
spool                                     # 不带子命令即进对话；不是工作区先询问初始化

spool run --goal "修好 calc.py 里 sum_to 少算一个的问题，不要改测试"
```

改动默认只产出 diff；`--policy auto` 才在 `--scope` 内自动落盘。要求 Python ≥ 3.12；
开发用 `uv venv --python 3.12 && uv pip install -e ".[dev]"`（需先激活虚拟环境）。

## 能做什么

| 能力 | 说明 |
|---|---|
| 本地小模型驱动 | 真实任务用 llamacpp 或 gemini。上下文窗口与分词均向服务端查询 |
| 长任务自主编排 | 大目标拆成带验收标准的步骤（排不完则分批续排），逐步执行、验收，可断点续跑 |
| 子智能体 | 一步可派给独立上下文的实现者，再由另一个独立上下文审查 |
| 代码索引 | Python 用 AST，其它语言用行首声明扫描；符号表 + 引用图 + 邻域检索 |
| 分级授权 | 写操作默认只产出 diff，按 `ask / auto / deny` 三档处理；越界退回确认，白名单外的命令需申请 |
| 自我验证 | 改完自动跑验收；模型自述不足以判定完成，验收命令的退出码才是判据 |
| 常驻入口 | 网页壳（`spool web` / `hub` / `serve`）、聊天通道（`spool qq` / `bridge`）、MCP（`spool mcp`） |
| 和别的 agent 互通 | 可被 Codex / Claude Code / Cursor 通过 MCP 调用，也可调用外部现成的 MCP 工具 |

## 常用命令

| 命令 | 做什么 |
|---|---|
| `spool` | 进对话；不是工作区先询问是否初始化 |
| `spool init [路径]` | 把目录做成工作区（建 `.agent/` 并登记，幂等） |
| `spool ws` | 工作区管理：`use <名字\|序号>` 切换、`add` 新建、`forget` 移除、`alias` 设短名 |
| `spool run --goal …` | 跑一次任务 |
| `spool run --autonomous --scope … --goal …` | 自主拆解并逐步做完（未指定 `--policy` 时提权为 auto） |
| `spool run --plan` / `--resume` | 推进已有计划的下一步 / 从检查点继续 |
| `spool plan --goal …` | 只拆解并落盘计划，不执行 |
| `spool chat` | 对话式使用：多轮，共用同一个会话 |
| `spool session` | 会话：`list`（默认）/ `new [标题]` / `use` / `rename` / `archive`；`--all` 连同归档的一起列出 |
| `spool web` | 机器级网页壳（= `hub` 不带通道；任意目录可起，进入后选工作区）|
| `spool hub` | 网页壳；加了 `--channel` 才再拉一条聊天桥（任意目录可起）|
| `spool serve` | 只起 Web UI，不拉通道（任意目录可起）|
| `spool qq` | 起 QQ 通道（= `bridge --channel qqbot`；默认值来自用户配置 `[channel.qqbot]`）|
| `spool bridge --channel …` | 只用聊天通道（`fake` / `telegram` / `wecom` / `qqbot`）|
| `spool mcp` / `mcp-servers --check` | 被别的 agent 调用 / 查看自配的外挂 MCP |
| `spool approve <码>` | 批准聊天通道的配对码（**可在任意目录执行**）|
| `spool extra add <目录>` | 关联工作区外的目录（读放开，写要确认）|
| `spool service` | 按本机配方启停 llama / 隧道（配方不进仓库）|
| `spool config` / `limits` / `policy` / `env` | 用户级默认值 / 能力标定值 / 授权策略 / 用户级 `.env` |
| `spool revert` / `diagnose` / `bench` | 回滚上一次写入 / 环境诊断 / 跑回归集 |

逐条参数见 [`docs/cli.md`](docs/cli.md)。"在哪个工作区、哪条会话中运行"属**运行时状态**
（`spool ws use`；聊天里 `/ws use`；网页壳换目标），不是启动参数：`--root` / `--session`
是"这一次覆盖"，`--session` 留空沿用上次那条，**换工作区不需重启，也不需重新配对**。

## 配置

| 配什么 | 在哪 | 怎么用 |
|---|---|---|
| 供应商 / 模型 / 服务地址 | 用户级默认 | `spool config --set provider=llamacpp --set base_url=<地址>`；`spool config` 看现值与来源 |
| 同上，临时改一次 | 命令行 | `--provider llamacpp --model <名> --base-url <地址>` |
| 同上，只在当前 shell 生效 | 环境变量 | `SPOOLKIT_PROVIDER` / `SPOOLKIT_BASE_URL` / `SPOOLKIT_MODEL` / `SPOOLKIT_PROXY` |
| Gemini / Tavily 密钥、通道凭据 | 工作区 `.env` 或用户级 `.env` | `spool env set KEY=VALUE`；`spool bridge --channel <通道> --check` 看是否认到 |
| 能力标定值（预算、超时、上限） | `.agent/limits.json` | `spool limits --set 名字=值`，`spool limits` 看现值与来源 |
| 授权策略 | `.agent/policy.json` | `spool policy --set auto`（三档：ask/auto/deny）|
| 额外关联目录 | 本机指令 | `spool extra add D:\其它目录`；一次性用 `--allow-read` |

三层，具体者优先：**命令行 > 环境变量（`SPOOLKIT_*`）> 用户配置
`%APPDATA%\spoolkit\config.toml` > 内置默认**（内置默认为 `fake` 假模型，不生成代码）。
两条分工不混用——**密钥只放 `.env`**（解析顺序：环境变量 → 工作区 `.env` → 用户级 `.env`，
Windows `%APPDATA%\spoolkit\.env`），**非密钥默认值只放 `config.toml`**。

用户级那份无需记路径：`spool env` 查看、`spool env set KEY=VALUE` 写入、`spool env import --from <工作区的 .env>` 搬移。旧名仍可用：命令 `agents-dev`、环境变量 `AGENTS_DEV_*`、用户配置 `%APPDATA%\agents-dev\config.toml` 仍识别；分层理由见 [`docs/rationale.md`](docs/rationale.md#配置三层)。

## 已知限制

- **索引口径需区分**：Python 用 AST，准确；其它语言用行首声明扫描，仅够回答"这个文件
  里有什么、大概在第几行"，不够回答"改这里安全吗"。**引用图与 `find_callers` 只对 Python
  成立**；其它语言上工具会明说"「没有调用方」等于「没查」，不等于「没有」"。
- **多模态未接**：供应商抽象里留了位置，暂无图文输入路径。
- **本地超长文件仍整份读**：网页有切块通道（`web_fetch` / `web_read`），工作区的大手册仍用 `read_file`。
- **llama.cpp 等待期会报心跳**：生成仍是非流式的；满 15 秒打一行「模型生成中，已等待 <秒>s」
  （CLI stderr / 网页）。总超时公式不变。
- **单机单人**：单 GPU、串行执行，无并发与多用户设计。
- **模型越弱，越依赖这套脚手架**：能力标定值为弱模型准备；强模型上它们是上限
  而非保护，需调大。

## 文档

索引与写法：[`docs/README.md`](docs/README.md)。

| 文档 | 内容 |
|---|---|
| [getting-started](docs/getting-started.md) | 故障表 · 工作区入口 · 模型服务 |
| [cli](docs/cli.md) | 命令参数 |
| [manual](docs/manual.md) | 运行机制契约 |
| [agent-loop-flow](docs/agent-loop-flow.md) | 主循环控制流 |
| [bridge](docs/bridge.md) · [mcp](docs/mcp.md) | 通道 / MCP |
| [rationale](docs/rationale.md) | 设计取舍（结论+指针） |
| [benchmarking](docs/benchmarking.md) · [dogfooding](docs/dogfooding.md) | 基准 · 实测日志 |
| [superpowers/specs](docs/superpowers/specs/) | 历史设计档 |

## 开发与许可

关口只有一条命令，两个平台都一样（`scripts/gate.py` 是唯一的实现；
`scripts\gate.ps1` / `scripts\pytest.ps1` 是 Windows 习惯用的薄壳）：

```bash
uv sync --extra dev          # 装开发依赖（含 mypy，版本在 uv.lock 里锁死）
python scripts/gate.py             # 全套：环境体检 + 测试 + 类型检查
python scripts/gate.py --only doctor   # 只做环境体检（受限环境下先看它）
python scripts/gate.py --only tests    # 只跑测试
python scripts/gate.py --only types    # 只跑类型检查
```

**环境问题与代码回归要分得开**（这是关口最重要的性质）：

- 环境准备只有一处实现：`scripts/dev_env.py`（能力体检 + 环境变量 + 临时目录
  补丁），`pytest.ps1` / `gate.ps1` / 探针都调它，不再各写一份；
- 用例自己声明宿主依赖：`@pytest.mark.needs("process_kill")` → 缺能力就**跳过
  并说明缺什么**（受限沙箱里杀进程被拒就是这种情况）；能力在时照常跑，
  红了就是真回归；
- 兜底清单 `tools/known-env-failures.json`：每条带 `requires` / `evidence` /
  `expires`；**能力回来即失效**、**非受限环境整体失效**、**过期直接让关口红**。

**类型检查带基线棘轮**：存量的 161 条发现记在 `tools/mypy-baseline.json` 里，
关口**只对新增报红**；修掉的条目下次刷新基线时收账。刷新基线
（`--update-baseline`）等于承认这一批债务，必须是一次显式提交——换 mypy
版本同理（基线里记着版本号，对不上会提示）。

为什么是 mypy 而不是 pyright：pyright 在 Node 生态里（PyPI 的包装包也要
Node，除非再拉 40MB 的 nodejs 轮子），而本仓是纯 Python——`uv sync` 一条命令
就能在任何只有 Python 的机器上跑起来。这是"同一份代码在多数开发者手里都能
便利开发"的一部分。

basetemp 放仓库外，避免被当成工作区根。License：MIT。
