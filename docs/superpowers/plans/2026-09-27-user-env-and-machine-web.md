# 用户级配置与机器级入口（批次二）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐机器级配置层（用户级 `.env`），把网页壳接上批次一的内核（一个进程服务任意工作区/会话，页面里切），并把机器级入口做成任意目录下的短命令。

**Architecture:** 三层配置（命令行/环境变量 > 工作区级 > 用户级）由 `settings.py` 统一解析，密钥只走 `.env`；cwd 退出凭据解析。网页壳从"一个进程一个 Runner"改成"一个进程一个 target 状态 + 按 (工作区, 会话) 复用的 Runner 注册表"，形状与 `mcp/server.py` 的 `WorkspaceRunner` 一致。`spool web` / `spool qq` 是既有命令的短写，`serve` / `hub` / `bridge` 语义不变。

**Tech Stack:** Python ≥ 3.12，标准库 + 现有依赖（**零新依赖**）；测试 pytest，命令行入口 `.\.venv\Scripts\python.exe`。

**设计文档：** [`docs/superpowers/specs/2026-09-27-user-env-and-machine-web-design.md`](../specs/2026-09-27-user-env-and-machine-web-design.md)

## Global Constraints

- Python ≥ 3.12；**不加任何新依赖**（网页壳仍是 `page.py` 里的一份 HTML + 原生 JS）。
- 注释与文档用中文，标识符用英文。
- 上限、名单、阈值**不写死**：由调用方给参数，或放 `.agent/limits.json` / 用户配置。
- **每个改动都带测试**。提交前跑全套，**只许用脚本**（沙箱会拦手抄的那套）：
  `.\scripts\pytest.ps1`（worktree 加 `-Repo .worktrees\<名字>`，跑一部分就直接透传路径）。
  基线 **1542 passed / 3 failed**；那 3 条（`tests/cli/test_service_command.py` 两条、
  `tests/tools/test_js_verify.py` 一条）是本机沙箱**禁止起子进程 / 找不到 node** 导致的，
  与代码无关。除此之外任何失败都算回归。
- **只 stage 本次改的文件，禁止 `git add -A`**（工作区里有别人的未跟踪文件）。
- 机器级状态一律走 `workspace.state_dir()`；用户级配置一律走 `settings._default_dir()` 与
  `SPOOLKIT_ENV_FILE` / `SPOOLKIT_CONFIG` 覆盖（测试靠它们隔离，**不许写死 `%APPDATA%`**）。
- **兼容是验收条件**：`serve` / `hub` / `bridge` 的既有参数与输出不变；批次的旧配置、
  旧 `.env`、旧配对文件、旧会话名都不许坏，旧文件不删不迁。
- 密钥**不回显**（只给前几位 + `***`），报错里不许出现密钥值。
- 长驻进程（桥 / web）**不许缓存配置与凭据**：每次用都重读文件（批次一的教训：
  "批准了但机器人还说不认识"就是拿启动那一刻的内存快照）。
- `--events` 既有事件语义（`start/step/tool/diff/await/confirm/usage/final/error/note/wait`）
  只增不改；agent 的工具表不动。
- 每个任务改完在 `docs/dogfooding.md` 补一条（做了什么、量到什么、哪条被证伪），
  由最后一个任务统一回写其余文档。

---

## 文件结构

```
新增
  tests/cli/test_env_command.py          `spool env`
  tests/web/test_runner_registry.py      按 (工作区, 会话) 复用 Runner
  tests/web/test_target_endpoint.py      /target 切换与 /state 清单
  tests/web/test_switch_semantics.py     在跑的任务不被切换打断
  tests/test_secrets.py                  三层解析与优先级

改造
  src/spoolkit/settings.py               用户级 .env 位置、密钥解析、[channel.*] 小节
  src/spoolkit/bridge/credentials.py     改成 settings 的薄转出壳（保老导入路径）
  src/spoolkit/cli/runtime.py            Tavily key 走同一条解析
  src/spoolkit/llm/providers.py          Gemini key 走同一条解析
  src/spoolkit/cli/commands/bridge.py    root 不再取 cwd；参数默认值来自用户配置
  src/spoolkit/cli/commands/serve.py     机器级（不钉 target，交给 web 层）
  src/spoolkit/cli/commands/hub.py       同上；子进程按当前 target 钉住
  src/spoolkit/cli/commands/env.py       新文件：`spool env`
  src/spoolkit/cli/app.py                注册 web / qq / env；别名不留坑
  src/spoolkit/web/server.py             target 状态 + 注册表 + /target + SSE 重订阅
  src/spoolkit/web/runner.py             Runner 支持被注册表复用（不持全局单例）
  src/spoolkit/web/page.py               顶部工作区/会话选择栏 + 切换 + "别处在跑"提示

文档（最后一题统一回写）
  README.md / docs/manual.md / docs/cli.md / docs/bridge.md / docs/getting-started.md
  docs/roadmap.md / docs/dogfooding.md
```

