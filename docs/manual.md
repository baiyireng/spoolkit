# 使用手册

运行机制契约。参数 → [cli.md](cli.md)；上手 → [getting-started.md](getting-started.md)；取舍 → [rationale.md](rationale.md)。

**模型**：一步 = 一次模型调用选工具 → 执行 → 看结果；状态在 `.agent/`，历史可弃。控制流 → [agent-loop-flow.md](agent-loop-flow.md)。

## 工作区

工作区 = 含 `.agent/` 的目录（**不进 git**）。

| 路径 | 级 | 内容 |
|------|----|------|
| `memory.md` / `memory.db` / `index.db` | 工作区 | 热记忆 / 冷记忆 / 代码索引 |
| `policy.json` · `limits.json` · `read-roots.json` | 工作区 | 策略 / 标定覆盖 / extra 可读根 |
| `sessions/<会话>/plan.json` · `progress.md` · `tasks/*.json` | 会话 | 计划 / 进度 / 检查点 |

计划与进度必须会话级（曾工作区级 → 换会话串计划）。身份由 `.agent/` 决定，不是 cwd。

**客户端指向**（运行时，非启动参数）：`cli` / `chat:<通道>:<用户>` / `mcp` / `web` 各一份。显式 `--root`/`--session` 优先且不写指针。

## 配置

分层表见 [getting-started.md](getting-started.md#配置与密钥)。要点：

- 非密钥：`config.toml`（白名单键）
- 密钥：`.env`；缺凭据报错打印「找过的位置 + 键名」，不打印值
- 空值 `KEY=` = 未配

## cwd 契约

| 入口 | cwd |
|------|-----|
| CLI | 认（向上找 `.agent/`） |
| web / bridge / mcp | 不认（机器级） |

唯一静默变更：`serve`/`hub` 无 `--root` 不再认 cwd。桥登记表空 → 启动目录（仅首次配对）。→ [rationale.md](rationale.md#cwd-契约)

## 任务生命周期

1. 取证（读/索引/记忆）  
2. 拆解（步骤 + 验收）  
3. 执行（工具）  
4. 落地（diff → ask/auto/deny）  
5. 验收（**退出码**；自称不算）  
6. 记忆（事件 / 失败 / 热记忆）  

详图：[agent-loop-flow.md](agent-loop-flow.md)

## 权限

| 档 | 行为 |
|----|------|
| `ask` | 写先进待确认 |
| `auto` | `--scope` 内自动落盘；越界仍确认 |
| `deny` | 丢弃写预览 |

工作区外读：`spool extra add <目录>`（写仍确认）。命令越权 → 申请 `o/s/n/b`；无人值守不静默放行。

## 记忆

| 层 | 介质 | 规则 |
|----|------|------|
| 热 | `memory.md` | 每轮注入；硬容量；长文不进 |
| 冷 | `memory.db` FTS5 | 事件/失败/教训；中文逐字切词 |
| 教训 | 冷 → 热推送 | 场景命中主动注入；按成功率调置信度 |

通道/网页：每条消息一次 `run`；近 `recent_chat_turns`（默认 6）轮摘要进提示。历史原文默认不进上下文 → `search_history`。

## 上下文预算

- 窗口：向供应商查（`--window` 可压）
- 截断：放大 → 切块独立会话 → 最后才缩要求
- 标定：`spool limits`；默认按 27B+8K，换机重标

## 联网

| 工具 | 行为 |
|------|------|
| `web_fetch` | URL → 目录 + 首块；缓存 `.agent/web/<sha1前16>/` |
| `web_read` | 按块取 |

边界：不可信数据；内网硬拦；只读。搜索：`TAVILY_API_KEY`。关：`--no-web`。JS 渲染页无正文时工具明示，禁止编造。

## 长任务

- `--autonomous` 且无 `--policy` → 提权 `auto`（边界仍 `--scope`）
- 失败步骤自动重试 1 次（`--plan`/`--resume`）
- 子智能体：实现 + 审查分上下文
- 督导：预算尽/打转时续期·改道·收手；`--no-supervise` 则到顶即停
- `--resume` 读检查点

## 验证与诊断

- 有 pytest → 跑 pytest；HTML + 有 node → 抽内联 script `node --check`
- `write_file` 大幅删减 → 警告/硬拒，逼局部替换
- 环境问题：agent 只登记 `.agent/diagnosis/`；签名报告来自人（密钥在 `~/.spoolkit/`，工具不可达）

## 聊天通道（摘要）

详情 → [bridge.md](bridge.md)。

- 默认配对；`spool approve <码>`；身份级信任（换工作区不重配）
- `/ws` `/sessions` `/use` `/new` 只改指向，不改授权/extra/revert
- 同工作区串行一轮；超时先回执，结束后补发结论

## 网页壳（摘要）

详情端点 → [cli.md](cli.md#spool-serve)。

- 机器级；Runner 按 `(工作区, 会话)` 复用 → 切换不杀任务
- 探活后台；`/state` 只读缓存（`probed_at`/`age_s`）
- 非回环 host → 写端点 403；`--root` → 钉死，`POST /target` 409
- 停止：`POST /run/stop`（温和 terminate）

## 互通

- 被调：`spool mcp` → [mcp.md](mcp.md)
- 调外挂：用户配置 `[[mcp]]` → 工具名 `mcp__<服务>__<工具>`；只给主循环；连不上跳过并明示

## 观察

| 问题 | 命令 |
|------|------|
| 服务状态 | `spool status` |
| 步进事件 | `spool run --events` |
| 会话 | `spool session` |
| 标定/配置/密钥 | `spool limits` / `config` / `env` |
| 凭据来源 | `spool bridge --channel … --check` |
| 回滚 | `spool revert` |
| 外挂 | `spool mcp-servers --check` |
| 诊断登记 | `spool diagnose` |

## 命名

发行名 **spoolkit**，命令 **spool**。兼容：`agents-dev` 命令、`AGENTS_DEV_*`、旧配置路径。供应商：`llamacpp` / `gemini`；`fake` = 测试回放器。
