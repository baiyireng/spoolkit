# 自动代码审查层设计（Automatic Review Layer）

- 日期：2026-10-03
- 状态：**设计稿，未实现**。本文只做设计，不改任何代码。
- 出发点（用户原话）：实际开发中用户基本不会仔细审阅 AI 的代码，尤其高速迭代期；我们已经有「自动验证」，**应该围绕这一思路完善 agent 的自动化代码审查机制，而不是加强用户层面的审阅**。
- 阅读约定：每条现状论断都带 `文件:行`。标 **[已实现]** 的是仓库里今天就有的东西；标 **[缺口]** 的是今天不成立的；标 **[设计]** 的是本文新增、尚未写进代码的东西。所有行号对应**当前工作树**（`git status` 显示 `src/spoolkit/agent/loop.py`、`src/spoolkit/tools/edit.py`、`src/spoolkit/cli/approval.py`、`src/spoolkit/tools/view.py`、`src/spoolkit/tools/trial.py`、`src/spoolkit/context/templates.py` 等有未提交改动），不对应任何 commit。

---

## 0. 结论

现状是「**验证很勤，判决很软**」：自动验证每次改完都跑（`src/spoolkit/agent/loop.py:1548`），收尾也如实报最终验证结果（`src/spoolkit/agent/loop.py:1756`），但**验证结果对「能不能宣称完成」「能不能落盘」没有任何约束力**——模型说 done 就直接收尾（`src/spoolkit/agent/loop.py:1637`），落盘只按 policy/scope 判（`src/spoolkit/cli/settle.py:64`）。于是出现「验证报未通过、改动照样自动落盘、结论照样自称完成」这一整类事实。

本设计的做法：**把已经机械化的东西扩到「改动一致性」这一层（L1），给验证结果接上阻断语义，再用一个独立上下文、结构化输出、且每条断言都过机械核对的审查者补语义层（L3），最后用证据账本（L4）让「完成」这句话必须挂证据。**机械优先，模型其次；能判定就不问模型。反过来，影响面大的改动不靠「用户会自己看」兜底，而是按风险分级**拦下具体动作**并请示一次批复（§3.2）：默认零打扰，例外可审计、可回退。

**本设计的落盘口径（一句话）**：**拦的是危险动作，不是成果。**模型产出的**文件编辑（含大规模改动）持续落盘到工作区、看得见**（§3.2.0、§2.5）；只有**执行命令、删除、迁移、联网、越界写**这类动作在**执行前**被拦下请示（§3.2.2）；请示期间其余成果**已经落盘**，「待批」指的是**某一个被拦的动作**在等批复，不是把改动扣在内存里（§3.2.3）。这条口径是为修掉本文件上一版的一处设计而定的，被否掉的旧写法与其原因见 §1.7 G9。

---

## 1. 取证：现有家底

### 1.1 自动验证（最厚的一块，可直接复用）

| 事实 | 位置 |
| --- | --- |
| 验证器装配：有 pytest 就跑 pytest，没有就按产出类型验 | `src/spoolkit/tools/verify.py:201`（`make_verifier`） |
| 测试命令 `python -m pytest -q`，探测标记 `pyproject.toml`/`pytest.ini`/`tox.ini`/`setup.cfg`/`tests/` | `src/spoolkit/tools/verify.py:28`、`src/spoolkit/tools/verify.py:34`、`src/spoolkit/tools/verify.py:185` |
| 自动验证入口：改动后立刻跑，结论紧跟改动注入历史（不塞进 system 区段） | `src/spoolkit/agent/loop.py:1548`、`src/spoolkit/agent/loop.py:1570` |
| 验证单跑一次并缓存「路径=内容指纹」键 | `src/spoolkit/agent/loop.py:2179`、`src/spoolkit/agent/loop.py:1699` |
| 收尾对最终文件再验一次，报落成改动数 + 行数 + 指纹 + 最终验证 | `src/spoolkit/agent/loop.py:1756`、`src/spoolkit/agent/loop.py:1728` |
| **按原测试判定**：改动里的测试文件在试跑副本里被还原成改动前内容 | `src/spoolkit/tools/verify.py:270`、`src/spoolkit/tools/exec.py:544` |
| 删除的测试文件在试跑副本里保留 | `src/spoolkit/tools/exec.py:560` |
| 跑测试走试跑副本，真实工作区不动 | `src/spoolkit/tools/exec.py:535` |
| 按产出类型分派：`.html/.htm/.js/.css/.json/.toml/.py` | `src/spoolkit/tools/verify.py:1012`、`src/spoolkit/tools/verify.py:431` |
| HTML 静态检查四件套：标签配对 / id 引用 / 本地资源存在 / 内联 CSS 配对（+内联 JS 走 node --check） | `src/spoolkit/tools/verify.py:670`、`src/spoolkit/tools/verify.py:693`、`src/spoolkit/tools/verify.py:745`、`src/spoolkit/tools/verify.py:784` |
| Python 只做语法编译，另有结构约束检查（函数行数/参数/嵌套） | `src/spoolkit/tools/verify.py:1108`、`src/spoolkit/check/constraints.py:104` |
| 「查不了」显式说出来，绝不沉默通过；无验证器给替代做法 | `src/spoolkit/tools/verify.py:426`、`src/spoolkit/tools/verify.py:1091`、`src/spoolkit/tools/verify.py:443` |
| 检查结果三分类（problems / cannot / uncovered / notes） | `src/spoolkit/tools/verify.py:997` |
| 验证范围收敛：一次最多逐处验 5 个范围、最多检 5 个文件 | `src/spoolkit/tools/verify.py:38`、`src/spoolkit/tools/verify.py:436` |
| 环境问题单独标记，改由 `request_diagnosis` 处理 | `src/spoolkit/tools/verify.py:32`、`src/spoolkit/tools/verify.py:315` |
| 合流规则「只加不减」：pytest 的措辞与判定原样保留 | `src/spoolkit/tools/verify.py:362` |

### 1.2 护栏与台账（可直接复用）

| 事实 | 位置 |
| --- | --- |
| 全部写操作先进 `PendingChanges`，确认后才落盘 | `src/spoolkit/tools/edit.py:1225` |
| 消失清单：旧文本里有、新文本里没有的符号/id/事件/CSS 选择器 | `src/spoolkit/tools/edit.py:375`、`src/spoolkit/tools/edit.py:127`、`src/spoolkit/tools/edit.py:136`、`src/spoolkit/tools/edit.py:146` |
| 声明式重写：`rewrite_reason` + 逐项 `acked_disappeared`，未声明则硬拦 | `src/spoolkit/tools/edit.py:46`、`src/spoolkit/tools/edit.py:47`、`src/spoolkit/tools/edit.py:863` |
| 留痕随改动走到用户确认那一刻（`declared`） | `src/spoolkit/tools/edit.py:1138`、`src/spoolkit/cli/approval.py:75` |
| 改动前快照 Baseline + `spool revert` | `src/spoolkit/tools/edit.py:1520`、`src/spoolkit/tools/edit.py:1585`、`src/spoolkit/cli/commands/revert.py:10` |
| 删除文件有真工具（`delete_file`，且与写工具并列，触发验证） | `src/spoolkit/tools/edit.py:97`、`src/spoolkit/tools/edit.py:1866`、`src/spoolkit/agent/loop.py:65` |
| **全仓残留引用检测**（改名后旧名还在别处）：`vanished_def_names` → `lingering_references` → `lingering_note` | `src/spoolkit/tools/edit.py:209`、`src/spoolkit/tools/edit.py:238`、`src/spoolkit/tools/edit.py:303`、`src/spoolkit/tools/edit.py:188` |
| 残留报告接到工具输出与事件里（放在 detail 最前，避免被截断） | `src/spoolkit/agent/loop.py:2067`、`src/spoolkit/agent/loop.py:1472`、`src/spoolkit/agent/loop.py:1480` |
| 扫描有界：单文件 200k / 总量 400k 字符 / 最多 8 文件 / 每文件 4 行 | `src/spoolkit/tools/edit.py:191`、`src/spoolkit/tools/edit.py:192`、`src/spoolkit/tools/edit.py:193`、`src/spoolkit/tools/edit.py:194` |
| 提示词里写死「清单没清空前不得说『整个项目里都统一了』」 | `src/spoolkit/context/templates.py:258` |
| 测试文件判定：`test_*.py` / `*_test.py` / `test`/`tests` 目录 | `src/spoolkit/tools/edit.py:155`、`src/spoolkit/tools/edit.py:110` |

### 1.3 事实核对与独立审查（L3 要复用的正是这套）

| 事实 | 位置 |
| --- | --- |
| `RunFacts`：工具名来自注册表、改动与路径来自循环自己的账，**不用 trace 里的字** | `src/spoolkit/agents/supervisor.py:225`、`src/spoolkit/agent/loop.py:1829` |
| `fact_conflicts`：注入前逐句核对，与记录冲突就改写成记录里的事实 | `src/spoolkit/agents/supervisor.py:260`、`src/spoolkit/agents/supervisor.py:307` |
| 目前只认两种断言形状：「工具不存在」「0 处改动」 | `src/spoolkit/agents/supervisor.py:197`、`src/spoolkit/agents/supervisor.py:208` |
| 冲突只记事件、不改行动（action/steps 不动） | `src/spoolkit/agents/supervisor.py:313`、`src/spoolkit/agent/loop.py:1914` |
| 督导证据包里**刻意不含对话历史**（避免附和推理） | `src/spoolkit/agent/loop.py:1867`、`src/spoolkit/agents/supervisor.py:74` |
| 已经有只读的 `reviewer` 角色（工具集里没有写权限） | `src/spoolkit/agents/runtime.py:80`、`src/spoolkit/agents/runtime.py:101`、`src/spoolkit/agents/runtime.py:112` |
| 审查结论必须结构化：`verdict`/`reasons`/`fix_goal` | `src/spoolkit/agents/dispatcher.py:46`、`src/spoolkit/agents/dispatcher.py:70` |
| 解析不出来按未通过；「没结论」与「判不合格」分开 | `src/spoolkit/agents/dispatcher.py:96`、`src/spoolkit/agents/dispatcher.py:57`、`src/spoolkit/agents/dispatcher.py:67` |
| 审查者的证据包 = 改动差异 + 自动验证结果（不给实现者推理） | `src/spoolkit/agents/dispatcher.py:148`、`src/spoolkit/agents/runtime.py:178` |
| 判定模板：「审查本身含糊、没有给出依据时，按未通过处理」 | `src/spoolkit/context/templates.py:723` |
| 派发前的硬闸门：没有验收标准就不许派发 | `src/spoolkit/agents/runtime.py:156` |

### 1.4 越界与授权（L1 要复用的）

| 事实 | 位置 |
| --- | --- |
| `path_in_scope` / `out_of_scope`（目录前缀或通配），空 scope = 不许自动改动 | `src/spoolkit/agents/plan.py:565`、`src/spoolkit/agents/plan.py:580` |
| 只能收窄不能扩权（模型不能自己给自己发许可证） | `src/spoolkit/agents/plan.py:593` |
| auto 落盘只在 scope 内，越界退回确认；无人值守只丢越界那几处 | `src/spoolkit/cli/settle.py:64`、`src/spoolkit/cli/settle.py:74`、`src/spoolkit/cli/settle.py:80` |
| 事件路径与终端路径判定一致 | `src/spoolkit/cli/events.py:78` |
| 策略三态 ask/auto/deny | `src/spoolkit/policy.py:18` |

### 1.5 验收台（探针集）