---

### Task 1: 三层配置与统一密钥解析

**Files:**
- Modify: `src/spoolkit/settings.py`（加 `env_path` / `effective_env_path` / `user_env` /
  `resolve_secret` / `describe_secret_search`）
- Modify: `src/spoolkit/bridge/credentials.py`（`find()` 委派给 `settings.resolve_secret`）
- Modify: `src/spoolkit/cli/runtime.py`、`src/spoolkit/llm/providers.py`（接同一条入口）
- Test: `tests/test_secrets.py`、`tests/bridge/test_credentials.py`（补齐）

**要点：**
- 顺序固定为 **环境变量 → 工作区 `.env` → 用户级 `.env`**（"更具体的赢"）；
  命令行参数仍由调用方先给，不进这一层。
- 工作区那一份只认**传进来的工作区**，`root=None` 就跳过——**cwd 不再参与解析**。
- 每次调用重读文件，不缓存。
- `describe_secret_search()` 要能说出"找过哪些位置、结果如何"（照 `describe_missing` 的先例：
  这类"我明明填了"的抱怨，只靠复现是猜不出来的）。
- `SPOOLKIT_ENV_FILE`（旧名 `AGENTS_DEV_ENV_FILE` 也认）指到哪用哪。

**验收：** 用户级 `.env` 里写了 `QQ_AppID`，在任意目录（cwd 不含 `.env`）都能解析到，
且来源说明指向那个文件；工作区 `.env` 同名键**优先于**用户级。

---

### Task 2: `spool env`

**Files:**
- Create: `src/spoolkit/cli/commands/env.py`
- Modify: `src/spoolkit/cli/app.py`
- Test: `tests/cli/test_env_command.py`

**要点：**
- `spool env`（无参数）= 印用户级 `.env` 路径（存在与否）+ **键名列表，不回显值** + `config.toml` 路径；
- `spool env set KEY=VALUE`：写进用户级 `.env`（同键替换，保留其余行与注释）；
- `spool env import --from <某工作区的 .env> [--only KEY]`：把工作区里已有的那份搬（复制）到用户级，
  用来把"项目专用"的凭据升级成"机器级"；**不删原文件**，来源写在输出里；
- 任何路径都要能覆盖（`SPOOLKIT_ENV_FILE`），测试不许碰真实 `%APPDATA%`。

**验收：** 在 `C:\Users\32615` 执行 `spool env import --from D:\workSpace\agentWorkSpace\.env --only QQ_AppID --only QQ_AppSecret`
之后，`spool env` 能看到两个键名，`spool qq` 不再报缺凭据。

---

### Task 3: 机器级网页壳（后端）

**Files:**
- Modify: `src/spoolkit/web/server.py`、`src/spoolkit/web/runner.py`
- Modify: `src/spoolkit/cli/commands/serve.py`、`src/spoolkit/cli/commands/hub.py`
- Modify: `src/spoolkit/cli/commands/bridge.py`（机器级桥**每轮** target 传 `use_cwd=False`）、
  `src/spoolkit/cli/app.py`（`optional_root` 入口的向导不许把模型密钥写进 cwd）
- Test: `tests/web/test_runner_registry.py`、`tests/web/test_target_endpoint.py`

**要点：**
- 新增按 `(工作区, 会话)` 复用的注册表（形状照 `mcp/server.py` 的 `WorkspaceRunner`），
  不再有"唯一一个 Runner"的假设；
- 服务持有**当前 target**（可被 `POST /target` 改），`GET /state` 带上：
  当前 target、已登记工作区清单（名字/路径）、当前工作区的会话清单；
- SSE：target 变了就退订旧的、订阅新的，并补一帧 `state`（页面不会停在旧工作区的事件流上）；
- `--root` 给了 = 钉住（老行为不变，`/target` 拒绝切换并说明原因）；不给 = 机器级，
  初始目标走内核顺位（web 指针 → 登记表 → 选择），**不认 cwd**；
- 同一 `(工作区, 会话)` 只允许一个在跑的任务（现有 409 语义保留）；不同 target 可同时跑；
- **收掉 Task 1 二轮评审留下的两处 cwd 残留**（都属"机器级入口不认 cwd"这条契约）：
  ① `hub_command` 不给 `--root` 时 `resolve_target(client="web")` 仍 `use_cwd=True`，把 cwd
  派生的工作区**钉**给桥子进程，于是 `cwd/.env` 仍可盖住用户级（评审实测：同一台机器上
  hub 起的通道拿到 `D:\workSpace\agents_dev\.env` 里的凭据，而裸 `spool bridge` 说缺凭据）；
  ② 机器级桥**每轮**解析 target 时也没传 `use_cwd=False`，而那个 root 会成为 agent 子进程
  的工作区（模型 key 按它读 `.env`）。两处都改成"机器级不认 cwd"，与 `mcp/server.py` 对齐。

