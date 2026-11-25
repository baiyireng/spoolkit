# 工具输出 / 上下文 token 审计（B0）

测量日：2026-10-21。基线 `c76a4bb`（`feat/task-flow-contract` 从这里拉出）。未改产品代码。

## 方法

- 最全装配走 `cli/runtime.assemble_loop`，与 `tests/test_prompt_mechanics.py` 的「最全装配工具清单」同一条路径：25 件工具（编排层删除之后）。外挂 MCP 不在这 25 件里。
- token 以本机 llama.cpp `POST /tokenize` 为准。探活 `http://127.0.0.1:8080/health` = 200；`/props` 给出 `n_ctx=8192`，模型 `Qwen3.8-27B-UD-IQ2_S.gguf`。地址来自用户配置 `config.toml`。
- 离线计数（ASCII 4 字 1 token，非 ASCII 1 字 1 token）并列写出。它在中文上偏高，在代码 diff 上偏低，不能单独当 summary 上限。
- 工具抽样：临时项目 + 本仓库只读调用。写工具只打进临时 `PendingChanges`，不落盘。
- 动态三任务在仓库外临时目录用 `spool run --events --provider llamacpp` 跑完。跑前备份、跑后还原 `%APPDATA%\spoolkit\workspaces.json`。产品树没有这些任务的改动。

`Config.context_window` 默认 8192，与这次服务端 `n_ctx` 一致。有效预算 = 窗口 − 15% 输出预留。

| 窗口 | 输出预留 | 有效预算 | system 配额 | hot_memory | task_state | retrieval | code | lessons |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 8192 | 1228 | 6964 | 900 | 1044 | 348 | 1044 | 2437 | 696 |
| 4096 | 614 | 3482 | 696 | 522 | 174 | 522 | 1218 | 348 |

system 区段是必留，配额 900 **不会裁它**。裁剪线是「整段 ≤ 有效预算」。下面的 system 全文（服务端 2432）两边都放得下。

## 1. ToolResult 使用盘点

`ToolResult` 现在只有 `ok / content / facts / usage / raw`。没有 `summary`、`data`、`artifacts`。25 件工具的 handler 全部返回 `ToolResult`。模型看到的是 `content`（`read_file` 另有循环内的呈现规则，见下）。`facts` 进数字核对，不进提示。`usage` 累加进用量。`raw` 留给审查层，不进提示。UI 不读这三个字段。

成功的 `tool` 事件：轨迹只画「名字 → 成功」。`detail` 在事件里先被压成 200 字，而且**成功行不把 detail 放进轨迹**。失败行才带 detail。结算时的 `diff` 事件另走待确认卡片，不进轨迹。