| 事实 | 位置 |
| --- | --- |
| `python scripts/probe.py --all`，5 个真实任务形状，各带可证伪判据 | `scripts/probe.py:14`、`scripts/probe.py:49`、`benchmarks/probes/probes.json:20` |
| 判据必须能证伪（未改种子要红、人工修好要绿），由测试钉住 | `benchmarks/probes/checks/_common.py:6`、`benchmarks/probes/checks/_common.py:119` |
| 每条记录的 **19 个必填字段** | `scripts/probe.py:60`（19 项，逐条列出） |
| 记录组装点（新字段加在这里） | `scripts/probe.py:710` |
| 下限校验（`max_repeat_blocks` 等映射到裸计数） | `scripts/probe.py:788`、`scripts/probe.py:795` |
| 人读一行 notes | `scripts/probe.py:829` |
| 现有 17 条记录文件 / 17 行 `results.jsonl`；`probes.json` 里 5 个探针 | `benchmarks/probes/records/`、`benchmarks/probes/results.jsonl`、`benchmarks/probes/probes.json` |
| 改测试来凑绿这件事已被探针用 guard_hashes 钉住 | `benchmarks/probes/probes.json:80`、`benchmarks/probes/probes.json:105` |
| 数字核对器（可复用的「断言里的事实」机械核对） | `src/spoolkit/check/numbers.py:107`、`src/spoolkit/check/numbers.py:166`、`src/spoolkit/tools/sources.py:37` |

### 1.6 现状数字（取自 17 条探针记录，未跑新探针）

- `final_ok`（任务自认完成）在 **17/17** 条记录里都是 `True`。
- `check_ok`（判据验收）在 **5/17** 条里是 `False`。这 5 条即 `final_ok=True 且 check_ok=False` 的**假完成**样本：`20261003-205331 merge-html`、`20261003-205331 rename-cross-file`、`20261003-210803 merge-html`、`20261003-210803 rename-cross-file`、`20261003-220652 rename-cross-file`。
- 首轮基线（205331 那 5 条）3 绿 2 红，与探针集提交信息一致。
- `20261003-214107 rename-cross-file`：**自动验证 0/5 通过**（`verifier_passed=0`、`verifier_failed=5`），`final_text` 里写着「最终验证：未通过」，而 `settled="auto"` 仍然自动落盘；同时该条的 `check_ok=True`（验收判据认为活干对了）。这是同一现象的两个面：判决没有约束力 + 验证会误报。
- `20261003-220652 rename-cross-file`：残留恰好在本文件 `pipeline/fetch.py`（1 处），**残留检测没报**，探针判据 `CHECK-FAIL` 报了（`check_output`）。
- `supervisor_false_claims` 在 17 条里全是 0；`rewrite_refusals` 全是 0。

### 1.7 缺口清单

- **G1 [缺口] 没有阻断语义。** 模型 `done` 即收尾，验证失败只追加一句话：`src/spoolkit/agent/loop.py:1637`、`src/spoolkit/agent/loop.py:1812`；落盘只看 policy/scope，不看验证结果：`src/spoolkit/cli/commands/run.py:250`、`src/spoolkit/cli/settle.py:64`。这正是「接受了一份最终验证未通过的改动」的机制。
- **G2 [缺口] 残留检测有盲区。** 检测跳过被改的文件本身（`src/spoolkit/tools/edit.py:254`）与已扫过的路径（`src/spoolkit/tools/edit.py:272`），只报「别处还在」；删除文件直接不查（`src/spoolkit/tools/edit.py:249`）。220652 那条的残留就在本文件里，工具没看见。
- **G3 [缺口] 「测试被改弱」只挡住了一部分。** 现有防线是「按原测试判定」（`src/spoolkit/tools/verify.py:270`、`src/spoolkit/tools/exec.py:544`），它挡得住「把断言改成 `assert True`」「删测试文件」（`src/spoolkit/tools/exec.py:560`）。挡不住的是**不在测试文件判定口径里的东西**：`conftest.py`、`pyproject.toml`/`pytest.ini` 里的 `addopts`/`-k`/`--ignore`、非 `.py` 的测试数据与 golden 文件、以及 `tests/helpers.py` 这类被 `is_test_path` 判为非测试的模块（`src/spoolkit/tools/edit.py:155`、`src/spoolkit/tools/edit.py:110`）。关联目录（绝对路径）的改动既不还原（`src/spoolkit/tools/exec.py:557`）也不跑工作区测试（`src/spoolkit/tools/verify.py:281`）。
- **G4 [缺口] 完成声明与台账不挂钩。** `fact_conflicts` 只认两种形状（`src/spoolkit/agents/supervisor.py:197`、`src/spoolkit/agents/supervisor.py:208`）；「已统一」「已删除」「已实现 X」都不核对；收尾结论也没有「每条声明挂哪条证据」的账（`src/spoolkit/agent/loop.py:1756` 只挂整体状态）。
- **G5 [缺口] 「声称写的文件在不在」没有核对。** `_file_state_line` 会写「（现在不存在）」（`src/spoolkit/agent/loop.py:1744`），`verify_file` 对不存在报错（`src/spoolkit/tools/verify.py:1200`），但没有一处拿「结论里提到的路径」与台账比对。
- **G6 [缺口] L2 三项在产品里没有。** 对照运行 / 公共接口快照 / 性能不倒退目前只活在探针判据里（`benchmarks/probes/checks/vague_optimize.py`、`benchmarks/probes/checks/add_feature.py`）。
- **G7 [缺口] 误报率没有度量。** 214107 那条（验证 0/5 未过、验收通过）就是一次误报，但记录里没有任何字段把它记下来；用户会因此学会忽略验证结论。
- **G8 [缺口] 主循环不知道 scope。** scope 判定只在落盘（`src/spoolkit/cli/settle.py:65`）与计划/派发（`src/spoolkit/agents/plan.py:580`、`src/spoolkit/agents/runtime.py:153`）路径上，自动验证路径不看它。
- **G9 [缺口] 「批复前不落盘 + 挂起待批」是**已否决**的写法。** 本文件上一版把升级处置写成「**先请示、等批复，再决定落盘**（不先落盘再问）」「升级项**挂起为待批**（不落盘、不动工作区）」（见 §3.2.0 引用的原文）。它已被否决，原因：升级路径一旦落在「改动规模超阈值」（T7）这类**不涉及任何危险动作**的条件上，整轮的文件编辑就被扣在暂存里；用户在无人值守或长任务里跑很久，回来看到的可能是一个**空工作区**（`settle` 的拒绝/丢弃分支只打印一句就 `pending.discard()`，`src/spoolkit/cli/settle.py:60`、`src/spoolkit/cli/settle.py:90`）。这不是安全，是**把成果当人质**——而这类产品（Codex / Cursor 这一系的普遍模式）默认直觉恰恰相反：「看得见的工作区」。替代写法见 §3.2.0 与 §2.5。**注意 §2.5 的不变量约束的是「成果有没有落盘、有没有回退」，不是「编辑内容不许删改」**——它带反向条款，删死代码、合并重复实现、删已被替代的文件都算正常编辑。

---

## 2. 分层审查模型

四层，各层只回答一个问题。**下层的结论永远不覆盖上层的机械事实**：L1 说「旧名还在 README 第 7 行」时，L3 说「已统一」不算数。

### L1 机械一致性（零模型、零新依赖；必须最全）

**[设计]** 统一入口 `review_mechanical(root, pending, scope, facts) -> list[Finding]`，全部复用现有原料：`WorkspaceView`（叠加待确认改动）、`pending.items()`、`edit.lingering_references`、`verify.analyze_text`、`plan.out_of_scope`、索引里的 import/引用图（`src/spoolkit/index/graph.py`、`src/spoolkit/index/refs.py`）。

| 编号 | 判什么 | 用什么证据 | 机械? | 失败处置 | 成本 |
| --- | --- | --- | --- | --- | --- |
| L1.1 | 改名/移动后的**全仓残留**（含 README/docs/配置/模板/字符串） | 扩展现有 `vanished_def_names` + `_name_lines`：不再跳过本文件与已扫路径；对 `delete_file` 也查；后缀表已含 `.md/.txt/.rst/.json/.toml/.cfg/.ini/.yaml/.yml/.css/.sh` 等（`src/spoolkit/tools/edit.py:199`） | 是 | **阻断**（高严重度） | 近零：沿用 400k 字符 / 8 文件上界（`src/spoolkit/tools/edit.py:191`） |
| L1.2 | 本地引用闭合：`import`/`require`/路径字符串/`src`/`href` | `.py` 用 `ast` + 索引的 import 图；HTML/资源复用 `_resource_report`（`src/spoolkit/tools/verify.py:745`，已能判「引用的本地文件都存在」） | 是 | 阻断 | 近零（HTML 那部分已有） |
| L1.3 | 被删符号/文件是否仍被引用；有没有替代 | 索引符号表（`src/spoolkit/index/symbols.py`）+ 全仓扫描 | 是 | 阻断 | 近零 |
| L1.4 | **测试是否被改弱**：删用例/跳过/放宽断言/`xfail` | 改动前快照：用例名集合 + 各文件用例数 + 断言数 + 测试相关文件指纹（`conftest.py`、pytest 配置、测试数据）。基线可用 `Baseline` 全文（`src/spoolkit/tools/edit.py:1520`） | 是 | 阻断 | 近零（纯静态） |
| L1.5 | 越界改动（scope 之外的文件被改） | `out_of_scope`（`src/spoolkit/agents/plan.py:580`）用同一份 scope | 是 | 阻断该处，范围内的照落（沿用 `pending.drop` 粒度，`src/spoolkit/cli/settle.py:80`） | 零 |
| L1.6 | 声称的产物真实存在 | 从结论里抽路径（`looks_like_path`，`src/spoolkit/tools/edit.py:169`）与台账/`WorkspaceView.exists` 比对 | 是 | 阻断 | 零 |

L1 的**默认输出为零**：没问题就什么都不说（沿 `make_checker` 只回灌问题的口径，`src/spoolkit/tools/verify.py:1261`）。

### L2 行为回归（半机械）

| 编号 | 判什么 | 证据 | 机械? | 处置 | 成本 |
| --- | --- | --- | --- | --- | --- |
| L2.1 | 项目测试 | 已有 `make_verifier`（`src/spoolkit/tools/verify.py:201`） | 是 | 阻断 | 一次 pytest（受 `command_timeout` 约束，`src/spoolkit/tools/exec.py:45`） |
| L2.2 | **对照运行**：同输入、改动前后逐字比对输出 | 用 `Baseline.old_text` 在试跑副本里复原「改动前」的树（`src/spoolkit/tools/edit.py:1520` + `trial_workspace`，`src/spoolkit/tools/exec.py:536`），两边跑同一条入口命令，`diff` 输出。探针 `vague-optimize` 就是这个思路（40000 行输入、摘要与独立参考逐字相同，`benchmarks/probes/probes.json:65`） | 半（需要对「跑哪条命令、拿什么输入」） | 阻断（逐字不一致） | 2 次子进程 |
| L2.3 | 公共接口快照：导出签名 / CLI `--help` | `ast` 取公开 def/class 签名；CLI help 文本快照 | 是 | 阻断（删除/改签名） | 近零 |
| L2.4 | 性能不倒退 | 任务被判定为「优化类」时，对基准输入计时，与改动前那次对比 | 半（阈值要标定） | 记录 + 超阈值阻断 | 2 次计时 |
| — | 触发条件 | 只在「有可运行的入口」或「改动触及公开符号」时跑；否则明说没跑（沿 `CANNOT_CHECK` 口径，`src/spoolkit/tools/verify.py:426`） | — | — | — |

### L3 语义审查（用模型，但独立上下文 + 结构化输出 + 事实核对）

