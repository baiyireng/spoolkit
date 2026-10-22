# 用户级配置与机器级入口 —— 设计文档

日期：2026-09-27
状态：待评审
前置：[`2026-09-24-workspace-session-kernel-design.md`](2026-09-24-workspace-session-kernel-design.md)（批次一，已并入 `main`）

---

## 1. 为什么做这一件事

这一批的三个需求，来自今天真实踩到的三件事，不是"想到一个功能"。

**① 机器级服务拿不到凭据。** 在 `C:\Users\32615` 下敲 `spool bridge --channel qqbot …`，
报"缺少 AppID 与 AppSecret"，并建议把密钥写进 `C:\Users\32615\.env`——
而凭据好好地在 `D:\workSpace\agentWorkSpace\.env` 里。原因是
`bridge/credentials.py::find(root, …)` 的 `root` 来自
`cli/commands/bridge.py::_build_channel` 里的 `Path(getattr(args, "root", ".")).resolve()`：
**不给 `--root` 就是 cwd**。一条"服务整台机器"的桥，它的密钥查找根居然是"你碰巧站在哪个目录"。

**② 网页壳必须钉住一个工作区才能起。** `serve` 与 `hub` 都在启动那一刻
`resolve_target(...)` 解出一个工作区 + 会话，塞给**唯一一个** `Runner`
（`web/server.py::serve(project_root, …)`）。于是"在任意目录起 web、进去再选工作区"
做不到——而批次一已经把"在哪个工作区、哪条会话"变成了**内核状态**，
网页壳现在是四个入口里唯一还没接上内核的那个（roadmap §5 也记着"换脸是批次二"）。

**③ 起一条通道要敲六个参数。** `--channel qqbot --bridge-proxy socks5://… --policy ask
--scope "**" --timeout 240 --web`——这几个值没有一个属于"这一次"，它们都是
**这台机器上的默认值**。用户级配置里已经有 `config.toml`（llama / tunnel 的
`[[service]]` 就写在那儿），但通道的参数没有位置可放，于是只能每次重打。

一句话：**工作区级配置齐了，机器级配置缺一半；四个入口里 CLI 与通道接上了内核，
网页壳还没接。**

---

## 2. 现状的破绽（可核对）

| # | 事实 | 位置 |
|---|---|---|
| 1 | 凭据查找 = 环境变量 → `root/.env`，`root` 缺省是 **cwd** | `bridge/credentials.py:23`、`cli/commands/bridge.py:34` |
| 2 | 密钥解析至少三处各自为政：通道凭据 / Tavily / Gemini | `bridge/credentials.py`、`cli/runtime.py:529`、`llm/providers.py:54` |
| 3 | 用户级只有 `config.toml`，且是**非密钥**白名单（`settings.KEYS`），没有放密钥的地方 | `settings.py:42`、`settings.py:126` |
| 4 | 网页壳 = 一个进程一个目标：`serve()` 构造一个 `Runner`，`/state` `/run` `/confirm` 全打在它身上 | `web/server.py:138`、`web/runner.py` |
| 5 | `serve` / `hub` 在启动时解析并钉住 target | `cli/commands/serve.py:58`、`cli/commands/hub.py:95` |
| 6 | 四入口对"cwd 算不算数"口径不一（CLI 认 cwd；web/bridge/MCP 不认；桥还有一条"登记表为空时退回 cwd"） | 批次一遗留，见 deferred 清单 |

第 3 条是根：**密钥只能放工作区**，所以任何"机器级"的服务都得先假装自己有工作区。

---

## 3. 核心决策

### 3.1 配置分三层，密钥只放 `.env`

| 层 | 属于谁 | 放哪 | 内容 |
|---|---|---|---|
| 命令行 / 环境变量 | **这一次** | 参数、本 shell 的 `$env:` | 临时覆盖 |
| 工作区级 | **这个项目** | `<工作区>/.env`、`.agent/*` | 项目专用密钥与策略 |
| 用户级（新增补全） | **这台机器上的你** | `%APPDATA%\spoolkit\.env`（新）、`%APPDATA%\spoolkit\config.toml`（已有） | 机器级密钥、非密钥默认值 |

优先级：**命令行 > 环境变量 > 工作区级 > 用户级 > 内置默认**。
"更具体的赢"——项目里写的那份盖住个人默认那份，本 shell 的环境变量盖住文件。

**密钥只写 `.env`，`config.toml` 只放非密钥默认值。** 这条分工是有意的：
`config.toml` 是 TOML、有键白名单（写错一个字母的症状是"设置了却没生效"），
而密钥要能随手加一行、要能被 `.gitignore` 一句话排除。

用户级 `.env` 的位置：`SPOOLKIT_ENV_FILE` 指到哪就用哪（测试与多套配置用），
否则 Windows 是 `%APPDATA%\spoolkit\.env`，其它平台 `$XDG_CONFIG_HOME/spoolkit/.env`。
**与 `config.toml` 同目录**，`spool env` 会把两个路径都印出来。

### 3.2 cwd 不再参与凭据解析

凭据的"工作区级"那一份，只认**内核解析出来的工作区**（`resolve_target` 的结果），
不再认"你现在站在哪个目录"。四个入口的 cwd 口径也就此固定成契约：

