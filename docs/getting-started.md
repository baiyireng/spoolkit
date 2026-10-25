# 上手

安装、第一条任务、常用命令、配置分层 → [README](../README.md)。参数 → [cli.md](cli.md)；机制 → [manual.md](manual.md)。

这一页只留 README 没有的三块。

## 模型服务

`hub` / `web` **不**拉起 llama。模型用 `spool service`，或自己起 `llama-server`。细节 → [cli.md § spool service](cli.md#spool-service)。

```powershell
spool service start llama       # 按用户级配方拉起；list / stop 同名
# 配方在用户级 config.toml，路径按本机 gguf / llama-server 改：
#   [[service]]
#   name = "llama"
#   command = ["D:\\llm\\llama\\cuda\\llama-server.exe", "-m", "D:\\models\\x.gguf",
#              "--host", "127.0.0.1", "--port", "8080", "-c", "8192", "-ngl", "99",
#              "--reasoning", "off"]
#   health = "http://127.0.0.1:8080/props"
```

卸载已装的命令：`uv tool uninstall spoolkit`。

## 工作区解析

顺位：`--root` → 向上找 `.agent/` → 客户端指向 → `workspaces.json`（多条目且不明则报错）→ cwd。

| 入口 | 认 cwd | 无目标时 |
|------|--------|----------|
| CLI（`run` / `chat` / …） | 是 | 退回 cwd，由命令报错 |
| `serve` / `hub` / `web` | 否 | 空态，待点名 |
| `bridge` / `qq` | 否 | 报错要求点名（登记表空 → 启动目录，仅首次配对） |
| `mcp` | 否 | 每次 `delegate_task` 点名 |

`cd proj && spool serve` 改为 `spool serve --root .`。短写：`spool web`、`spool qq`。理由 → [rationale.md](rationale.md#cwd-契约)。

## 故障速查

| 症状 | 处理 |
|------|------|
| 不是工作区 | `spool init` 或 `--root` |
| 假模型 / 只回放 script | `spool config --set provider=…` |
| 模型空内容 | llama-server `--reasoning off` |
| 终端长时间无输出 | 非流式生成；15s 心跳属正常（TTY 原地刷新）。心跳≠有正文；截断重试会重置该次等待计时，并出现「放大预算重试」说明。`spool chat` 进行中会打步骤/工具到 stderr |
| 没有这个配对码 | 看错工作区；报错会写读的文件 |
| 网页无聊天 | 用 `hub --channel` 或 `spool qq`，不是裸 `serve` |
| 输出截断 | `spool limits` / `.agent/limits.json` |
| 一直问确认 | `--policy auto` + `--scope` |
| `No module named 'socksio'` | `uv pip install "httpx[socks]"` 或 `.[socks]` |
| 填了密钥仍报没配 | 机器级读**用户级** `.env`；`spool env` / `spool env import` |
| 键存在值空 | 等于未配；`spool env set …` |
| 壳切不了工作区 | 去掉 `--root`；写不动 → 绑回 `127.0.0.1` |