**[设计]** 复用 `reviewer` 角色（`src/spoolkit/agents/runtime.py:80`）与既有的「审查者不能写代码」边界（`src/spoolkit/agents/runtime.py:112`），把结论从 `pass/fail` 升级为**发现清单**：

```json
{"findings": [{"file": "...", "line": 12, "problem": "...",
               "severity": "high|medium|low", "suggestion": "...",
               "evidence": "L1.stale_reference | L2.regression | quote:..."}],
 "verdict": "pass|fail", "reasons": ["..."]}
```

- **只看 diff + 用户请求 + 证据**（L1/L2 结果、测试输出），**不看实现者的自我陈述**——这条已有先例：督导证据包里刻意没有对话历史（`src/spoolkit/agent/loop.py:1867`），审查者的证据包只给改动差异与验证结果（`src/spoolkit/agents/dispatcher.py:148`）。
- **每条事实性断言都要过机械核对**：复用 `fact_conflicts` 的形状（`src/spoolkit/agents/supervisor.py:260`），但把 `RunFacts` 扩成 `ReviewFacts{tools, edits, changed, checks_run, check_results, test_inventory}`；`ReviewFacts` 里没有支撑的断言 → 该条 finding 降级为 `unverified` 并从结论里剔除（不注入、不据此阻断）。
- **对抗性**：专门回答「它声称做到了，做到了吗」。首轮 `rename-cross-file` 就是这一类（README/docs 旧名没动却宣称已统一，`benchmarks/probes/probes.json:140`）。做法是把结论里的每条完成声明映射到 L1/L2 的检查项，缺证据即 `false_completion`。
- 解析不出来按未通过（沿用 `src/spoolkit/agents/dispatcher.py:96`）；「没结论」与「判不合格」继续分开（`src/spoolkit/agents/dispatcher.py:67`）。
- 数字类断言复用 `check/numbers.py`（`untraceable`/`mismatched`，`src/spoolkit/check/numbers.py:166`、`src/spoolkit/check/numbers.py:300`）。

### L4 证据账本

**[设计]** 每次运行产出一份 `ReviewLedger`，逐条记：`claim`（完成声明）/ `check`（哪条 L1/L2 检查）/ `result`（通过/失败/查不了）/ `evidence`（输出摘要 + `file:line` 或退出码）/ `content_key`（内容指纹，复用 `_content_fingerprint`，`src/spoolkit/agent/loop.py:207`）。

- 收尾时，**没有账本条目的「完成」声明一律降级为「未验证」**，并写进最终结论（`_with_final_state` 的同一出口，`src/spoolkit/agent/loop.py:1756`）。
- 「查不了」必须带出来（`src/spoolkit/tools/verify.py:1091` 已经这么做，账本只是把它结构化）。

### 2.5 工作区不变量（硬不变量，必须钉住）

**[设计]** 本节是本设计**唯一**的硬不变量。它比「阻断语义」（§3.1）与「风险分级放行」（§3.2）优先。**先说清约束对象：这条约束的是运行/审查层对用户的承诺，不是对模型编辑内容的禁令。** 它管的是「产出有没有交出来」，不是「内容改没改、删没删」：**只要有产出，就必须已经落在工作区（或隔离副本）里、并且有可执行的回退；不得把成果扣在系统内部**。换句话说，它禁止的只有一种状态——**成果被系统扣住不写盘**；「删死代码」「合并重复实现」「删已被替代的文件」是正常编辑，与本条无关。

**反向条款（counter-clause，**本不变量的组成部分**，不得省略）**：下面这些是**允许且正常**的编辑，不是违规——删除死代码与冗余分支、合并重复实现、精简文件、**删除已被替代的文件**、重命名与移动、拆分成多文件。**本不变量只要求落盘 / 可回退 / 可列出，删除本身不是违规。** 要判定违规，判的从来不是「删了东西」，而是「产出没落盘 / 没有回退 / 收尾报告列不出来」。

**硬规矩（给未来）**：这段文字若进入**模型可见的上下文**（提示词、拦截话术、督导证据、`src/spoolkit/context/templates.py` 等），**必须连同上面这段反向条款一起进，且不得改写为「禁止删除」类表述**——只进前半段就等于把一条「落盘义务」读成「编辑禁令」，模型会因此不敢删死代码、不敢合并重复实现、不敢精简文件、不敢删已被替代的文件（这条规矩的理由见 §8 D12）。

**判定口径（与「删不删」无关）**：只要模型产出了改动，就必须满足下面三条——**产出在盘上、有整轮回退命令、收尾报告能逐条列出**；三条缺一即视为运行失败，且失败原因要写进收尾报告（列被删的文件与位置也算「列出」）：

1. **已经落在工作区（或隔离副本）里、看得见**：不允许把已产出的文件编辑扣在暂存/内存中不写盘（`src/spoolkit/tools/edit.py:1225` 的 `PendingChanges` 在本设计里**不再充当**「批复前不落盘」的闸门，只充当「同一次落盘的审计与回退依据」）。隔离模式（§3.2.0）下写在**独立副本**里也算满足。
2. **有可执行的回退命令**：落盘的每一轮改动都配一份 **Baseline（改动前快照）** 与**一条可直接执行的整轮回退命令**（`Baseline`/`BaselineEntry`，`src/spoolkit/tools/edit.py:1542`、`src/spoolkit/tools/edit.py:1528`；落盘/读取 `save_baseline`，`src/spoolkit/tools/edit.py:1582`；回退实现 `revert`，`src/spoolkit/tools/edit.py:1593`；命令 `spool revert`，`src/spoolkit/cli/commands/revert.py:10`）。回退必须能**一处一处**做（逐路径），也能**整轮**做（一条命令）。
3. **能在收尾报告里被逐条列出**：改了什么路径、多少行、内容指纹、回退命令各是什么（挂在 L4 账本上，`src/spoolkit/agent/loop.py:1756` 的同一出口）。

**若因高风险而暂缓，暂缓的是「具体动作」，不是成果**：该动作必须在报告里**点名**，并且带齐 §3.2.3 的五件（影响 / 结论 / 建议 / 选项 / 回退）。「本轮没做 X（等批复）」与「本轮什么都没留下」是两种状态，后者永远不允许。

**唯一允许「扣住成果」的正当理由**：**给不出回退能力**（没有 Baseline、或给不出一条可执行的回退命令，即 T11 / T13）。此时**这一轮不许落盘**，并且必须把这条理由写进收尾报告（是「不敢写」，不是「不告诉你」）。除此之外没有任何理由让产出被扣在系统内部（注意：不落盘指的是**成果没写出来**，不是「不许删东西」）。

**怎么验（可证伪）**：见 §6 的 `workspace_not_empty` / `changes_on_disk` / `revert_available` 三个探针字段（判据口径见 §6 字段表），与每期验收里那条「运行结束时若报告产出过改动，则盘上能看到且回退可用」。

### 2.6 分层与现有循环的接线点

1. L1/L2 挂在 `make_verifier` 之后、同一个 `ToolResult` 出口（`src/spoolkit/tools/verify.py:353` 的 `_merge_types` **只加不减**），不新开第二条验证通道。
2. **阻断动作**的判定放在动作执行口（命令/删除/迁移/联网/越界写，见 §3.2.2 的触发条件表）**之前**；**不许宣称完成**的判定放在 `turn.done` 之前（`src/spoolkit/agent/loop.py:1637`）。**落盘不再有「批复前」这道闸门**：成果按 §2.5 持续落盘，落盘口（`src/spoolkit/cli/settle.py:64`）只做两件事——查 Baseline/回退可用（给不出就按 T11/T13 不落并把理由写进报告），以及把这一轮记进账本。
3. L3 只在触发条件满足时跑，结论以事件发出（沿用 `note` 事件，`src/spoolkit/agent/loop.py:1912`）。

---

## 3. 阻断语义

### 3.1 不许静默放行（原有阻断语义）

**[设计] 什么情况下不许宣称完成**（`finished=True` 一律不允许）：

1. L1 有任一高严重度发现：残留引用、引用断裂、被删符号仍被引用、测试被改弱、越界改动、声称的产物不存在。
2. L2 回归：项目测试失败；对照运行输出逐字不一致；公共接口被删/改签名；优化类任务性能倒退。
3. L3 高严重度发现未被处理（未修、也未给出机械可核对的理由）。

处理顺序（默认不打扰；命中 §3.2.2 的触发条件才请示一次）：

1. 把发现当反馈重新注入下一轮（现有机制 `src/spoolkit/agent/loop.py:1570`），给一个有限的补救步数预算（沿用 `step_ceiling`/`verdict.grants`，`src/spoolkit/agent/loop.py:1139`）。
2. 预算用尽仍不通过 → **先按 §3.2.2 看有没有命中升级触发条件（T1–T13）**，但**升级拦的是动作、不是成果**：命中的若是**危险动作**（T13、以及 T5/T6 里「执行」的那一面），该动作在执行前被拦下、进「等批复」；命中的若是**纯规模/纯语义条件**（T7、T8、T9、T10、T12 这类不涉及危险动作的），**成果照样按 §2.5 落盘**，只是把「不许宣称完成」照 §3.1 第 1 条写进结论。未命中任何触发条件时同样 `finished=False`，结论写成「未完成（被审查拦下）+ 每条发现 + file:line」——**但落盘与否只看 §2.5**：有 Baseline 与整轮回退就落盘并记账，给不出回退才按 §2.5 的例外不落并把理由写进报告。
3. 无人值守（`non_interactive`）时**不弹问题**：自动修 N 轮仍不通过的高严重度发现按 §3.2 升级——**成果落盘、被拦的危险动作挂起为待批、可续跑**（不是静默放行，也不是一律拒绝，更不是把成果扣住不落盘的「空工作区」）。越界那几处单独丢、范围内的照常处理（`src/spoolkit/cli/settle.py:80`），范围内的**高严重度**发现只降级结论，不牵连同一次运行里已经落盘的成果。
4. 显式例外只能来自命令行开关（例如 `--allow-unverified`），默认关闭；这是开放决策，见 §9。

**谁有权放行**：**默认无人**——默认路径就是「机械判据全部通过 → 自动放行」，不打扰用户。**人工放行只有一条通道**：§3.2 的按风险升级拿到批复；没有命中触发条件的改动不请示、也不阻塞。`agent` 自己永远不能放行（它会自称完成，17/17 条记录都是 True）。第 4 条那种命令行开关只是用户**事前**显式选择的例外，不是审查放行。

### 3.2 风险分级放行（risk-tiered gate）

**[设计]** §3.1 只回答「不许静默放行」；这一节回答「那什么时候该问人」。两者是**同一套机器**：先不许静默放行，再按风险决定「拦哪个动作」与「要不要请示」。

#### 3.2.0 拦动作、不拦成果（落盘口径；替代被否决的「批复前不落盘 + 挂起待批」）

**[设计]** 这一类产品（Codex / Cursor 这一系的**普遍模式**）的默认直觉是「**看得见的工作区**」，不是「批复前什么都不写」。本设计照这个口径定（**注意本表分的是「成果」与「动作」两类，不是「允许改」与「不许改」两类**：删文件既会作为**成果**被要求落盘、被列出，也会作为**危险动作**在执行前被拦一次，两者不矛盾）：

| 类别 | 覆盖什么 | 处置 |
| --- | --- | --- |
| **文件编辑（含大规模改动）** | 写/改/删文件的**成果**（**删文件也算成果**；这里的意思是「产出要看得见」，不是「不许删」）；`PendingChanges` 里已经产出的新文本、大重写、批量改动（对应 T7 的规模条件、T1–T4 的路径条件命中的**编辑**、T9/T12 的语义问题所在的那批编辑） | **持续落盘到工作区**（隔离模式下落到独立副本，§3.2.5）。**看得见**是第一要求——**要求的是产出写出来**，不是「不许删除、不许精简」 |
| **危险动作** | 执行命令（尤其写操作/`shell=True`）、删除文件、迁移、联网、越界写（对应 T2/T3/T4/T5/T6，以及 T13 的「无回退落盘」） | **在执行前拦下、请示一次**（§3.2.3 五件）。拦的是**这次动作**，不是这一轮的全部成果 |