- **CLI 认 cwd**（`spool run` 在某个工作区里敲，就该跑那个工作区）；
- **web / bridge / MCP 不认 cwd**（它们服务整台机器，站在哪不该改变行为），
  初始目标走内核顺位：客户端指针 → 登记表 → 交互式选择。

这条同时收掉第 6 条破绽的**大半**：`serve` / `hub` / `mcp` 与机器级桥的 target 解析都不再认 cwd。

**但有一处没动，得在这里说清楚**（任务 3 交付时如实标明、账本也记着）：`bridge/core.py::target_for`
里"登记表为空时退回启动目录"那条兜底**还在**——它是批次一评审过的三段式（指针 > 登记表 >
（仅当登记表为空）启动目录），而"一个工作区都没登记"的那个人多半正第一次用。
agent 子进程的 `--root` 已不再来自 cwd（密钥那条路封了），剩下的是权限与斜杠命令的判定仍看 cwd。
要不要一并收掉，留给后续定（见账本 deferred 表）。

### 3.3 机器级网页壳：一个进程 = 一个 target **状态** + 一张 Runner 表

`serve()` 从"构造一个 Runner"改成"持有当前 target + 按 (工作区, 会话) 复用的
Runner 注册表"。理由是批次一在 MCP 那边已经这么做过了（`mcp/server.py` 的
`WorkspaceRunner`），网页壳照同一个形状做，两处不再各有一套。

- `GET /state` 扩展：当前 target + 已登记工作区清单 + 当前工作区的会话清单；
- `POST /target`：切工作区 / 切会话（`{"workspace": …, "session": …}`，都可选）；
- SSE `/events`：切换时**不重连客户端**——服务端发现 target 变了就退订旧的、
  订阅新的，并补一帧 `state`（页面不会停在旧工作区的事件流上）。
- `--root` 给了就钉住（老行为、老脚本不变）；不给就是机器级。

### 3.4 切换语义：在跑的任务不杀、不阻塞

切工作区/会话时，如果原会话有任务在跑：**让它跑完**。任务留在它自己的
`(工作区, 会话)` 里，事件与历史都留在那儿，页面上给一条"别处还有一条在跑"的提示，
切回去就能看到。

理由：用户刚在 A 工作区发起的任务，因为"我想看一眼 B 工作区"就被杀掉，是更坏的体验；
而"有任务在跑就不许切"会让机器级网页壳失去意义（那正是它比 `spool run` 多出来的东西）。
代价是同时可能有多个任务在跑——所以页面必须**说得出**这一条，不能假装只有一条。

### 3.5 命令面：机器级入口在任意目录可用，且要短

| 新命令 | 等价于 | 说明 |
|---|---|---|
| `spool web` | `hub`（不带通道） | 机器级网页壳，任意目录；不钉工作区，进去再选 |
| `spool qq` | `bridge --channel qqbot …` | 通道参数与凭据走用户级配置与用户级 `.env` |
| `spool env` | — | 用户级 `.env` 在哪、有哪些键名（**不回显值**）、写一条进去 |

`serve` / `hub` / `bridge` **全部保留、语义不变**（老脚本、文档、`.bat` 不能坏）。
`spool qq` 只是"按这台机器上的默认值起 QQ 通道"的短写；参数仍可覆盖。

### 3.6 用户配置新增 `[channel.<名字>]`

```toml
[channel.qqbot]
proxy   = "socks5://127.0.0.1:1080"
policy  = "ask"
scope   = "**"
timeout = 240
web     = true
```

读取走 `settings.channel_options(name)`，优先级同样是
**命令行 > 环境变量 > 用户配置 > 内置默认**；白名单机制沿用 `KEYS` 的思路——
键名写错不能静默（照 `describe_missing` 的先例，把"找过哪些位置、有什么键"说出来）。

### 3.7 安全边界：仍是 loopback；给了非 loopback 就只读

网页壳默认绑 `127.0.0.1`。显式 `--host` 给了非 loopback 地址时，本批**不实现远程可写**：
`/run` 与 `/confirm` 一律 403，并说明"远程只能看"。远程可写要单独一个开关
（真开就要先想清楚鉴权与传输），不在本批。

---

## 4. 不做的事（non-goals）

- 不做多用户、登录、鉴权，不做远程可写；
- 不引入前端框架或任何新依赖（网页壳仍是 `page.py` 里的一份 HTML + 原生 JS/DOM）；
- 不做 `spool up`（一键：探活服务 + web + 通道）——记进 deferred，等 `web`/`qq` 稳定后再说；
- 不动 agent 的工具表，不改 `--events` 的既有事件语义（只许增加）；
- 不改 `.agent/` 下任何既有文件的格式；旧文件不删、不迁。

---

## 5. 验收

1. 在**任意目录**（例如 `C:\Users\32615`）`spool qq` 能起来并连上网关，凭据取自用户级 `.env`；
2. `spool web` 在任意目录能起，页面顶部可切工作区与会话，切换后事件流跟着换；
3. 一条会话在跑时切走再切回，任务没被杀、历史与事件都还在；
4. `spool serve --root X` / `spool hub --channel qqbot --root X` 的行为与本批之前**逐字一致**；
5. 全套测试无回归（基线 `1542 passed / 3 failed`，那 3 条是本机沙箱限制）；
6. 每个任务在 `docs/dogfooding.md` 留一条，最后一个任务统一回写其余文档。
