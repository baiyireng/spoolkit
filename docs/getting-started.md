# 上手

首跑路径与故障表。参数 → [cli.md](cli.md)；机制 → [manual.md](manual.md)。

## 最短路径

```powershell
uv tool install --editable .    # PATH: ~/.local/bin；改源码即生效。另：uv venv + pip install -e .
spool config --set provider=llamacpp --set base_url=http://127.0.0.1:8080
# 本机配方（用户级 config.toml；路径按你的 gguf / llama-server 改）：
#   [[service]]
#   name = "llama"
#   command = ["D:\\llm\\llama\\cuda\\llama-server.exe", "-m", "D:\\models\\x.gguf",
#              "--host", "127.0.0.1", "--port", "8080", "-c", "8192", "-ngl", "99",
#              "--reasoning", "off"]
#   health = "http://127.0.0.1:8080/props"
spool service start llama       # 按配方拉起；list / stop 同名
# 也可手起：llama-server.exe -m <模型.gguf> --host 127.0.0.1 --port 8080 …
cd D:\我的项目
spool init                      # 建 .agent/
spool run --goal "修好 calc.py 里 sum_to 少算一个的问题，不要改测试"
```

- 卸载：`uv tool uninstall spoolkit`
- 无子命令的 `spool` = 对话；非工作区会问是否 `init`
- 内置默认 `provider=fake`（只回放 `--script`，**不能写代码**）→ 必须先配真实供应商
- `hub`/`web` **不**启 llama；模型用 `spool service`（或手起）。细节 → [cli.md § spool service](cli.md#spool-service)

## 配置与密钥

| 层 | 内容 | 位置 |
|----|------|------|
| CLI / env | 本次 | `--provider` / `SPOOLKIT_*`（兼容 `AGENTS_DEV_*`） |
| 工作区 | 项目策略/标定/密钥 | `.agent/*`、`<root>/.env` |
| 用户级 | 本机默认 + 机器级凭据 | `%APPDATA%\spoolkit\config.toml` + `.env` |

- **非密钥**：CLI > env > 用户 `config.toml` > 内置
- **密钥**：env → 工作区 `.env` → 用户级 `.env`（更具体优先）。机器级入口无工作区层 → 只读用户级
- 密钥只写 `.env`；非密钥默认只写 `config.toml`

```powershell
spool env                         # 路径 + 键名（值遮罩）
spool env set GEMINI_API_KEY=...
spool env import --from .env --only GEMINI_API_KEY
```

## 工作区解析

顺位：`--root` → 向上找 `.agent/` → 客户端指向 → `workspaces.json`（多条目且不明则报错）→ cwd。

| 入口 | 认 cwd | 无目标时 |
|------|--------|----------|
| CLI（`run`/`chat`/…） | 是 | 退回 cwd，由命令报错 |
| `serve`/`hub`/`web` | 否 | 空态，待点名 |
| `bridge`/`qq` | 否 | 报错要求点名（登记表空 → 启动目录，仅首次配对） |
| `mcp` | 否 | 每次 `delegate_task` 点名 |

迁移：`cd proj && spool serve` 须改为 `spool serve --root .`。短写：`spool web`、`spool qq`。理由 → [rationale.md](rationale.md#cwd-契约)。

## 常用命令

```powershell
spool run --goal "…"
spool run --autonomous --scope "**" --limit 20 --goal "…"   # 未写 --policy 时提权 auto
spool run --resume
spool web                    # http://127.0.0.1:8765/
spool hub --channel qqbot
```

- 默认写操作进待确认；`--policy auto` + `--scope` 才自动落盘；越界仍确认
- `--root` 钉死工作区 → `POST /target` = 409；非回环 `--host` → 写端点 403

## 故障速查

| 症状 | 处理 |
|------|------|
| 不是工作区 | `spool init` 或 `--root` |
| 假模型 / 只回放 script | `spool config --set provider=…` |
| 模型空内容 | llama-server `--reasoning off` |
| 终端长时间无输出 | 非流式生成；15s 心跳属正常 |
| 没有这个配对码 | 看错工作区；报错会写读的文件 |
| 网页无聊天 | 用 `hub --channel` 或 `spool qq`，不是裸 `serve` |
| 输出截断 | `spool limits` / `.agent/limits.json` |
| 一直问确认 | `--policy auto` + `--scope` |
| `No module named 'socksio'` | `uv pip install "httpx[socks]"` 或 `.[socks]` |
| 填了密钥仍报没配 | 机器级读**用户级** `.env`；`spool env` / `spool env import` |
| 键存在值空 | 等于未配；`spool env set …` |
| 壳切不了工作区 | 去掉 `--root`；写不动 → 绑回 `127.0.0.1` |

## 索引

[cli.md](cli.md) · [manual.md](manual.md) · [agent-loop-flow.md](agent-loop-flow.md) · [mcp.md](mcp.md) · [bridge.md](bridge.md) · [rationale.md](rationale.md)