**「待批」的语义在本次修订里被改写**（这是对被否决写法的直接替换）：

- **旧写法（已否决）**：「**先请示、等批复，再决定落盘**（不先落盘再问）」「升级项**挂起为待批**（不落盘、不动工作区），运行以「待批复」结束」。
- **新写法**：**待批 = 某一个被拦下的危险动作正在等批复；其余成果已经落盘**。运行可以在「有 N 个动作待批」的状态下结束，而工作区里已经摆着这一轮做出来的改动。

**为什么这么定**（先记被否决的原因，再记正向理由）：

- **旧写法为什么会被否决**：`settle` 的拒绝/丢弃分支只打印一句就 `pending.discard()`（`src/spoolkit/cli/settle.py:58`、`src/spoolkit/cli/settle.py:60`、`src/spoolkit/cli/settle.py:90`）。升级条件一旦落在 T7 这类**不涉及危险动作**的规模线上，被扣住的是**整轮的文件编辑**。用户在无人值守下跑很久，回来看到的是**一个空的工作区**（成果被扣在系统内部，不是「用户删过东西」）——这正是用户提出的顾虑。它既不符合「看得见的工作区」这一默认直觉，也让「可续跑」变成唯一的补救手段（续跑失败就等于全丢）。
- **新写法的正向理由**：落盘之后，人的决策从「放行 / 不放行」变成「**保留 / 回退 / 合并**」——这是一次**更便宜、信息更全**的决策（能看到东西再决定），也正是事后可审可撤这套做法成立的前提（回退能力见 §2.5）。
- **不这么定会怎样**：不做「持续落盘」，就只能靠「什么都不给」来避免风险——用「用户跑了很久、回来发现成果被扣住没落盘」换「没写下可能不好的改动」，代价与收益不成比例；而不做「危险动作前置拦截」，就等于用「成果已落盘」当借口把不可逆动作一起放行。两者必须同时成立。

#### 3.2.1 默认路径：机械判据全过 → 自动放行、不打扰用户

- 触发条件：L1 无高严重度发现、L2 无回归、L3 无未处理的高严重度 finding。
- 处置：自动落盘，不弹问题、不进人眼，沿用现有 `auto` 路径（`src/spoolkit/cli/settle.py:70`）。
- **为什么这么定**：AI coding 的价值就是高速迭代，默认路径必须零打扰；17/17 条 `final_ok=True` 也说明「把结论端给用户看」本来就不构成防线（§9.1）。
- **不这么定会怎样**：每一步都问一次 = 把审阅成本塞回用户，用户学会点「全部同意」，等于没有审查，只多了一次点击。

#### 3.2.2 升级路径：命中任一触发条件 → 主动请示并等待批复

触发条件必须**可机械判定**（路径匹配 / AST / 既有判定函数的返回值），不写「高风险」这类形容词：

| # | 触发条件 | 机械判据（判在哪） |
| --- | --- | --- |
| T1 | 删除/重命名**公共接口或导出** | 公开符号快照出现删除或签名变更：`__all__`、模块级公开 `def`/`class` 签名、CLI 子命令与 `--help` 文本（即 L2.3；P1 只用其中 `ast` 签名快照这一半，见 §6 P1） |
| T2 | 改**依赖与构建/部署配置** | 路径命中 `pyproject.toml`、`setup.cfg`、`requirements*.txt`、锁文件、`tox.ini`/`pytest.ini`、`.github/workflows/**`、`Dockerfile`、`Makefile`、`scripts/**`、`*.sh`/`*.ps1` |
| T3 | 触及**认证/密钥/安全** | 路径或内容命中 `*auth*`/`*secret*`/`*token*`/`*credential*`/`.env*`/`*.pem`/`*.key`；或改到 `policy.py`、`cli/approval.py`、scope 判定处；或新增 `shell=True`/`eval`/`exec`/出站网络 |
| T4 | **数据/schema 迁移** | 命中 `migrations/**`、`*.sql`、`CREATE TABLE`/`ALTER TABLE`，或改到本仓 `PRAGMA user_version` 的步进（`src/spoolkit/memory/store.py:849`、`src/spoolkit/memory/store.py:869`） |
| T5 | **批量删除文件** | `delete_file` 次数或删除文件数越过 Knob（默认与 `review_limit` 同量级，`src/spoolkit/limits.py:209`）；单次删除且被删者无入边引用可降级为「记录」 |
| T6 | **越界或工作区外路径** | `out_of_scope` 命中（`src/spoolkit/agents/plan.py:580`），或解析后的真实路径不在工作区根（`..`、绝对路径、符号链接逃逸） |
| T7 | **改动规模超阈值** | 改动文件数/行数/发现条数越过 `limits.py` 的 Knob（`review_trigger_files`，§5） |
| T8 | **L2 回归** | 项目测试失败 / 对照运行逐字不一致 / 接口快照变化 / 优化类性能倒退 |
| T9 | **L3 高严重度未处理** | `severity=high` 的 finding 未修、也没有机械可核对的理由（沿「解析不出按未通过」口径，`src/spoolkit/agents/dispatcher.py:96`） |
| T10 | **没有验证器可用的高风险改动** | 命中 T1–T7 任一，且同一改动上带着「查不了」（`CANNOT_CHECK`/`uncovered`，`src/spoolkit/tools/verify.py:426`、`src/spoolkit/tools/verify.py:1091`） |
| T11 | **不可回退**（看不清时拦动作） | 改动没有 Baseline 快照、给不出一条可执行的撤销命令（`Baseline`，`src/spoolkit/tools/edit.py:1542`；回退实现，`src/spoolkit/tools/edit.py:1593`；命令，`src/spoolkit/cli/commands/revert.py:10`）。**T11 在对回退能力还没把握时拦「动作」，T13 在已经确定给不出回退时不许「落盘」**：两者是同一条前置条件（§2.5）的两个出口 |
| T12 | **L1/L2 高严重度未处理** | 自动修 N 轮后（`step_ceiling`/`verdict.grants` 用尽，`src/spoolkit/agent/loop.py:1139`）阻断项仍在，且不属于 T1–T11、T13 任一条 |
| T13 | **回退能力不足** | 已知这次改动**给不出** Baseline 或整轮回退命令（新建文件的基线要让回退能删回「它不存在」，`src/spoolkit/tools/edit.py:1528` 的 `existed`；删除文件的回退要能写回全文，`src/spoolkit/tools/edit.py:1593`）。**这是唯一「扣住成果」的正当理由**（§2.5），且理由必须写进报告 |

处置分四类，**成果是否落盘只由第 1 类决定，其余三类都不碰成果**：

| 命中 | 处置 |
| --- | --- |
| **T13**（给不出回退） | **不落盘**，理由写进收尾报告（§2.5 的唯一例外）；运行结束在「不敢写」状态，不是「待批复」状态 |
| **T11、T2/T3/T4/T5/T6 的「动作」那一面** | **拦下该动作**（执行前请示），**这一轮其余成果照落盘**；批复「批准」才执行该动作、批复「只批准其中几处」只执行被批准的那几个动作、批复「撤销」不执行该动作（成果仍在工作区，可用回退命令撤） |
| **T7 / T8 / T9 / T10 / T12**（纯规模 / 纯语义，不涉及危险动作） | **成果照落盘**；把「不许宣称完成」写进结论（§3.1），并在收尾报告里点名是哪一条触发条件被命中 |
| **T1**（删/改公共接口签名） | 拆成两半：「改文件」这一半照落盘（于是 `--help`/签名快照的差异立刻看得见）；「把它当成本次运行的最终结论」这一半等批复 |

自动修补仍在前面：先按 §3.1 的处理顺序自动修 N 轮，修不掉才升级——升级是「拿不准才问」，不是「一有风险就停」，更不是「一有风险就把东西收走」。

- **为什么这么定**：T1–T13 的共同点是「改坏了不便宜」——公共接口、依赖与部署、认证密钥、数据迁移、批量删除、越界路径，都是事后难回滚或会影响别人的改动，值得一次人工批复。而「值得一次批复」并不等于「值得让工作区空着」：**批复的成本换的是动作的执行权，不是成果的可见性**。
- **不这么定会怎样**：默认路径的「自动放行」落在这些改动上就是**静默承担不可逆风险**；反过来，如果只是把这些改动一律硬拦、不给人一次放行的机会（§7 原措辞的极端读法），在极端情形下等于白跑一趟长任务——用户既没机会放行，也拿不到产物。上一版更进一步，把产物**扣住**到批复为止，那是本条顾虑的出处（G9）。

#### 3.2.3 请示契约：一次说清五件

**每次升级必须一次给全五件**（缺任一即视为无效请示，记录里判不合格）：

**先说清请示的对象**：五件说的是**「动作 / 合并」**，不是**「成果」**。被请示的是一次**尚未执行**的危险动作（跑命令、删文件、迁移、联网、越界写），或隔离模式下的**一次合并**（§3.2.5）；**已经落盘的成果不在请示范围内**——它已经在工作区里，用户看到请示的同时也看得到它。这一条是本次修订的核心：**请示不能变成“成果的释放条件”**（同理，**被请示不等于「这种编辑被禁止」**：拦住的是这一次动作的执行，不是「不许删这个文件」）。

| # | 要件 | 内容与证据要求 |
| --- | --- | --- |
| ① | **影响** | 哪些文件 / 哪些公共接口 / 哪条流程 / 谁会受影响（有上下游就写出来）。**这一段只说已经被拦下的那个动作**会动到什么，不重复报「已经落盘了什么」 |
| ② | **结论** | 自动审查与验证的结果 + 证据（`file:line`、命令与退出码、测试输出摘要）——**只给结论与证据，不贴 diff** |
| ③ | **建议** | 推荐怎么做（明确「建议批准 / 建议改方案 / 建议撤销」），有替代方案就并列 |
| ④ | **选项** | **针对动作粒度**：批准这个动作 / **只批准其中几处**（哪几个动作）/ 撤销 / 改方案（四档，与现有四档批复词表对齐，`src/spoolkit/bridge/agent_runner.py:35`）。**「只批准其中几处」的粒度是动作，不是成果**——成果不需要逐处批准 |
| ⑤ | **回退保证** | 一条**可直接执行**的撤销命令（`spool revert`，`src/spoolkit/cli/commands/revert.py:10`）；给不出就按 T11 / T13 明说「没有回退」，并说明因此**这一处不会落盘**（§2.5） |

**呈现方式**（要短、可复制、不必读 diff）：