| 工具 | 分组 | content 典型 / 上限 | raw | facts | usage | UI |
|---|---|---|---|---|---|---|
| read_file | 看 | 无硬上限。`loop.py` 整文件 **284141 字 / 7471 行**。80 行片段 2003 字，服务端 **709** token。可检查后缀且超过 `observation_inline_lines`（80）时，循环改给模型一张卡，不改工具返回值 | 否 | 否 | 否 | 成功不进轨迹 |
| list_dir | 看 | 无硬上限。工具目录 237 字 / 23 行 | 否 | 否 | 否 | 同上 |
| search_code | 看 | 默认最多 50 行，无字数上限。50 条命中 790 字；本仓库 `class ToolResult` 1 条 48 字 | 否 | 否 | 否 | 同上 |
| survey | 看 | 默认预算 1200 token，调用可在 200–4000 之间。工具目录抽样 2524 字 / 97 行 | 否 | 否 | 否 | 同上 |
| find_symbol | 查符号，注册在「看」 | 未抽样。符号正文有 `MAX_SYMBOL_LINES=400` | 否 | 否 | 否 | 同上 |
| file_symbols | 看 | 未抽样。无 content 硬上限 | 否 | 否 | 否 | 同上 |
| find_callers | 看 | 未抽样。无 content 硬上限 | 否 | 否 | 否 | 同上 |
| dir_stats | 量 | `src/spoolkit` 1814 字 / 41 行，**102 条 facts**。临时小目录 931 字 / 18 条 facts | 否 | **是** | 否 | facts 不进 UI |
| web_read | 查 | 未抽样。按块读缓存，块大小默认 2400 字 | 否 | 否 | 否 | 同上 |
| research | 查 | 未抽样。短结论 + `usage` | 否 | 否 | **是** | 用量进 tool 事件的 token 字段 |
| write_file | 改 | 话术 + **整份 diff**，无上限。新建 3 行文件的回执 186 字 | 否 | 否 | 否 | 事件 detail 截到 200 字；完整 diff 要等结算 `diff` 事件 |
| replace_lines | 改 | 同上，外加行编辑回执。400 行文件里改 2 行：**5901 字，服务端 4183 token** | 否 | 否 | 否 | 同上 |
| delete_file | 改 | 未抽样。话术 + diff，无上限 | 否 | 否 | 否 | 同上 |
| confirm_rewrite | 改 | 未抽样。话术 + diff，无上限 | 否 | 否 | 否 | 同上 |
| run_command | 跑 | `max_output_chars=4000`。拒绝话术 75–151 字。动态任务里的拒绝 detail 18 字 | 字段会原样带走，普通执行路径不填 | 同左 | 否 | 失败 detail 进轨迹；stdout 不进卡片 |
| verify_file | 核 | 短结论。`big.py` 101 字；动态任务 `hello.py` 103 字。不带 raw | 否 | 否 | 否 | 成功不进轨迹 |
| check_numbers | 核 | 未抽样。核对散文 | 否 | 否 | 否 | 同上 |
| request_diagnosis | 诊 | 未抽样。短登记回执 | 否 | 否 | 否 | 同上 |
| read_diagnosis | 诊 | 未抽样 | 否 | 否 | 否 | 同上 |
| calc | 量 | `2+2 = 4`，7 字 | 否 | 否 | 否 | 同上 |
| recall | 记 | 未抽样。条数默认 `recall_limit=5` | 否 | 否 | 否 | 同上 |
| remember | 记 | 未抽样。短回执 | 否 | 否 | 否 | 同上 |
| search_history | 记 | 动态任务 124 字、143 字（未触 200 字截断）。片段默认 240 字，条数默认 5、最大 20 | 否 | 否 | 否 | 成功不进轨迹 |
| dispatch | 派 | 未抽样。子会话结论 + `usage` | 否 | 否 | **是** | 用量进 tool 事件 |
| tool_help | 其它 | 单件 `read_file` 105 字。`"*"` **6159 字，服务端 3456 token** | 否 | 否 | 否 | 成功不进轨迹 |

自动验证器（`make_verifier`，不是 `verify_file` 这件工具）会把 pytest 原文放进 `raw`，`content` 用 `condense_test_output`（默认约 360 字）。模型看 content，审查层看 raw。这条分裂已经存在，但只覆盖自动验证，不覆盖上表的工具。

`replace_text` 在能力表里有声明，**最全装配不注册它**。工作方式因此走「整份用 write_file，区间用 replace_lines」。

`read_file` 的呈现（只影响进模型的那一份，不改 `ToolResult.content`）：

- 后缀属于 `.py/.html/.htm/.js/.css/.json/.toml`，且这次正文超过 80 行：模型看到观察卡。`loop.py` 整文件对应的卡是 116 字，服务端 **57** token。工具自己仍返回 284141 字。
- 行区间不超过 80 行：模型看到带行号的原文。80 行片段服务端 709 token。
- 其它后缀（例如 `.md`）整份进模型，没有这条卡。

## 2. 上下文 token 拆分

最全 25 件、空历史、首轮装配。服务端 token 是 `/tokenize`。装配器自己的 `total_tokens=3227` 用的是离线计数，不要跟服务端 2432 混读。

| 块 | 字数 | 离线 token | 服务端 token | 占 system 全文 |
|---|---:|---:|---:|---:|
| system 骨架（`SYSTEM` 去掉工具索引和工作方式） | 750 | 520 | **384** | 16% |
| `registry.describe()` 索引 | 1078 | 437 | **400** | 16% |
| `build_workflow()` | 2973 | 2237 | **1646** | **68%** |
| system 全文 | 4804 | 3194 | **2432** | 100% |
| 空任务的 task_state | 25 | 18 | 13 | — |
| 用户目标（这句测量用的话） | 21 | 15 | 10 | — |