**验收：** `spool web` 在任意目录起得来；`curl` 打 `/state` 能看到工作区清单；
`POST /target` 换工作区后 `/state` 的口径跟着换。

---

### Task 4: 网页壳的选择栏（前端）

**Files:**
- Modify: `src/spoolkit/web/page.py`
- Test: `tests/web/test_page_targets.py`

**要点：**
- 顶部一条：工作区 `<select>` + 会话 `<select>`（原生控件，别引框架）；切换即 `POST /target`，
  成功后重绑事件流并清空对话区，加载新 target 的历史；
- 显示当前 target 全路径（网页壳服务整台机器时，“我在哪个工作区”必须一眼看得出，
  这是批次一踩过的坑：`__init__.py` 被写空就是绑错了工作区）；
- 页面要能说出**别处还有一条在跑**（详见 Task 5）。

**验收：** 页面切工作区后，`/state` 与页面标题都指向新工作区；刷新页面仍停在切换后的 target。

---

### Task 5: 切换与并发语义

**Files:**
- Modify: `src/spoolkit/web/server.py`、`src/spoolkit/web/runner.py`、`src/spoolkit/web/page.py`
- Test: `tests/web/test_switch_semantics.py`

**要点：**
- 切换**不杀任务、不阻塞**：任务留在原 `(工作区, 会话)`，事件与历史都留在那儿；
- `GET /state` 报告"正在跑的 target 集合"，页面显示"别处有 task 在跑（工作区/会话）"；
- 注册表要有上限与回收（由调用方给，默认值要标定依据），不能因为切来切去攒下一堆 Runner；
- 退出时把**自己拉起的**子进程收干净（照 `hub.stop_children` 的做法）。

**验收：** A 里起一条长任务 → 切到 B → A 的任务仍在跑（事件流里有后续事件）→ 切回 A 看得到全过程。

---

### Task 6: 短命令 `spool web` / `spool qq` 与 `[channel.*]`

**Files:**
- Modify: `src/spoolkit/settings.py`（`channel_options(name)`）、`src/spoolkit/cli/app.py`
- Modify: `src/spoolkit/cli/commands/bridge.py`（默认值来自用户配置）
- Test: `tests/test_channel_options.py`、`tests/cli/test_shortcuts.py`

**要点：**
- `[channel.<名字>]` 小节（`proxy` / `policy` / `scope` / `timeout` / `web` / `root`），
  优先级 **命令行 > 环境变量 > 用户配置 > 内置默认**；键名写错要能说出口；
- `spool web` = 机器级网页壳（= `hub` 不带通道）；`spool qq` = 按用户配置起 QQ 通道；
  两者在任意目录可用，`--root` 仍可显式钉住；
- `serve` / `hub` / `bridge` 全部保留；`spool qq` 的任何参数都能覆盖用户配置。

**验收：** 在 `C:\Users\32615` 只敲 `spool qq` 就能起桥并连上网关（参数全来自用户级配置与 `.env`）。

---

### Task 7: 非 loopback 只读 + 文档回写

**Files:**
- Modify: `src/spoolkit/web/server.py`、`src/spoolkit/cli/commands/serve.py`
- Modify: `README.md`、`docs/manual.md`、`docs/cli.md`、`docs/bridge.md`、
  `docs/getting-started.md`、`docs/roadmap.md`、`docs/dogfooding.md`
- Test: `tests/web/test_remote_readonly.py`

**要点：**
- 绑非 loopback 时 `/run` 与 `/confirm` 一律 403，并在页面与终端说明"远程只能看"；
  绑 loopback（默认）不受影响；
- 文档统一回写：三层配置、cwd 口径、`spool web`/`spool qq`/`spool env`、
  网页壳切换与"多条在跑"、用户级 `.env` 的位置与写法；
- `docs/roadmap.md` 的状态段（分支、HEAD、测试基线）在这一题里更新。

**验收：** `--host 0.0.0.0` 时 `/run` 返回 403 且有说明；默认 `127.0.0.1` 一切照旧。

---

## 任务顺序与依赖

```
Task 1 ──┬─→ Task 2 ─┐
         │           ├─→ Task 6 ─→ Task 7
Task 3 ──┴─→ Task 4 ─┴─→ Task 5
```

Task 1/2（配置层）与 Task 3/4（网页壳）互不依赖，可并行；
Task 5 依赖 3 与 4；Task 6 依赖 1 与 2；Task 7 收尾。

## Self-Review 提醒（每个任务提交前）

1. 有没有把"这次"的意图写进代码（阈值、路径、命名）？——该由调用方给的要留参数。
2. 新加的每条路径**带测试**了吗？失败路径（找不到、读不出、权限）也测了吗？
3. 密钥有没有可能进日志/异常/页面？来源说明能不能说出"找过哪些位置"？
4. 长驻进程会不会拿着旧快照？（配置、凭据、放行都必须重读）
5. 旧命令、旧参数、旧文件格式还通吗？