| 通道 | 落点 | 形态 |
| --- | --- | --- |
| 终端 | `settle()` 的确认路径（`src/spoolkit/cli/settle.py:94`）+ `ask` 注入（`src/spoolkit/cli/approval.py:22`） | 五件各一行 + 一条撤销命令；**默认答案是「不执行这个动作」**（与现有 `[y/N]` 默认 N 一致；注意默认不再是「不落盘」——成果已经落盘了）；输出必须走 `out`，否则 `--events` 的 JSON 行会被污染（`src/spoolkit/cli/approval.py:12` 记着这次事故） |
| 网页壳 | 已经在跑的等待通道 `awaiting` / `awaiting_detail`（`src/spoolkit/web/runner.py:448`）与确认卡片（`src/spoolkit/web/page.py:2516`） | 五件塞进 `awaiting_detail` 的短字段；批复走 `Runner.confirm`（`src/spoolkit/web/runner.py:390`）与 `Runner.answer`（`src/spoolkit/web/runner.py:394`） |
| 聊天通道 | `bridge` 的待确认问答（`src/spoolkit/bridge/agent_runner.py:212`、`src/spoolkit/bridge/agent_runner.py:228`） | 一条消息给五件 + 撤销命令，按四档词批复（`src/spoolkit/bridge/agent_runner.py:35`） |
| MCP / 任务状态 | `status.awaiting > 0` 已经是「需要审批」的信号（`src/spoolkit/mcp/server.py:440`） | 复用同一信号，不新增通道 |
| **隔离模式（新增）** | 跑完给 diff 与合并/丢弃选项（§3.2.5） | 五件里的「影响」= 这批改动合并会动到什么；「选项」= 合并全部 / 只合并其中几处 / 丢弃 / 改方案；**被拦的是「合并」这个动作，不是副本里的成果** |

**超时与无人值守行为**（**成果已落盘**、被拦的动作挂起为待批、可续跑）：

- 有人在（有 TTY / 网页壳 / 聊天通道在线）：给五件，等批复；**等不到批复就不执行那个动作**——成果不因此回滚，也不因此被扣住（§2.5）。用户随时可以用 ⑤ 给的命令回退。
- 无人值守（`non_interactive`，`src/spoolkit/cli/settle.py:88`）：**不弹问题、绝不静默放行、也不一律拒绝**——**先把成果落盘**（§2.5），被拦的**危险动作**挂起为待批，运行以「待批复（N 个动作）」结束；需要跨结算保留的只是那些**还没执行的动作清单**（`src/spoolkit/cli/settle.py:113` 的 `staged_items` 读数，`src/spoolkit/tools/edit.py:1339`），批复后从挂起点续跑。同一次运行里的非升级项照现有语义处理（越界处单独丢，`src/spoolkit/cli/settle.py:80`）。**未落盘的只剩 T13 那一类**（给不出回退），且必须在报告里点名。
- 超时只有一个含义：**挂起多久之后可以再次请示**。过期**不自动执行动作**（超时放行等于没有阻断），也**不当拒绝**（当拒绝会丢掉「这个动作本来有机会被批准」，且会把「可续跑」变成「重跑」）。

**批复与审计**（谁批的、依据什么、记在哪）：

- **谁批的**：批复来源三通道之一（终端 / 网页壳 / 聊天通道）连同批复词原样记录；`agent` 自己无权批复。
- **依据什么**：记触发条件 ID（T1–T13）+ **被拦动作**的内容指纹（复用 `_content_fingerprint`，`src/spoolkit/agent/loop.py:207`）。指纹变了说明批复针对的不是眼前这个动作，批复作废、重新请示。
- **批复不撤销成果**：批复「撤销 / 不批准」的语义是「**不执行这个动作**」，**不是**「回滚已经落盘的成果」。要回退成果，用批复里 ⑤ 给的那条 `spool revert` 命令（一处一处或整轮）——这是**两个分开的动作**，记录里必须分开记，不能把「没批准」记成「没改动」。
- **记在哪**：事件流（沿用 `note` 事件，`src/spoolkit/agent/loop.py:1912`）+ L4 证据账本 + 探针记录字段（§6）；判错的升级写进 lessons（`src/spoolkit/agent/loop.py:426`）。

#### 3.2.4 打扰预算（升级率必须有度量）

升级本身是**新的打扰**：升级率过高等于把审阅又塞回给用户，本设计的初衷就被推翻。**本次修订后，升级不再阻塞成果落盘**：被升级的对象是**动作 / 合并**，成果已经按 §2.5 落在工作区里。因此这张表量的是**「每轮打断了用户几次、每次打断是否值这个价」**，而不是「卡住了多少成果」——**升级率高低与工作区空不空是两件不相干的事**（后者由 §2.5 的硬不变量钉住，不由这张表管）。要记的指标（落到 §6 的探针字段）：

| 指标 | 定义 | 目标量级 |
| --- | --- | --- |
| `escalation_rate` | 升级（被拦动作）次数 / 运行次数 | **< 1 次/运行**；≥ 0.5 视为设计失败，回去改触发条件 |
| `blocked_actions` | 一轮运行里被拦下、等批复的**危险动作数** | 记录；与 `changes_on_disk` 一起看——**理想形态是「`changes_on_disk` > 0 且 `blocked_actions` 很小」**，出现「`blocked_actions` > 0 而工作区空」即违反 §2.5 |
| `escalation_recall` | 命中 T1–T13 的**动作**里被拦下的比例 | **100%**（漏拦 = 静默执行危险动作） |
| `escalation_precision` | 被拦的动作里，事后被探针判据/人工核对确认为真高风险的比例 | **100%**（每条都能回指一个具名触发条件；出现「无触发条件的升级」即不合格） |
| `escalation_by_trigger` | 按 T1–T13 分档的次数 | 找出哪个条件在刷屏，去调那个阈值，而不是调用户的耐心 |
| `escalation_wait` / `escalation_abandoned` | 人等了多久才批复 / 挂起后再没被批复的次数 | 只记录；`abandoned` 高说明提示位置不对。**注意 `abandoned` 不再意味成果丢失**——成果已在工作区，丢的只是那个动作的执行机会 |
| `escalation_false_alarm` | 拦了动作、但验收判据全绿（同 §4 的 `review_false_alarm` 口径） | 记录 + 报告单列；被这类刷屏就调阈值 |

以上全部是 **[设计] 目标**，不是实测：探针集跑完之前不设具体阈值，先记录、再按 §8 D9 标定。

#### 3.2.5 隔离模式：`--isolated`（新副本里跑完给 diff，再决定合并或丢弃）

**[设计] 开关名**：`--isolated`（命令行；plan/自动化调用方同名参数）。**这是新增设计，仓库里目前没有这个开关**——`grep -rn "worktree\|--isolated" src/` 无命中，隔离能力今天只有一处雏形：自动验证用的**一次性试跑副本**（`trial_workspace`，`src/spoolkit/tools/trial.py:66`；复制入口 `copy_project`，`src/spoolkit/tools/trial.py:60`；实现方式是 `shutil.copytree` 跳过虚拟环境与缓存，`src/spoolkit/tools/trial.py:43`）。该副本**用完即删**，是「验证用」，不是「产出用」；隔离模式要的是「**把成果留在副本里**」。

**边界（隔离模式不碰用户主线）**：

- **写入面**：所有文件编辑落到**独立工作副本**（或独立分支）。用户主线工作区在整个运行期间**零改动**——这是它比默认模式更宽松的前提：**隔离模式下高风险也可以先落盘**，因为它不碰用户主线。
- **不出网**：网络类动作（T3 的联网那一面）在隔离模式里**不在本节的放宽范围**——出网是环境副作用，不是「写在哪儿」的问题，仍按 §3.2.2 拦下。
- **命令类动作**：在副本里执行，边界是「不得影响副本之外」（绝对路径出副本、越界写、操作副本外的仓库/服务，仍按 T6 拦）；这条是**[设计]**，具体白名单/黑名单要在实现时按 `limits.py` Knob 定，本稿不编造名单。

**与主线的关系（跑完给什么）**：

- 运行结束时给：**副本里的 diff**（哪些路径、多少行、指纹）+ **合并 / 丢弃**两个选项，以及 §3.2.3 的五件（此时「影响 / 选项 / 回退」都指向这批改动的合并与丢弃）。
- **合并是一次被请示的动作**（§3.2.3 的「动作 / 合并」）：合并前请示一次，选项粒度是「合并全部 / 只合并其中几处 / 丢弃 / 改方案」。**请示时副本里的成果一直在**，所以即使永远不合并，也不存在「成果被扣住、工作区里空的」这种状态——用户在副本里看得到东西。丢弃命令也必须在报告里给全（等价于 ⑤ 的回退保证）。
- **隔离模式也受 §2.5 管**：「已经落在隔离副本里」就算满足不变量的第 1 条；第 2 条（Baseline + 整轮回退）与第 3 条（报告里逐条列出）在副本里同样要成立——否则**副本里也不许落盘**（T13）。
- **为什么不做成「默认」**：副本/分支是额外的复制与空间成本，而默认路径（§3.2.1）本来就是零打扰；隔离模式是**给「宁可先在一个干净副本里跑完再看」的那类调用方**准备的（长任务、无人值守前后要人工过一遍的场景）。选择权给用户，不给模型。

---

## 4. 误报与噪声

- **默认零依赖、可用、可信**：L1/L2 只用标准库 + 现有 node + 项目测试（`src/spoolkit/tools/verify.py:422` 的既有口径）。
- **每条发现必须带 `file:line` 与机械证据**，否则不上报（L3 的 `unverified` 发现直接剔除）。宁可漏判也不误报——这条在督导核对里已有先例（`src/spoolkit/agents/supervisor.py:263`）。
- **误报率必须度量**，否则用户会学会忽略（214107 那条已经是活教材）：
  - `review_findings`（发现条数与严重度分布）
  - `review_false_alarm`（审查报失败但验收判据通过）——214107 rename 那条就是这个值 ≥1
  - `blocked_but_ok`（被阻断但验收绿）——阻断的代价必须可见
  - 目标：**L1 误报必须为 0**（它是机械的，有误报就是判据写错了）；L3 误报率用「高严重度发现中，探针判据/人工核对确认为真的比例」表示，阈值靠探针集标定，先不设没有依据的数字。
- 误报的去处：写进 lessons（现有机制 `src/spoolkit/agent/loop.py:426`），并把「发现 → 处置 → 结果」记进探针记录，用数字回答「自动审查有没有用」。
- 噪声控制：L1 通过就沉默；L2 只在触发条件满足时跑；L3 一次审查的发现条数设上限（超出说明改动太大，应当拆）。**升级本身也是噪声**：按 §3.2.4 度量升级率，指标超标就回去改触发条件，而不是让用户习惯点「批准」。**注意升级噪声与「成果被扣住」是两件事**：升级率高说明打断多，但被拦的是动作，成果照样落盘（§2.5）；反过来，为了压低升级率而放行危险动作才是真的错。

---

## 5. 成本预算与 L3 触发条件

| 层 | 预算 | 依据 |
| --- | --- | --- |
| L1 | 近零：不新增进程、不新增依赖；扫描上界沿用 200k/文件、400k/次、8 文件、5 范围 | `src/spoolkit/tools/edit.py:191`、`src/spoolkit/tools/verify.py:38`、`src/spoolkit/tools/verify.py:436` |
| L2.1 | 一次 pytest，受 `command_timeout` / `max_command_timeout` 约束 | `src/spoolkit/tools/exec.py:45`、`src/spoolkit/tools/exec.py:46` |
| L2.2 | 2 次子进程（改动前副本 + 改动后副本） | `src/spoolkit/tools/exec.py:536` |
| L2.3 | 近零（`ast` + help 文本） | — |
| L3 | 一次审查 ≈ 主循环一步的量级（督导自述「代价约等于主循环的一步」，`src/spoolkit/agents/supervisor.py:9`）；上界用 `max_tokens` 与超时双重约束（沿 `judge_review` 的 `max_tokens`，`src/spoolkit/agents/dispatcher.py:71`） | — |

参考量级（取自现有 17 条记录，不含新功能）：单次任务 `prompt_tokens` 14198–62462、`completion_tokens` 816–3505、墙钟 34–181 秒。L3 若每步都跑，成本将随步数线性放大，所以必须设触发条件。

**L3 触发条件（满足其一才跑，不是每步都跑）**：