25 件工具的说明分层（离线合计；索引那一行是服务端实测）：

| 层 | 在哪 | 规模 |
|---|---|---|
| `brief` | 每轮索引里的半句 | 25 件合计离线 **188** token |
| 索引整段 | 每轮常驻 | 服务端 **400** |
| `description` | 不在每轮提示里，在 `tool_help` | 25 件合计离线 **2771** |
| `explain`（description + 参数） | `tool_help` | 25 件合计离线 4438；一次 `tool_help("*")` 服务端 **3456** |
| `WORKFLOW_*` | 每轮常驻 | 服务端 **1646** |

「工具说明长」的答案：

- 不长在 `brief`，也不长在每轮索引。索引 400 token，是 system 全文的 16%。
- `description` 不在每轮提示里。一次把全部说明摊开（`tool_help("*")`）是 3456 token，比整段 system 还大，但这是按需调用，不是常驻税。
- **常驻的长度在 `build_workflow()`**：1646 token，占 system 的 68%。
- **尖峰在工具 `content` 本身**，而且可以比工作方式更大。400 行文件上用 `replace_lines` 改 2 行，回执里的整份 diff 是服务端 **4183** token（离线只报 1597）。这一个回执就超过常驻 system。

其它预算（默认标定，这次首轮大多是空的）：

| 来源 | 默认上限 | 这次实测 |
|---|---|---|
| 预取符号表 `prefetch_budget` | 400 token | 400 行单文件夹具是空的 |
| 预取正文 `prefetch_content_budget` | 1000 token | 动态任务 1 的小目录：**214 字，服务端 85 token**，里面已经有 README 和 `app.py` 全文 |
| 热记忆 | 有效预算的 15%（8192 下 1044） | 新会话为空，未进装配 |
| 教训 | 10%（696） | 空 |
| 最近对话回灌 | 6 轮 × 每轮 240 字；更早摘要 12 条 / 400 字 | 三任务都是新会话，只有当前这句 |
| 最近工具结果进历史 | 最近 6 条消息；`read_file` 超宽改成卡；其它工具整份 `content` 进 `role=tool` | 见第 4 节 |
| 素材卡（硬重置之后） | 8 条 × 240 字，合计 1200 字 | 这三次没重置 |

### 消耗排行（服务端 token）

按「会不会真的进一次请求」排，不把离线计数当名次。

| 名次 | 来源 | 服务端 token | 何时付 |
|---:|---|---:|---|
| 1 | 编辑回执里的整份 diff（`replace_lines` 在 400 行文件上改 2 行） | 4183 | 每次这种写入 |
| 2 | `tool_help("*")` | 3456 | 模型主动要全部说明时 |
| 3 | `build_workflow()` | 1646 | 每一轮 |
| 4 | `read_file` 的 80 行片段 | 709 | 区间读取；超宽可检查文件改成 57 token 的卡 |
| 5 | `registry.describe()` | 400 | 每一轮 |

system 骨架 384 token 紧挨在索引后面，算常驻第四块，但小于上面这五块。空首轮在工具还没跑的时候，前三名是工作方式 1646、索引 400、骨架 384，合计就是 system 全文 2432，约占 8192 有效预算 6964 的 35%。

离线计数不能用来封 diff：同一份 5901 字的 diff，离线 1597，服务端 4183。中文工作方式则相反（离线 2237，服务端 1646）。

## 3. 事件盘点

协议在 `web/protocol.py`。网页轨迹由 `web/runner.py::trace_line` 从事件推出来，上限 40 行；事件日志上限 400 条。模型不读这些事件。模型读的是装配进 `messages` 的 `content`。

