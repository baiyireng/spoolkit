# 设计取舍

结论 + 指针。用法见活文档；事故细节 → [dogfooding.md](dogfooding.md)；规格 → `superpowers/specs/`。

主循环图：[agent-loop-flow.md](agent-loop-flow.md)（改 `loop.py` 控制流必同步）。

## 配置三层

- 寿命：CLI = 一次；env = shell；用户配置 = 长期。无用户级 → 人写 `.bat` 掩盖来源。
- 密钥 ∈ `.env`；非密钥 ∈ `config.toml`（白名单键，防「写了不生效」）。

→ dogfooding §2026-09-27 · `specs/2026-09-27-user-env-and-machine-web-design.md`

## cwd 契约

- CLI 认 cwd；机器级入口不认。不明 → 报错点名，不猜。
- 事故：cwd `.env` 盖过用户级凭据 → 无 `--root` 时 `workspace=None`，凭据只走用户级。
- 迁移：`serve`/`hub` 无 `--root` 不再认 cwd。例外：桥登记表空 → 启动目录（仅首次配对）。

→ dogfooding §2026-09-25 · `specs/2026-09-24-workspace-session-kernel-design.md`

## 机器级入口与短命令

- `spool web` / `spool qq` = 短写；`serve`/`hub`/`bridge` 语义不变。
- `[channel.<名>]` 仅短命令读；其中 `root` 认但不读（工作区更早解析完）。

→ dogfooding §2026-09-28 · 同上 specs

## 通道与凭据

- 默认配对，禁止默认放行（公网 + 可驱动 agent）。
- 信任 = 身份级（通道+用户→工作区集合），与当前工作区解耦。
- 凭据解析单实现；插件：`spoolkit.channels` / `SPOOLKIT_CHANNEL_PLUGINS`。
- 不做个人微信/个人 QQ 第三方 hook（条款/封号/绕过配对）。

→ dogfooding §2026-09-13、§2026-09-27

## 长连接与渲染

- QQ：心跳迟发、握手丢帧 → 已修并钉确定性用例。
- 发送侧按通道能力剥标记；不靠「模型别写 Markdown」占上下文。

→ dogfooding §2026-09-13 · `tests/bridge/test_ws.py`

## 异步与协议

- MCP `tools/call` 同步，任务可数十分钟 → `delegate_task` 立即返回 id，进度轮询/`progressToken`。
- 事件环缓冲约 400 条 + seq；选 MCP 而非自定协议（复用接入方适配）。

→ dogfooding §2026-09-16

## 网页壳与切换

- Runner 按 `(工作区, 会话)` 复用 → 切换不杀任务、不丢待确认。
- 换目标：退订旧 `/events` → 订新 → 补 state 帧（按版本号）。
- 仅显式选择写指针；探活在后台，请求路径只读缓存。

→ dogfooding §2026-09-27 Task 3/4

## 权限与只读

- 非回环绑定 → 写端点 403（鉴权未做）。判据 = 实际绑定地址，非 CLI 字符串。
- `/target` 可改进程内视图，不写机器级持久状态。

→ dogfooding §2026-09-28 · specs §3.7

## 长任务的等待

- llama.cpp 非流式 → 15s 心跳区分「仍在生成」与失败。
- 桥超时 = 回执非失败；结束后补发结论。
- 同工作区串行一轮（共享记忆/检查点）。

→ dogfooding §2026-09-22 · `specs/2026-09-21-model-wait-heartbeat-design.md`

## 索引与记忆的口径

- 仅 Python AST + 引用图；其它语言行首扫描，「无调用方」≠「无引用」。
- 冷记忆 FTS5（非向量）；热记忆硬上限；长文不进热。
- `transcript` 默认不进上下文 → `search_history`；默认作用域 = 当前会话。
- 有界摘要可进提示（标定封顶 + `redact_secrets`）。

→ dogfooding §2026-09-12～13

## 测试与工具

- `fake` 非兜底生成器。
- `--check` 必须走真实路径（含 `--bridge-proxy`）。
- 偶发缺陷 → 确定性用例；`verify_file` 按扩展名分派。

→ dogfooding §2026-09-27～28