1. L1 有任一发现（此时需要一个能读懂语义的裁判来判严重度）。
2. L2 出现回归（测试失败 / 对照运行不一致 / 接口变化）。
3. 改动规模越过阈值（文件数 ≥ N，默认与 `review_limit` 同量级，`src/spoolkit/limits.py:209`）。
4. 收尾时存在**没有证据的完成声明**（L4 降级项非空）。
5. 用户/调用方显式要求（`--review`）。

其余情况只跑 L1+L2，不调用模型。新增阈值全部走 `limits.py` 的 Knob 机制（如 `review_trigger_files`、`review_max_findings`、`review_max_tokens`），可覆盖、可标定。§3.2 的升级阈值同样走 Knob（`escalate_max_files`、`escalate_max_deleted`、`escalate_timeout_sec`），默认值先与 `review_limit` 同量级（`src/spoolkit/limits.py:209`），再按 §3.2.4 的指标标定——**阈值是「打扰预算」的旋钮，不是拍出来的数**。隔离模式（§3.2.5）另需一个开关 Knob（`--isolated` 与副本/分支的落点），默认关闭。

---

## 6. 分期与验收（可证伪）

前提：验收台就是探针集（`scripts/probe.py --all`，`benchmarks/probes/probes.json`）。每期的验收标准都写成「跑完 `--all` 后记录里应看到什么变化」。

**每期共通的一条硬验收（§2.5 的可证伪形式）**：**运行结束时若报告产出过改动，则盘上能看到且回退可用**——断言 `workspace_not_empty == true`、`changes_on_disk > 0`（只要本轮模型产出过改动）、`revert_available == true`。**口径提醒**：这条量的是**落盘 / 可回退 / 可列出**，与「删不删、删多少」无关；文件数量变少、文件被删、被合并、被拆分，都**不**构成本条不通过（§2.5 的反向条款）。**P0/P1/P2 每一期都要过这一条**，不因为「本期没做落盘改造」而豁免。**证伪方法（每期都跑）**：构造一个**高风险任务**（例如跨文件改名 + 删除公共导出，命中 T1/T7/T10），运行到结束，然后断言两件事——(a) 成果**已落在工作区**（`changes_on_disk > 0`：探针沙箱里直接读文件，或按 `changed_paths` 逐个查在不在、内容指纹对不对）；(b) 报告里那条回退命令**可执行**（在沙箱副本上真跑一次 `spool revert`，断言工作区回到 baseline 指纹）。任一条不成立即该期不合格。

### P0（小，当天可验）

范围：**只做 L1.1 盲区修补 + L1.4 测试改弱检测 + L4 最小账本**（把 L1 结果与「未验证声明」写进最终结论）。不动阻断语义，不接 L3。

可证伪验收：

1. `rename-cross-file` 那条新记录里 `stale_references >= 1`，且残留包含**本文件内**的那处（对齐 220652 那条 `CHECK-FAIL: 整个沙箱里没有 get_rows 了（残留：['pipeline\\fetch.py（1 处）']）`）。
2. 新增单测：把 `tests/test_merge.py` 的一条断言放宽 / 加 `xfail` / 往 `pyproject.toml` 的 `addopts` 里塞 `-k`，`tests_weakened > 0`；不改测试时为 0。
3. `false_completion` 字段出现在记录里；P0 不改阻断，所以 5 条假完成样本**仍然**是 5 条（这是「设计没越权」的对照）。
4. L1 误报：5 条探针上 `review_false_alarm == 0`。
5. 升级率与请示质量（P0 没有分级放行，所以这里是**反向判据**）：全部记录 `escalations == 0`、`blocked_actions == 0`，且不得出现 `escalation_approved == true`（出现即「P0 偷跑放行」）；若 P0 顺手加了请示渲染却没有触发条件，`escalation_precision` 必然无法回指 T1–T13，同样判不合格。
6. **本节开头的共通硬验收**（若产出过改动则盘上能看到 + 回退可用）：P0 只加了「读数」字段，**不允许**为了产生这些字段而改落盘行为——所以这条在 P0 上测的是**现状基线**：若 P0 跑出来的 `workspace_not_empty == false`，那是**现状的缺陷被记录下来了**（正是要修的东西），不许把判据改成「P0 可以豁免」。
7. P0 必须把这组读数**在同一批探针上留下一份基线**（`changes_on_disk` / `workspace_not_empty` / `revert_available` 的逐条值），P1 才能证明「落盘改造真的让成果不再被扣住」。基线不是新数字，就是 P0 那批记录的字段值。

### P1（L1 全量 + 阻断语义接线 + 风险分级放行）

范围：L1.2/L1.3/L1.5/L1.6 + §3.1 的阻断（`turn.done` 前一处 + **动作执行口前一处**，见 §2.6 第 2 条）+ **§2.5 的持续落盘口径与回退前置条件**（含 T13 的「给不出回退就不落盘」；**原先写的「无人值守不落盘」在本期被替换成这条**）+ **§3.2 风险分级放行**（默认路径 / 拦动作不拦成果 / 升级触发条件 T1–T13 / 请示契约 / 打扰预算 / 隔离模式 `--isolated`）+ 探针新增 `workspace_not_empty`、`changes_on_disk`、`revert_available`、`blocked_actions`、`blocked_by_review`、`escalations` 等字段。

**注意本期验收口径与上一版的差别**：上一版有一条「被阻断的运行 `settled != "auto"`（改动没进工作区）」——**这条已被删除并反向**。`settled != "auto"` 现在**不是**「成果没落盘」的证据，而是「有动作没执行 / 有动作等批复」的证据；被阻断的运行**必须** `changes_on_disk > 0`（量的是产出在不在盘上，**不是**「删过东西就算缺陷」）。留着旧判据会把「成果已落盘」判成缺陷。

**为什么放在 P1**：分级放行与阻断语义是同一套机器上的两个出口——先把「不许静默放行」接通（P1 前半），再接「按风险决定拦哪个动作、要不要请示」（P1 后半），顺序反了就会用「请示」掩盖「静默放行」。**但「持续落盘」必须在「拦动作」之前接通**：先落盘、再拦动作，顺序反了就会出现「动作拦住了、成果还没写」的**空工作区——即成果被扣在系统内部**（这正是本文件上一版的缺陷；**不是一个「不许删」的判据**）。T1 需要的 `ast` 公开符号签名快照从 L2.3 提前到本期（L2.3 其余部分仍在 P2），T2/T4/T6 只要路径与既有判定函数，近零成本。

可证伪验收：

1. `--all` 后，原先 5 条假完成样本里，`rename-cross-file` 与 `merge-html` 的 `final_ok` 变为 `False`（被拦下），`false_completion`（`final_ok=True 且 check_ok=False`）从 5 条降到 ≤4 条。
2. `blocked_by_review >= 1`；被阻断的运行**仍然** `changes_on_disk > 0`（拦的是结论与危险动作，不是成果、更不是「不许删东西」），且那条「未落盘」只允许出现在 `blocked_actions` 里（具体动作没执行）——**不允许**出现「`blocked_by_review >= 1` 且 `changes_on_disk == 0`」。
3. `check_ok` 不允许下降（阻断不能把本来绿的判成红）：与 P0 的 `check_ok` 逐条比较，只允许持平或上升。
4. 误报：任何 `blocked_by_review >= 1 且 check_ok=True` 的运行都要在报告里单列（`blocked_but_ok`），第一个版本目标为 0；同时单列「T13 导致的未落盘」（给不出回退那一类，**成果没写出来**），**不许混进 `blocked_by_review` 的「拒绝」口径**——一个是「不敢写」，一个是「拦了动作」。
5. **升级要真的发生，也要真的不发生**：`rename-cross-file`（跨文件改名 + 公共符号，命中 T1/T7/T10）必须 `escalations >= 1`；不含升级触发条件的探针（如 `fix-bug`，`benchmarks/probes/probes.json:95`）必须 `escalations == 0`。两边错任一边都判不合格（该拦没拦 = 静默执行危险动作，不该拦乱拦 = 把审阅塞回用户）。`fix-bug` 是否命中 T7 的规模线要等实测——若命中，按 D9 调阈值，而不是把这条判据删掉。
6. **必须在动作执行前拦**：`escalated_before_action == escalations`；出现 `False` 即「先执行再请示」，直接判不合格。**（本字段由上一版的 `escalated_before_apply` 改名而来：旧口径是「升级必须发生在落盘之前」，那条与新设计的 §2.5 直接冲突——成果本来就要先落盘。新口径只要求拦在**动作执行**之前。）**
7. **请示质量**：所有升级记录 `escalation_summary_complete == true`（影响/结论/建议/选项/回退五件齐）；人为去掉五件里任一件（测试里改渲染器），该字段必须变 `false`——判据要能证伪。
8. **无人值守不静默放行、也不把成果扣住**：无人值守运行里被拦项的 `escalation_approved == "pending"`，**但** `changes_on_disk > 0`（成果已落盘）且该被拦动作在报告里被点名；**同时**同一条记录的 `settled != "auto"` 只用来表示「有动作没执行」，**禁止**再用它推断「成果没落盘」（旧判据在此作废）。被拦动作那条的验收判据允许是红，但要在报告里单列，不混进 `check_ok` 的下降。
9. 升级率与请示质量（打扰预算）：全量跑一遍后 `escalation_rate < 0.5`、`escalation_recall == 1.0`、`escalation_precision == 1.0`；任何一条升级回指不到 T1–T13 里的具名条件，即判「升级率超标」——这条是「审查没有变回人肉审阅」的唯一可证伪判据。
10. **§2.5 的共通硬验收（本期的主判据）**：高风险任务（跨文件改名 + 删除公共导出，命中 T1/T7/T10）跑完后 `workspace_not_empty == true`、`changes_on_disk > 0`、`revert_available == true`；并把报告里给的那条回退命令**在沙箱里真跑一次**，断言工作区回到 baseline 指纹（`src/spoolkit/tools/edit.py:1593` 的 `revert`）。人为让 Baseline 写入失败（测试里把 `save_baseline` 的落点改成不可写，`src/spoolkit/tools/edit.py:1582`），则必须观察到 `revert_available == false` **且**这处改动**没有落盘**（T13 生效）+ 报告里点名——这条是「给不出回退就不落盘」的可证伪形式（**「不落盘」= 成果没写出来，与删不删无关**）。
11. **隔离模式**：带 `--isolated` 跑同一个高风险任务，主线工作区 `changes_on_disk == 0`（零改动），而**独立副本**里 `changes_on_disk > 0`、跑完能给出 diff 与合并/丢弃选项；与不带开关的同一探针**恰好相反**（一个写主线、一个写副本）。两边任一为假即判不合格。

### P2（L2 + L3）

范围：L2.2/L2.3/L2.4 + L3 发现清单与事实核对 + L3 触发条件。

可证伪验收：