| 事件 | 谁产生 | UI 怎么用 | 模型看不看 | 缺什么 |
|---|---|---|---|---|
| `start` | `cli/commands/run.py` 开工 | 占位卡片 | 不看 | 无 `turn_id` / `seq` / `phase`。字段：`session, goal, policy, window` |
| `step` | `AgentLoop._emit` | 轨迹「第 N 步」 | 不看 | 只有 `n`。没有意图、工具、耗时 |
| `tool` | 循环；另有「自动验证」「最终验证」「诊断登记」 | 轨迹状态行。成功丢 detail；失败或 `no_checker` 才带 detail | 不看事件。看的是历史里的 `content` | `detail` 先压到 200 字。有 `intent`（最多 200 字）时才带。无耗时、无稳定序号、无产物引用 |
| `diff` | `settle_with_events`，每条待确认改动一条 | 待确认卡片。**不进轨迹** | 不看 | 有 `path, text`（完整 diff）。不指向产生它的那次 `tool` |
| `await` | 待确认落盘、命令授权、风险批复 | 轨迹一句 + 确认卡（`diffs` / `awaiting_detail`） | 不看 | 命令授权有 `command, reason, count`。改动授权只有 `count`。无关联序号 |
| `confirm` | 同上，回答之后 | 轨迹「已应用/已拒绝」；清空 `diffs` | 不看 | `applied, count`，自动时有 `auto`，命令时有 `command` |
| `usage` | 运行结束时一条 | 轨迹里的用量行 | 不看 | 只有**全程合计**：`steps, calls, prompt_tokens, completion_tokens`。没有逐步 prompt |
| `final` | 运行结束 | 气泡收结论；带「详情：」的行折进轨迹 | 不看 | `text` 把模型结论和系统报告拼在一起。另有 `ok, concluded, terminated_by, model_final, settled, revert_*` |
| `error` | **没有产生者**。协议和轨迹渲染认识它 | 若出现会画「错误：」 | 不看 | 失败现在走 `final ok=false`、`tool ok=false` 或 `note` |
| `wait` | `--events` 下模型等待心跳 | 轨迹；同一 `phase` 替换末行 | 不看 | `elapsed, limit, phase`。不是任务阶段 |
| `note` | 督导、审查、验证、留档 | 轨迹正文 | 不看 | `text, ok`，常带 `kind`。L3 那条自带自己的 token 计数 |
| `delta` / `final_delta` | 循环流式正文 | 气泡追加 | 不看 | 纯文本增量 |
| `section_start` / `section_done` | 长文路径 | 按节替换气泡 | 不看 | 这三次短任务没有 |

快照里另有 `trace.lines`、`diffs`、`awaiting_detail`。`diffs` 是待确认卡片的数据，不是轨迹行。`trace` 是文本行 `{type, text, ok?, detail?}`，不能按阶段回放。

三次动态任务的事件形状（窗口都是 8192）：

| 任务 | 耗时 | 步 / 调用 | prompt 合计 | completion 合计 | 工具（事件 detail 字数） | 收尾 |
|---|---:|---|---:|---:|---|---|
| 读/答：说明临时目录是做什么的。`--policy deny`，4 步封顶 | 11.4s | 2 / 2 | 4901 | 141 | `search_history` 成功，124 字。没有 `read_file` | `final ok=true`，52 字。`settled=none` |
| 改+验：`hello.py` 的返回值改为 `hi`，再 `verify_file`。`--policy auto --scope hello.py` | 26.9s | 4 / 4 | 10325 | 258 | `search_history` 143；`write_file` 成功 detail **200（被截断）**；`自动验证` detail 空；`verify_file` 103；结算后 `最终验证` | `diff` 是 5 行 unified diff；`confirm applied=true auto=true`；`final` 191 字 |
| 命令授权：`echo hello`。`--ask-on-stdin`，stdin 回答 `n` | 10.2s | 2 / 2 | 4505 | 120 | 先 `await`（`command=echo hello`），再 `confirm applied=false`，然后 `run_command` 失败，detail 18 字「用户拒绝了执行：echo hello」 | `final ok=true`（模型给出了「被拒绝」这句结论） |

任务 1 没调用 `read_file`。同一目录上的预取正文是 214 字 / 服务端 85 token，已经包含 README 和 `app.py`。答案里的「测量夹具 / greet」来自预取，事件流里看不到这段正文。

任务 2 的 L3 `note` 自带 `prompt_tokens=603`、`completion_tokens=230`。用量事件是另一套合计，这次不能从事件证明两者是否相加，报告里不分摊。

