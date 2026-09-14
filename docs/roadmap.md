# 后续开发计划与交接说明

> **写给接手的 agent（Cursor / Codex / 任何人）**：先读这一页，再按需展开
> [`manual.md`](manual.md)（它怎么工作）、[`benchmarking.md`](benchmarking.md)
> （量出来的数）、[`dogfooding.md`](dogfooding.md)（每次改动为什么这么改）。
>
> 状态：2026-09-14，HEAD `3b279a7`，全套 **1232 passed**，工作区干净，
> 远端 `git@github.com:baiyireng/spoolkit.git`（public，分支 `main`）。

## 0. 三分钟上手

```powershell
# ① 本机模型服务（**现在是停的**，要用先起它）
Start-Process 'D:\llm\llama\cuda\llama-server.exe' -ArgumentList `
  '-m','D:\llm\llama\models\Qwen3.8-27B-UD-IQ2_S.gguf','--host','127.0.0.1',`
  '--port','8080','-c','8192','-ngl','99','-np','1','--cache-ram','0','--reasoning','off' `
  -WindowStyle Hidden

# ② 改完必须跑测试（当前 1232 条，约 2 分钟）
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp="$env:TEMP\agents-dev-pytest"

# ③ 跑一条任务（联网要显式开 --web）
spool run --goal "把 D:\workSpace\agentWorkSpace 里的 txt 列出来" --web
```

`spool` 是**全局命令**（`uv tool install --editable .` 装的，指向本仓库源码），
改代码立即生效，不用重装。改动了入口点或依赖才需要 `uv tool install --reinstall --editable ".[socks]"`。

## 1. 先理解支点，再改代码

- **一句话**：在 4K–8K 有效上下文下，用 7B–27B 的本地模型干成真实的中小型编程任务。
- **手段**：状态外置（记忆/计划/进度/索引都在 `.agent/`）+ **精确取用**
  （符号索引、引用图、切块、子智能体分开上下文）。
- **所以每个新功能的第一问是**：它会把多少内容灌进上下文？
  实测一页普通文档 ≈ 15 500 token ≈ 我们窗口的两倍——"抓回来直接塞"这种设计
  在这里一律不成立。

## 2. 现在有什么（可核对）

- **工具 15 个**（开 `--web` 后 17 个）：`read_file` `list_dir` `search_code`
  `survey` `file_symbols` `find_symbol` `find_callers` `dir_stats` `calc`
  `check_numbers` `run_command` `request_diagnosis` `read_diagnosis` `dispatch`
  `tool_help`（+ `web_fetch` `web_read`）。
- **记忆三层**：热记忆（`memory.md`，每轮注入、有容量上限）、冷记忆
  （`memory.db`，FTS5 关键词）、独立归纳会话（`memory/distill.py`）；教训带
  触发词与置信度，会主动推送、按效果淘汰。
- **长任务**：拆解带验收标准 → 分批续排 → 逐步验收 → 督导续期 → 断点续跑。
- **四个入口**：CLI（`run`/`chat`）、Web UI（`serve`）、MCP 服务端（`mcp`）、
  聊天桥（`bridge`：fake / telegram / wecom / qqbot）。
- **联网取用地基**（2026-09-13 完成）：`fetch/` 模块 + 两个只读工具 + 缓存 +
  不可信来源标记。
- **基准**：同一批 50 题，自主编排 **50/50、12.7 分钟、263k 输入 token**
  （逐轮记录在 `docs/benchmarking.md`）。环境：RTX 4080 Laptop 12GB、
  Qwen3.8-27B IQ2_S、8K 窗口。

## 3. 工作纪律（这个仓库的规矩，别绕过）

1. **先量再说**。"看起来对"不算结论；阈值、上限、收益都要有实测数字。
2. **不写死**。数量上限、名单、阈值一律由调用方给（`--xxx` 参数或
   `.agent/limits.json`），代码里只留默认值并说明它是按哪台机器标定的。
3. **每个改动都带测试**；提交前跑全套；提交信息写清"为什么"（这个仓库的
   commit message 普遍是长篇解释 + 数据）。
4. **改完在 `docs/dogfooding.md` 记一条**：做了什么、量到什么、哪条被证伪。
5. 注释与文档用中文，标识符用英文。
6. 不确定就如实写"不知道/没量"，**不要编数字**。

## 4. 后续任务（按优先级）

### T1 · 检索量测（下一步，先做这个）

**目标**：量"本地 27B 用 `web_fetch`/`web_read` 自己查资料"的真实水平。
**为什么先做它**：T2 的独立研究会话值不值得做，取决于这组数；不量就做，
等于赌一把（而这个项目的基本纪律是先量）。

**怎么做**
- 准备 5 道题，答案**只存在于某个网页里**（例如某个库的某个 API 在第几个参数上
  有特殊行为），每题给一个入口 URL，不给答案。
- 每题跑一次 `spool run --web --goal "<题面 + URL>" --session webbench`，
  记录：答对没有、调用了几次工具、抓了几页、输入/输出 token、墙钟。
- 脚本可参考 `spool bench` 的做法（`cli/commands/bench.py`），但**先手工跑
  5 道**：量测的目的是看形状，不是建流水线。

**验收**：一张表（题号 / 对错 / 工具调用数 / token / 秒）+ 一句结论：
"这条路能用 / 只能偶尔用 / 不能用"，以及**卡在哪**（找不到块？取错块？综合错？）。

**注意**：模型**不一定主动用工具**（历史教训：注册了不等于会用，
`WORKFLOW_WEB` 那段提示词就是为它加的）。如果它不用，先看提示词，别急着加代码。

### T2 · 独立研究会话（步 3，取决于 T1）

**目标**：主循环调 `research(问题)` → 一个**只读的独立会话**去搜/抓/读/合并 →
只把 ≤800 token 的结论 + 引用交回主循环。

**为什么**：① 主循环上下文不被污染；② 那个会话没有写文件/跑命令的工具，
就算网页里藏着提示注入，它也只能产出一段错话，碰不到工作区。

**怎么做**
- `agents/runtime.py` 已有 `Role` + `restrict(registry, role)` + `run_role`——
  照着 `IMPLEMENTER`/`REVIEWER` 加一个 `RESEARCHER`，工具表**只留**
  `web_fetch`/`web_read`（需要的化加 `calc`），**没有**写文件、`run_command`、`dispatch`。
- `tools/dispatch.py` 是模板：主循环调一个工具、拿回一段压好的文本
  （`_render` + `_flatten(limit)`）。
- **地图-归并**：每页一个干净上下文读要点 → 最后一次只做合并（不要"五页一起塞"，
  小模型最怕这个）。
- 输出契约：`{结论, 引用[url#块号], 不确定的地方, 还需不需要继续}`。
  引用必须能被主循环用 `web_read` 核对（缓存让核对几乎免费）。
- 预算（页数/字节/token）由调用方给，写进 `limits` 或命令行。

**验收**：T1 那 5 道题用研究会话再跑一遍，**主循环 token 明显下降**且答对率不降；
外加一条测试：研究会话的工具表里**没有**写工具（防止以后被改回去）。

**注意**：不要把 `research` 做成"每轮必经的阶段"——它是**一个动作**，
主循环需要时才调（这条原则在本项目的设计讨论里定过）。

### T3 · 搜索（步 4，可选）

**目标**：能"先搜再读"。
**怎么做**：接口留成可插拔（`--web-search-api` 之类），先只接一家（Brave / Tavily /
Bing / Google CSE 任一，要 key）；**不绑死供应商**，没有 key 时工具直接报
"没配搜索，只能直接给 URL"。
**别做**：抓搜索引擎结果页——实测 Bing 结果页 96 KB、去标签后**没有正文**。
**验收**：配一个 key 能搜到结果并抓第一页；没配 key 时报清楚而不是报错崩掉。

### T4 · 网页壳显示待批准的配对请求

**目标**：`spool serve` 的页面里能看到"有人在申请配对"，并给出配对码。
**为什么**：现在只有 `spool chat` 开局与 `spool approve --list` 能看到，
网页壳用户会漏掉。
**怎么做**：`web/runner.py` 的 `snapshot()` 加一个字段（读
`.agent/bridge-pairings.json` 的 pending），`web/page.py` 里像
`showCommandAsk` 那样显示一行。**注意**：这是给人看的，不进模型上下文。
**验收**：造一个 pending 码 → 打开页面能看到码；批准后这一行消失。

### T5 · 多模态（长期，未开工）

**现状**：`llm/` 的供应商抽象里留了图文输入的位置，**没有任何可用路径**。
本机是 llama.cpp，多模态要用 `llama-mtmd-cli` / `--mmproj` 那条路。
**建议**：先量"本地能跑哪个视觉模型、显存够不够"，再决定要不要做。
**注意**：图片进上下文比文字更贵，按本项目的原则，先想清楚"取用"形态。

## 5. 已知问题 / 开放问题（别当它们是 bug 去瞎改）

| 事项 | 状态 |
|---|---|
| **配置向导重复询问供应商** | 一次没复现的现场（用户机器上旧配置存在却弹了向导）。已加"找过哪些路径"的诊断输出；**再出现时先看那行**，别直接改逻辑 |
| **运行时切换绑定工作区** | 故意没做。记忆/索引/检查点/授权都挂工作区，需要先定"切了之后信任边界怎么变" |
| **维基百科打不开** | 三条出口（直连/系统代理/云主机 SOCKS）都不通或 403，是网络出口问题，不是代码 |
| **JS 渲染的页面抽不出正文** | 工具会如实说"抽不出正文，别编"；要支持得引入浏览器内核，代价大，未做 |
| **`serve` 的命令授权** | 已实现并测过（`tests/web/test_command_approval_web.py`），但**没有人在真浏览器里点过** |

## 6. 环境与操作备忘

**网络出口**（三选一，按目标站点试）
```
直连                      → Bing 通 / 维基不通
系统代理 http://127.0.0.1:7890（本项目默认用它）
云主机 SOCKS socks5://127.0.0.1:1080   ← 需要时先 ssh -N -D 1080 ssh.shi123.work
```
QQ 平台有 IP 白名单，所以桥走云主机出口（见下）。

**QQ 桥**（现在是停的；凭据在 `D:\workSpace\agentWorkSpace\.env`）
```powershell
spool bridge --channel qqbot --bridge-proxy socks5://127.0.0.1:1080 `
  --root D:\workSpace\agentWorkSpace --policy ask --scope "**" --timeout 240 --web
# 日志：$env:TEMP\spoolkit-bridge-web.txt（每条事件、每次发送结果都在里面）
```
用户 openid 已在 `agentWorkSpace` 放行；`spool approve --list` 可查。
**改完桥相关代码要重启它**（长驻进程不会自动加载新代码）。

**状态目录**（`.agent/`，不进版本库）
```
memory.db / memory.md / index.db / policy.json / limits.json / bridge-pairings.json  ← 工作区级
sessions/<会话>/plan.json / progress.md / tasks/<id>.json                           ← 会话级
web/<sha1(url)[:16]>/meta.json + 000.txt…                                           ← 网页缓存
```

**干净环境复验**（3.12，装的是构建出来的 wheel）：`%TEMP%\agents-install-a\Scripts\python.exe`

## 7. 踩过的坑（同一类错误别再犯）

| 症状 | 根因 | 教训 |
|---|---|---|
| 手机发消息完全没反应 | 回复打到了 `/v2/channels//messages`（C2C 的 `conversation` 是裸 openid，被当成了 `kind:` 前缀），平台回 11001 | **"平台拒绝"要和"我们发错"分开**；用同一 msg_id 试多种写法，把变量收敛到一个字段 |
| 每 60 秒断一次连接 | 心跳只在两次阻塞读之间检查，而读超时 20s、心跳间隔 41s → 心跳永远迟到 | 长连接的时序要按"截止时刻"算，而不是"两次操作之间" |
| 偶发连不上（单测必过） | 握手响应与第一帧同段到达，多读的字节被丢掉 | 偶发问题固定成确定性用例（让假服务端**故意**同段发送） |
| 批准了但机器人还说不认识 | 长驻进程拿着启动那一刻的内存快照 | 跨进程共享的状态要**按文件 mtime 重读** |
| 答非所问（读到别的任务） | `plan.json`/`progress.md` 是工作区级 | 会话级状态按会话隔离，**换会话=换一套状态** |
| 一次发送失败后彻底不理人 | 异常把长驻进程打死了 | 长驻进程的每次外部调用都要兜异常；失败也要留痕 |
| 模型说"已授权，无需再申请" | 用户的聊天内容被当成了授权 | 权限只能由系统给；提示词里写死这条 |
| 回 `y` 变成了新任务 | 等确认时的 y/n 没被当回答 | 交互式确认要在入口处拦截 |
| 改完名旧配置失效 | 路径/环境变量/密钥都换了位置 | 用户机器上**已有数据一律不动**，旧名字继续认 |
| `__init__.py` 被写空 | agent 跑偏去改源码（工作区绑错了） | 工作区绑定要显式核对；关键模块加"进程内守卫测试" |

## 8. 给 Cursor 的开场提示词（可直接粘贴）

```
仓库：D:\workSpace\agents_dev（public: github.com/baiyireng/spoolkit）

先读这三个文件，再动手：
- docs/roadmap.md   ← 后续计划与交接说明（含工作纪律、环境、踩过的坑）
- docs/manual.md    ← 它怎么工作（记忆/权限/长任务/通道/联网）
- docs/dogfooding.md ← 每次改动为什么这么改（最新几条是这两天的）

然后按 docs/roadmap.md 第 4 节的顺序做，从 T1（检索量测）开始。
每次改动的要求：
1) 先量再说，阈值不写死（由调用方给，`.agent/limits.json` 可覆盖）
2) 改动带测试，提交前跑：
   $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp="$env:TEMP\agents-dev-pytest"
   （当前 1232 passed）
3) 改完在 docs/dogfooding.md 补一条：做了什么、量到什么、哪条被证伪
4) 注释与文档用中文，commit message 写清"为什么"

本机模型服务要自己起（见 roadmap 第 0 节，命令可直接用），
当前它是停的；`spool` 是全局命令，改代码立即生效。
```
