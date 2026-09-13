# agents-dev

一个面向**本地小模型**（llama.cpp）的编程特化 Agent。

核心命题：在 4K–8K 的有效上下文预算下，用 7B–27B 级本地模型完成真实的中小型
编程任务。小模型做 Agent 的瓶颈不是"不够聪明"，而是三件事：上下文一塞满质量
就断崖式下跌、多步规划能力弱、以及爱编造。所以整套架构只有一个目标——
**让模型每一步面对的上下文都尽可能小、精确、可承载**。

实现手段是把状态外置（计划、进度、记忆都在文件与 SQLite 里，历史可以随时丢），
再用代码索引保证"精确取用"而不是"整份塞入"。

现状：**可用但仍是实验品**（0.0.1）。实测在同一批 50 道题上：单题逐条 6.5 分钟
50/50，自主编排（自己拆解、自己执行、自己验收）16 分钟 50/50。数字与测量方法
都在 [`docs/`](docs/) 里，不在 README 里复述。

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

## 快速开始

### 1. 装

```powershell
uv venv --python 3.12
uv pip install -e ".[dev]"      # 开发；只要用的话去掉 [dev]
```

装好后有 `agents-dev` 命令（`agents-dev --version` 能验证）。

### 2. 起一个本地模型服务

```powershell
llama-server.exe -m <模型.gguf> --host 127.0.0.1 --port 8080 -c 8192 -ngl 99 --reasoning off
```

两个注意点（都踩过）：带思考的模型要加 `--reasoning off`，否则推理内容会把输出
预算吃光、表现为"模型返回空内容"；窗口 `-c` 要按你的显存给，Agent 会向服务端
询问真实窗口，不写死。

### 3. 跑第一条任务

```powershell
# 先把默认值固定下来（只做一次），以后不用每次打 --provider/--base-url
agents-dev config --set provider=llamacpp --set base_url=http://127.0.0.1:8080
agents-dev config            # 看生效值，以及每一项是从哪来的

# 一题一跑：给目标，它在当前工作区里做完
agents-dev run --goal "修好 calc.py 里 sum_to 少算一个的问题，不要改测试"

# 长任务：自己拆解、逐步做完（--scope 是允许自动落盘的范围，必须由你给）
agents-dev run --autonomous --scope "**" --policy auto --limit 20 `
    --goal "把这个工作区里的题目都做对"
```

改动默认只产出 diff；`--policy auto` 才会在授权范围内自动落盘。`agents-dev revert`
可以回滚上一次写入。

### 4. 网页壳

```powershell
agents-dev serve                 # 供应商/地址取用户级配置，也可以用命令行覆盖
# 打开 http://127.0.0.1:8765/
```

页面里能发目标、看步骤与工具调用、看 diff，并在越界时点"应用/拒绝"；
**上方会回放这个会话之前的往来**（给人看的，不进模型上下文）。

也可以直接在终端里多轮地聊：

```powershell
agents-dev chat                  # 一行一句，共用同一个会话；/history、/exit
```

### 5. 让别的 agent 用它（MCP）

它也能反过来**被**调用：挂成 MCP 服务之后，Codex / Claude Code / Cursor
可以扮演用户下发编排任务，由这个 agent 在工作区里实施，事件与结论按协议交回。

```json
{"command": "agents-dev", "args": ["mcp", "--root", "D:\\你的项目", "--scope", "**"]}
```

工具、信任模型与调用序列见 [`docs/mcp.md`](docs/mcp.md)。

反过来也成立：**它自己能调别的 MCP 服务**（外面现成的工具不用重写一遍）。
在用户配置里加一段即可，加完用 `agents-dev mcp-servers --check` 验一遍：

```toml
[[mcp]]
name = "filesystem"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", 'D:\work']
```

挂进来的工具长这样：`mcp__filesystem__read_text_file`（名字带出处），
在工具表里单独一组「外挂」，只给主循环用。细节见 [`docs/mcp.md`](docs/mcp.md)。

## 配置

| 配什么 | 在哪 | 怎么用 |
|---|---|---|
| 供应商 / 模型 / 服务地址 | 用户级默认 | `agents-dev config --set provider=llamacpp --set base_url=<地址>`；`agents-dev config` 看现值与来源 |
| 同上，临时改一次 | 命令行 | `--provider llamacpp --model <名> --base-url <地址>`（优先于配置文件）|
| 同上，只在这个 shell 生效 | 环境变量 | `AGENTS_DEV_PROVIDER` / `AGENTS_DEV_BASE_URL` / `AGENTS_DEV_MODEL` / `AGENTS_DEV_PROXY` |
| Gemini 密钥 | 项目根 `.env` | `GEMINI_API_KEY=...`（本地模型不需要任何密钥）|
| 能力标定值（预算、超时、上限） | `.agent/limits.json` | `agents-dev limits --set 名字=值`，`agents-dev limits` 看现值与来源 |
| 授权策略 | `.agent/policy.json` | `agents-dev policy --set auto`（三档：ask/auto/deny）|
| 额外可读目录 | 命令行 | `--allow-read D:\别的地方`（只放开读，写入仍限工作区）|

能力标定值全部可覆盖、可回退：默认值是按本机 27B + 8K 窗口实测出来的，
换模型或换机器就该改，`agents-dev limits` 会告诉你每个值"现在是多少、从哪来"。

## 常用命令

| 命令 | 做什么 |
|---|---|
| `agents-dev run --goal …` | 跑一次任务 |
| `agents-dev run --autonomous --scope … --goal …` | 自主拆解并逐步做完 |
| `agents-dev run --plan` | 推进已有计划的下一个待办步骤 |
| `agents-dev run --resume` | 接着上次未完成的检查点继续 |
| `agents-dev plan --goal …` | 只拆解、落盘计划，不执行 |
| `agents-dev serve` | 起 Web UI |
| `agents-dev chat` | 对话式使用：多轮、共用同一个会话 |
| `agents-dev mcp` | 以 MCP 服务运行，供别的 agent 调用（见 docs/mcp.md）|
| `agents-dev mcp-servers --check` | 看/验自己配的外挂 MCP 服务 |
| `agents-dev config` | 查看/设置用户级默认配置（provider、地址、模型…）|
| `agents-dev bench --limit N` | 跑回归任务集（可复现的测量）|
| `agents-dev limits` / `policy` / `session` / `revert` | 标定值 / 授权 / 会话 / 回滚 |

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
uv sync --extra dev
uv run pytest -q
```

设计与测量记录在 [`docs/`](docs/)：`benchmarking.md`（怎么量、量到什么）、
`dogfooding.md`（每次改动的原因与结果）、`superpowers/specs/`（设计文档）。

## License

MIT