1. `vague-optimize`：`review_findings` 里出现对照运行的结论；人为把 `logstat/report.py` 改成结果错一处的版本，`L2.2` 必须报不一致（对照实验要能证伪）。
2. `fix-bug` 探针：`tests_weakened == 0`（guard_hashes 已钉住 `tests/test_merge.py`，`benchmarks/probes/probes.json:105`），而人为放宽断言时必须 >0。
3. `review_l3_ran` 只在触发条件满足时 >0；全量跑一遍后 `review_l3_ran` 的**总次数显著小于** `verifier_ran` 的总次数（触发条件真的在省）。
4. L3 每条 finding 都能在 `ReviewFacts` 里找到支撑；`review_l3_unverified` 记录被剔除的条数。
5. `false_completion == 0`。
6. 升级率与请示质量（L3 高严重度是升级源之一，所以本期判据要靠 L3 真跑起来）：`review_l3_findings` 里 `severity=high` 的每条要么对应 `blocked_actions >= 1`（**结论本身是一个危险动作，例如「建议删除这批文件」**），要么被显式记成「只影响结论、不影响成果落盘」——**L3 发现不构成「成果不许落盘」的理由**（§2.5），**也不构成「不许删文件」的理由**。两种情形都必须 `escalation_summary_complete == true`；`escalation_approved == "pending"` 的运行**成果仍已落盘**（`changes_on_disk > 0`），只是被点的那个动作没执行。人为造一条高严重度 finding 却不让它升级（改触发判定），该条必须变红。
7. **§2.5 的共通硬验收（L3 是最容易「拦成果」的一层，所以本期要再跑一遍）**：L3 报出高严重度 finding 时，`changes_on_disk > 0`、`revert_available == true`、报告里逐条列出改动与回退命令；断言它**不**等于「L3 不过就不落盘」（上一版的读法）。人为把 L3 判定改成「高严重度即不落盘」，该条必须变红。
8. **全期收口**：P2 结束时，三条 `settled != "auto"` 的运行（如有）都必须满足「有动作没执行 + 成果已落盘 + 回退可用」；任何一条出现 `workspace_not_empty == false`（在本轮报告产出过改动的前提下），P0/P1/P2 的验收一起判不合格（§2.5 是不变量，不分期豁免；它量的是落盘/回退/可列出，**不是文件数量的增减**）。

### 探针记录要新增的字段（`scripts/probe.py:710` 的组装点 + `REQUIRED_RECORD_KEYS`，`scripts/probe.py:60`）

| 字段 | 含义 | 来源 |
| --- | --- | --- |
| `review_findings` | 发现条数（或按严重度分档的对象） | 事件流里审查事件 |
| `false_completion` | `final_ok=True 且 check_ok=False` 的 0/1 | 记录内派生 |
| `tests_weakened` | 测试被改弱的条数（删用例/skip/xfail/放宽断言/改 pytest 配置） | L1.4 事件 |
| `stale_references` | 全仓残留引用处数（含本文件） | L1.1 事件 |
| **`workspace_not_empty`** | **判据：运行结束时若报告产出过改动（`changes_on_disk`/`changed_paths` 非空），则盘上能看到这些产出**（`true`/`false`）；§2.5 不变量的直接读数。**它不是「文件数量不得减少」，也不是「不许删文件」**——文件被删、被合并、被拆分后总数变少，只要本轮报告产出过改动且能在盘上/台账里看到，这个字段就该是 `true`（实现时不得写成「文件数 < 运行前文件数就报 false」） | 运行结束时读工作区（沙箱内实际查文件）+ 台账比对 |
| **`changes_on_disk`** | **已经落在磁盘上的改动路径数**（隔离模式下读**副本**，另用 `isolated_prefix` 标明落点）。**判据口径：这是「产出在盘上」的路径数，不是「文件被删了多少」**——**删除也算一条落到盘上的改动**；实现时不得写成「文件数没有增加就算 0」 | 落盘事件 + 工作区实读 |
| **`revert_available`** | **这一轮是否给得出一条可执行的整轮回退命令**（有 Baseline 且命令可执行）；`false` 时该轮成果不许落盘（T13）。**判的是「有没有回退」，不是「有没有删东西」** | Baseline 落盘读数 + 回退命令自检 |
| **`blocked_actions`** | **被拦下、等批复的危险动作数**（拦的是动作，不是成果，也不是「不许改动内容」）；细分建议按 T1–T13 分档 | §3.2.2 动作拦截出口事件 |
| `blocked_by_review` | 被判「不许宣称完成」的次数（**注意：不是「成果不许落盘」、更不是「不许删文件」**，这几件事在本设计里已分开） | 阻断出口事件 |
| `review_false_alarm` | 审查报失败但验收判据通过 | 派生 |
| `blocked_but_ok` | 被阻断但验收绿 | 派生 |
| `escalations` | 因风险触发而**主动请示**的次数（请示对象是动作 / 合并） | §3.2.2 升级出口事件 |
| `escalated_before_action` | 每次升级是否发生在**动作执行之前**（任一为 `False` 即缺陷；期望 `== escalations`）。**字段由旧版的 `escalated_before_apply` 改名**：旧口径「升级必须在落盘之前」与「成果先落盘」（§2.5）冲突，已废 | 升级事件与动作执行事件的先后核对 |
| `escalation_summary_complete` | 每次请示是否齐五件（影响/结论/建议/选项/回退） | 请示渲染器输出检查 |
| `escalation_approved` | 批复结果：`true`（批准该动作）/ `false`（撤销或要求改方案）/ `"partial"`（只批准其中**几处动作**）/ `"pending"`（挂起待批；**此时成果已落盘**） | 批复记录（`src/spoolkit/bridge/agent_runner.py:35` 的四档词表） |
| `review_l3_ran` / `review_l3_findings` / `review_l3_unverified` | L3 是否跑、发现数、被事实核对剔除的条数 | L3 事件 |
| `evidence_gaps` | 收尾时没有证据的完成声明条数 | L4 账本 |
| **`isolated`**（配合 `--isolated`） | 本轮是否跑在独立副本/分支里；为真时另记副本落点与「合并 / 丢弃」选项 | 隔离模式运行时事件（§3.2.5） |

配套：`evaluate_bounds` 要加对应的 `max_*` 键（现在只有 3 组映射，`scripts/probe.py:795`），`probes.json` 的 `lower_bounds` 里补上可证伪的判据话术（现在每个探针 3–4 条，`benchmarks/probes/probes.json:38`）。§3.2.4 的升级率指标同样要能被 `lower_bounds` 表达：`escalations`/`escalated_before_action` 走下限，`escalation_rate` 走上限（新增 `max_escalations`），`escalation_recall`/`escalation_precision` 按比值写进判据话术。**§2.5 的四个新字段走两条线**：`workspace_not_empty`/`revert_available` 为**布尔下限（必须为真）**，`changes_on_disk` 走**下限**（有产出时必须 > 0），`blocked_actions` 走**上限**（`max_blocked_actions`，与 `max_escalations` 同量级标定）——**「`changes_on_disk` 高 + `blocked_actions` 低」才是目标形态**。**这三条下限的语义都只关于「落盘 / 可回退 / 可列出」，与删不删无关**：任何一条判据的实现里都不得出现「文件数比运行前少即失败」这类比较（`workspace_not_empty` 尤其容易被后来人实现成「不许删文件」，§6 字段表已写明它的判据）。

---

## 7. 不做清单

1. **默认不做「让用户逐行/逐步审阅」**：不加 review UI、不加「请确认每一处改动」的交互、不把 diff 当交付物。**但保留按风险分级的主动请示与批复**（§3.2）：命中 T1–T13 的**动作 / 合并**主动提出请示并等待批复，提出时附带**影响、结论、建议、选项、回退**五件，且批复可以是「只批准其中几处」（粒度是动作）。出发点是用户不会看也不想看 diff——所以请示给的是五件摘要，不是 diff。**请示期间成果已经落盘**（§2.5、§3.2.0）：不做逐处审阅，不等于把改动扣着不给（**「扣着不给」指的是成果被扣在系统内部没写盘，不是「不许删除、不许精简」**）。
2. **默认不做常驻审查服务**：不新增守护进程、不做定时批处理；审查是运行内的一次动作。**但允许会话内按需挂起等待批复**（§3.2.3）：待批状态**不是常驻进程**——没有东西留在后台，跨结算保留的只是**还没执行的动作清单**与账本（**成果已经写进工作区，不在「待批」里**），人不在就挂在「待批」，批复后从挂起点续跑。
3. **默认不引入重型静态分析依赖**：不引入 mypy / ruff / semgrep / bandit / eslint；沿用「标准库 + 已有 node」（`src/spoolkit/tools/verify.py:422`）。**若将来证明必要（例如 T1/T10 这类「查不了」的缺口反复出现），作为显式开放决策再评估**（§8 D10），评估时必须写清代价：新增依赖与安装体积、CI 时长、误报面与噪声控制，以及它自己会触发 T2 升级。
4. **不让审查者获得写权限**：审查者永远不能改代码来掩盖问题（`src/spoolkit/agents/runtime.py:99`），也不能通过改代码让审查通过。
5. **不把 L3 做成每步都跑**：成本随步数线性放大，只按 §5 的触发条件跑。
6. **不引入 LLM 自由文本判定**：结论必须结构化（`src/spoolkit/agents/dispatcher.py:44` 的教训：自由文本只能靠关键词猜）。
7. **不给没有机械判据的东西硬造判据**：`.md` 写得「好不好」这类问题不判；宁可说「查不了」并给替代做法（`src/spoolkit/tools/verify.py:443`）。
8. **不动模型服务与共享环境**：不改 8080 上的服务、不起长驻进程、不碰别的 agent 工作区。

---

## 8. 开放决策清单