逐步 prompt 不在事件里。上表的 prompt 是各次模型调用的合计，不是单次提示的大小。单次常驻底是第 2 节的 system 2432，再加上预取、任务状态和已经回灌的工具 `content`。

## 4. 迁移候选与 B1 字段

按「上下文收益 × UI 收益 ÷ 风险」取 5 个。收益大的排前面。都不在 B0 改代码。

| 顺序 | 工具 | 为什么先动 | B1 之后各字段 |
|---|---|---|---|
| 1 | `replace_lines` 与 `write_file` | 实测最大尖峰：2 行改动回灌 4183 token。UI 其实已经有独立 diff 卡片，模型不该再吃一遍全文 | `summary`：路径、增删行数、尚未落盘，硬上限按**服务端 token 或字数**封，不要用离线计数。`data`：`path, start, end, lines_before, lines_after`。`artifacts`：`{type:diff, path}`。`raw`：unified diff，只给确认卡。`content` 继续做兼容别名，但别名应指向 summary，而不是整份 diff |
| 2 | `read_file` | 工具无上限（`loop.py` 284141 字）。循环只对 7 种后缀、且超过 80 行时改成 57 token 的卡；`.md` 和「80 行以内」仍整份回灌。这是循环特例，不是契约 | `summary`：路径、区间、行数、指纹；超宽就写「全文不进对话」。`data`：`path, start, end, lines, fingerprint, withheld`。`artifacts`：`{type:file, path}`。`raw`：正文或带行号片段，只给 UI。80 行以内的片段可以进 `data`，不要无上限进 summary |
| 3 | `search_code` | 最多 50 行、无字数上限，成功结果不进轨迹。模型需要命中列表，UI 需要可点的路径 | `summary`：命中条数、是否被 `max_results` 截断。`data`：`[{path, line, text}]`。`artifacts`：去重后的路径。`raw`：rg 原文 |
| 4 | `run_command` | 上限 4000 字，整段进模型。授权路径已经有 `await`/`confirm`，但和这次 `tool` 没有序号把它们拴在一起。动态任务看到的是拒绝，不是一大段 stdout | `summary`：退出码 + 第一处错误，硬上限。`data`：`argv, cwd, exit, timed_out`。`artifacts`：`{type:command, argv}`。`raw`：stdout/stderr，不进提示 |
| 5 | `verify_file` | 上下文收益小（这次 101–103 字），但和「改完怎么验」同一条线，结构化之后轨迹才能显示成功结论 | `summary`：保持现在这种一行结论。`data`：`path, suffix, problems, notes`。`raw`：语法路径留空。自动验证器已有的 pytest `raw` 不要并进这件工具的模型可见区 |

`survey` 不进这 5 个。它已经按 token 预算截断（默认 1200），收益小于上面的 diff 回灌。`tool_help("*")` 也不迁成新信封优先项；B1 只要保证整包说明默认不进常驻提示（现在已经是按需）。

未迁移工具继续返回旧 `content`。B1 的适配器把旧结果收成 `summary=content`（再按上限截断）、`raw=""`、`data` 空。不要在 B1 改这 25 件的行为。

## 5. 对 B1 字段够不够

够。不阻塞。

- `summary`：给模型的短文本，必须有硬上限。上限不能只靠离线计数。
- `data`：现在不存在。上表 5 个工具各自的结构不一样，不要合成一个公共 payload。
- `artifacts`：文件 / diff / 命令引用。`diff` 事件今天已经有 `path+text`，缺的是和 `tool` 的关联。
- `raw`：字段已有，默认不进提示。缺口是编辑类和 `read_file` 把原文放在 `content` 里。
- `content`：保留为模型可见文本的兼容别名。
- `usage` / `ok` / `facts`：保持。`facts` 仍只给核对。
- `TaskEvent` 需要的、而现在没有的：`turn_id, seq, phase, duration`，以及 `tool` 与 `diff`/`await` 的引用。`error` 事件没有产生者，不必另造一条平行通道；失败阶段放在 `phase=error` 即可。
- 原始 `thought` 今天就不在事件里，也不要回灌。`intent` 已经在 `tool` 事件上（最多 200 字），可以升为 TaskEvent 的 `intent`。