| # | 决策 | 选项 | 代价 | 建议 |
| --- | --- | --- | --- | --- |
| D1 | 阻断默认强度（**本次修订后：拦的是动作，不是成果**） | (a) 硬阻断该处改动**落盘** (b) 软降级：照落盘但结论标「未验证/有风险」 (c) **拦动作前置条件**：成果一律落盘，只有「执行危险动作」在动作前拦下（§3.2.0） | (a) 可能丢掉本来正确的改动（`src/spoolkit/cli/settle.py:74` 已经踩过「整批作废把对的也扔掉」），并且是**「成果被扣住不落盘」这一类状态**的来源（G9）；(b) 用户仍会拿到坏改动（本设计的出发点就是不能指望用户），且危险动作会被一起放行；(c) 需要 Baseline + 整轮回退作为**落盘前置条件**（给不出就不落，T13） | **(c) + 按处收窄**：成果落盘（§2.5）；L1/L2 高严重度按**处**处理（复用 `pending.drop`，`src/spoolkit/cli/settle.py:80`），且**丢的是那一处的落盘资格**——只有 T13（给不出回退）才真的不落；L3 高严重度先允许自动修一轮，修不掉只降级结论 |
| D2 | 无人值守时谁放行 | (a) 无人放行，不通过就报未完成 (b) 一个显式开关 `--allow-unverified` (c) 超时后自动放行 (d) **成果落盘 + 危险动作挂起为待批、批复后续跑**（§3.2.3） | (a) 可能白跑一趟长任务；(c) 等于没有阻断；(d) 要能跨结算保留**待批动作清单**（成果已落盘，不需要靠暂存保命） | **(a) + (b) + (d)**：默认不自动执行危险动作；命中升级条件的（§3.2.2）**先落盘、再挂起那个动作**，而不是把成果挂起来；唯一不落盘的情形是 T13（给不出回退）；(b) 只作为用户显式选择的例外，且结论里必须写明「未经机械验证」 |
| D3 | L3 触发条件 | (a) 每步 (b) 只在 L1 有发现 / L2 回归 / 改动规模超阈值 / 有未验证声明 / 显式要求 | (a) 成本×步数；(b) 可能漏掉「机械判据看不出来」的语义问题 | **(b)**，阈值走 `limits.py` 可覆盖 |
| D4 | 审查者复用还是新角色 | (a) 复用 `reviewer`（`src/spoolkit/agents/runtime.py:80`），换 findings schema (b) 新角色 | (a) 要改 schema 与解析；(b) 两套审查者会漂移 | **(a)**：一个角色、一套 schema、一处解析 |
| D5 | 对照运行的参照从哪来 | (a) 用 `Baseline.old_text` 复原「改动前」树 (b) 引入 `expected/` 目录（探针台那套） | (a) 只能比「自己前后」；(b) 要用户提供 golden，等于增加用户负担 | **(a)**；golden 属于探针台（`benchmarks/probes/expected/`），不进产品 |
| D6 | L1 接在哪 | (a) 并进现有 `make_verifier` 的同一个出口（`src/spoolkit/tools/verify.py:353`）(b) 新开一条审查通道 | (a) 出口更挤，但只有一份「验证结论」；(b) 两处结论冲突时无法仲裁 | **(a)**，`_merge_types` 的「只加不减」规则继续有效 |
| D7 | 测试改弱的判据强度 | (a) 只判「用例数/用例名集合变少」 (b) 再加「断言数变少」 (c) 再加「pytest 配置/`conftest.py`/测试数据指纹」 | (a)/(b) 会漏掉配置型作弊；(c) 会有误报（合法重构会改 `conftest.py`） | **(c) 但只报不减**：新增用例/新增 fixture 不报；删除与配置收紧要报，并给 `file:line` 让人一眼能判 |
| D8 | 误报阈值 | (a) 先不设阈值，只记录 (b) 直接设一个数就上线 | (a) 阻断可能被误报触发；(b) 没有依据的数字 | **(a)**：L1 目标 0 误报；L3 先用探针集标定再定阈值 |
| D9 | 升级触发阈值（T5/T7 的规模线、挂起超时后的重新请示间隔） | (a) 默认与 `review_limit` 同量级，再按 §3.2.4 标定 (b) 先设低值、多请示 (c) 不设阈值、全请示 | (a) 可能漏升级；(b) `escalation_rate` 直接越过 0.5 的失败线，等于把审阅塞回用户；(c) 同 (b)，且成本随步数放大 | **(a)**：阈值走 Knob，先记录后标定；升级率超标就回去改触发条件，不调用户耐心 |
| D10 | 是否引入重型静态分析（mypy/ruff/semgrep/bandit/eslint） | (a) 默认不引入，只留本决策待评估 (b) 现在就引入，补齐类型/安全判据 | (a) 部分类型与安全问题只能靠 L3 语义审查兜；(b) 新增依赖与安装体积、CI 时长、误报面，且违背「标准库 + 已有 node」的默认 | **(a)**（§7.3 的默认）；真要引入必须单独立项，写清 (b) 的代价与噪声控制，并接受它自己触发 T2 升级 |
| D11 | **隔离模式（`--isolated`，§3.2.5）怎么实现、默认开不开** | (a) 默认关闭，只在显式开关下用**独立工作副本**（复用 `copy_project`，`src/spoolkit/tools/trial.py:60`；可考虑 git worktree/独立分支）(b) 默认开启 (c) 不做 | (a) 副本要空间、要处理「副本里跑出来的绝对路径」与「合并时的一致性」，且**目前 `trial_workspace` 是用完即删**（`src/spoolkit/tools/trial.py:66`），要新增「成果留在副本里」这条路；具体形态（worktree / 分支 / 目录副本）本稿**不指定**，实现时定；(b) 每次运行都付复制成本，而默认路径本来就是零打扰（§3.2.1）；(c) 长任务与「宁可在干净副本里跑完再看」的调用方没有出口 | **(a)**：默认关闭；隔离模式下「高风险也可以先落盘」（不碰主线），跑完给 diff 与**合并 / 丢弃**选项，**合并**才是一次被请示的动作（§3.2.3） |
| D12 | **§2.5 这段不变量文字进模型可见上下文时怎么写**（提示词 / 拦截话术 / 督导证据，`src/spoolkit/context/templates.py` 等） | (a) **连同反向条款一起进**（保留「约束的是落盘与回退承诺，不是编辑禁令」+ 允许编辑清单），只做措辞打磨 (b) 只进「落盘/可回退/可列出」那半段，反向条款留在文档里 (c) 把不变量浓缩成一个短句（例如「不许把成果扣住」）嵌进现有提示 | (a) 提示词变长一点（可接受，这段是本设计唯一的硬不变量）；(b) 模型可见的只有义务、没有授权，会被读成「禁止删除/禁止精简」而束手束脚——这正是本次修订要修的误读，所以 (b) 在任何情况下都不可取；(c) 省字，但脱离反向条款后含义会漂移，必须逐字对照 §2.5 复核 | **(a) 为硬规矩、不是可选项**（§2.5 的「硬规矩（给未来）」）：**必须连同反向条款一起进，且不得改写为「禁止删除」类表述**；(c) 只有同时把反向条款的允许清单一起带上才可接受。本项是**文字纪律**，本稿**不编造**「提示里带了反向条款」的可证伪判据——要不要在实现期给它加一个文案自检（例如对 `context/templates.py` 里这段文字做关键词核对），留到实现时按 §4「不给没有机械判据的东西硬造判据」定 |

---

## 9. 我判断不该做的方向

1. **不要靠「把验证结论端给用户看」兜底**：17 条记录里 `final_ok` 全是 True；214107 rename 那次结论里明写「最终验证：未通过」，改动照样 auto 落盘并被接受。把结论给人看不是防线——升级请示端过去的也不是结论原文或 diff，而是 §3.2.3 的五件摘要（影响/结论/建议/选项/回退）；默认路径下用户什么都不用看（**但工作区里看得到东西**，§2.5：看得见 ≠ 要用户读结论）。
2. **不要做「模型自己审自己」**：没有独立上下文的自我审查等于附和（`src/spoolkit/agents/dispatcher.py:8` 已写明这条教训）。审查者必须独立上下文 + 只读工具 + 不看实现者自述。
3. **不要用自由文本审查结论驱动阻断**：解析不出来就得按未通过（`src/spoolkit/agents/dispatcher.py:96`），否则「看起来说了通过」会变成通过。
4. **不要为了覆盖面去验证没有判据的东西**：那只会制造噪声，而噪声的最终结果是用户学会忽略审查。
5. **不要把审查做成用户交互流程**（确认对话框、逐条 approve、审阅页面）——那是把成本推回给用户。**唯一的例外是 §3.2.2 的具名触发条件**：命中才请示一次，而且只给五件摘要、不给 diff，**且拦的是那个动作**；没命中一律自动放行（成果照样落盘）；判据缺失时只记录、不请示。
6. **不要用「批复前不落盘 / 挂起待批」防风险（本次被否决的写法，记在这里防止后来人再想一遍）**：它把**成果**当成风险的人质——升级条件一旦命中纯规模/纯语义项（T7/T8/T9/T10/T12），被扣住的是整轮文件编辑，用户在长任务或无人值守里回来发现**成果被扣在系统里没落盘**（G9）。**注意这不是「不许删除」类的规定，恰恰相反**，正确的做法是 §3.2.0 的「**拦动作、不拦成果**」：成果持续落盘（§2.5 硬不变量），危险动作在执行前拦下请示，**待批 = 某个动作在等批复，而成果已经在工作区里**。风险控制靠的是「可回退 + 可审计」（Baseline、整轮回退命令、账本），不是靠「什么都不写」。**更不要把 §2.5 那条不变量读成「不许删除 / 不许精简」**：它的反向条款明确允许删死代码、合并重复实现、删已被替代的文件，只要求落盘、可回退、收尾报告能逐条列出（§2.5、§8 D12）。

---

## 10. 取证边界（我没做什么）

- **没读** `D:\workSpace\agentWorkSpace\.env`，也没读本工作区根目录的 `.env`。
- **没动** `D:\workSpace\agentWorkSpace`，没起长驻进程，没碰 8080（`benchmarks/probes/probes.json:6` 里指向的那个 `llamacpp` 服务）。
- **没跑** `scripts/probe.py --all`（需要 8080 与长任务），也没跑 `pytest`。因此 §1.6 的数字全部取自 `benchmarks/probes/records/**` 与 `benchmarks/probes/results.jsonl`；§6 里「P0/P1/P2 后应看到的变化」是**预测**，不是实测。
- 没验证 `tests/**` 当前的通过情况；没跑任何会导致副作用的命令。
- 所有行号对应**当前未提交的工作树**（`git status` 有改动），不是某个 commit 的行号。若要写进长期文档，落代码时需重新对齐。
- 未取证到的：`context/budget.py`、`index/rank.py`、`memory/**` 与本设计的关系只做了必要处的引用，没有系统通读；`web/`（网页 GUI）侧的审查展示没有取证。
- **本轮修订（§3.2 风险分级放行）新增的取证边界**：§3.2.3 引用的三处批复通道只**读了入口代码**（终端 `src/spoolkit/cli/approval.py:22`、网页壳 `src/spoolkit/web/runner.py:390`、聊天通道 `src/spoolkit/bridge/agent_runner.py:228`），**没有实跑任何一条批复路径**——升级后的呈现形态、超时行为、跨结算保留暂存全部是 **[设计]**，不是实测；`escalations` / `escalation_summary_complete` / `escalation_approved` 三个字段**当前不存在**于 `scripts/probe.py` 与记录里，§6 写的是要加的东西（`escalated_before_apply` 已改名为 `escalated_before_action`，见 §6 字段表）。§3.2.4 的目标量级同样是 [设计] 目标，未跑探针标定。
- **再一轮修订（§2.5 工作区不变量 + §3.2.0 拦动作不拦成果 + §3.2.5 隔离模式）新增的取证边界**：§2.5 / §3.2.0 引用的落盘与丢弃分支**只读了代码**（`src/spoolkit/cli/settle.py:58`、`src/spoolkit/cli/settle.py:60`、`src/spoolkit/cli/settle.py:64`、`src/spoolkit/cli/settle.py:80`、`src/spoolkit/cli/settle.py:90`，以及 `PendingChanges` 的 `drop`/`discard`，`src/spoolkit/tools/edit.py:1507`、`src/spoolkit/tools/edit.py:1513`），**没有实跑任何一次「升级 → 落盘 / 不落盘」**；结论里「上一版会留下空工作区」是**读代码推出来的机制论断**，不是跑出来的实测。**§3.2.5 的 `--isolated` 是新设计，仓库里今天没有这个开关**（`grep -rn "worktree\|--isolated" src/` 无命中）；隔离能力今天只有「用完即删的验证副本」（`src/spoolkit/tools/trial.py:60`、`src/spoolkit/tools/trial.py:66`），**没有**「把成果留在副本里」这条路。§6 新增的 `workspace_not_empty` / `changes_on_disk` / `revert_available` / `blocked_actions` 四个字段同样**当前不存在**；`revert_available` 依赖的 `Baseline` / `save_baseline` / `revert` 与 `spool revert` 已存在（`src/spoolkit/tools/edit.py:1542`、`src/spoolkit/tools/edit.py:1582`、`src/spoolkit/tools/edit.py:1593`、`src/spoolkit/cli/commands/revert.py:10`），但**「落盘前置条件」这个判定点今天没有**（`settle` 不检查回退可用性）。§7 与 §9 里「Codex / Cursor 这类产品的普遍模式」是**按普遍做法表述的设计依据**，没有引用具体产品的界面细节，也没有逐条取证。
- **本次措辞修订（§2.5 约束对象明确化 + 反向条款 + 防误读口径）新增的取证边界**：本轮**只改本文件的文字**，没有改任何代码、没有跑任何命令、没有跑探针。§2.5 原文里那句「工作区不得出现『用户的东西不见了』」改写成「运行/审查层对用户的承诺：只要有产出就必须落盘、可回退、可列出」并补反向条款，**是文字层面的修订**，与 §2.5 原有的机制论断（落盘/回退/trial 副本）无关。**没有为「这段文字进模型提示时必须带反向条款」编造可证伪判据**（它是文字纪律，见 §8 D12）；`workspace_not_empty` 的新判据（「若报告产出过改动则盘上能看到」，**不是**「文件数不得减少」）同样是**[设计] 口径**，尚未实现、也未验证。本轮**未**核对 `src/spoolkit/context/templates.py` 里今天到底有没有这段文字（按任务约束未读代码），因此「这段文字会进模型可见提示」是**推断**，不是取证。
