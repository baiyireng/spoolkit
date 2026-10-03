# 自动代码审查层设计（Automatic Review Layer）

- 日期：2026-10-03
- 状态：**设计稿，未实现**。本文只做设计，不改任何代码。
- 出发点（用户原话）：实际开发中用户基本不会仔细审阅 AI 的代码，尤其高速迭代期；我们已经有「自动验证」，**应该围绕这一思路完善 agent 的自动化代码审查机制，而不是加强用户层面的审阅**。
- 阅读约定：每条现状论断都带 `文件:行`。标 **[已实现]** 的是仓库里今天就有的东西；标 **[缺口]** 的是今天不成立的；标 **[设计]** 的是本文新增、尚未写进代码的东西。所有行号对应**当前工作树**（`git status` 显示 `src/spoolkit/agent/loop.py`、`src/spoolkit/tools/edit.py`、`src/spoolkit/cli/approval.py`、`src/spoolkit/tools/view.py`、`src/spoolkit/tools/trial.py`、`src/spoolkit/context/templates.py` 等有未提交改动），不对应任何 commit。

---

## 0. 结论

现状是「**验证很勤，判决很软**」：自动验证每次改完都跑（`src/spoolkit/agent/loop.py:1548`），收尾也如实报最终验证结果（`src/spoolkit/agent/loop.py:1756`），但**验证结果对「能不能宣称完成」「能不能落盘」没有任何约束力**——模型说 done 就直接收尾（`src/spoolkit/agent/loop.py:1637`），落盘只按 policy/scope 判（`src/spoolkit/cli/settle.py:64`）。于是出现「验证报未通过、改动照样自动落盘、结论照样自称完成」这一整类事实。

本设计的做法：**把已经机械化的东西扩到「改动一致性」这一层（L1），给验证结果接上阻断语义，再用一个独立上下文、结构化输出、且每条断言都过机械核对的审查者补语义层（L3），最后用证据账本（L4）让「完成」这句话必须挂证据。**机械优先，模型其次；能判定就不问模型。反过来，影响面大的改动不靠「用户会自己看」兜底，而是按风险分级主动请示并等一次批复（§3.2）：默认零打扰，例外可审计、可回退。

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

### 分层与现有循环的接线点

1. L1/L2 挂在 `make_verifier` 之后、同一个 `ToolResult` 出口（`src/spoolkit/tools/verify.py:353` 的 `_merge_types` **只加不减**），不新开第二条验证通道。
2. 阻断判定放在 `turn.done` 之前（`src/spoolkit/agent/loop.py:1637`）与落盘之前（`src/spoolkit/cli/settle.py:64`）——两处都要有，否则「不宣称完成」也能落盘。
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
2. 预算用尽仍不通过 → **先按 §3.2.2 看有没有命中升级触发条件（T1–T12）**：命中就请示（人在）或挂起为待批（人不在），批复「批准」才落盘、批复「撤销」走拒绝分支；未命中则 `finished=False`，结论写成「未完成（被审查拦下）+ 每条发现 + file:line」，并**不落盘**（走 `settle` 的拒绝分支，`src/spoolkit/cli/settle.py:58`）。
3. 无人值守（`non_interactive`）时**不弹问题**：自动修 N 轮仍不通过的高严重度发现按 §3.2 升级——**挂起为待批、不落盘、可续跑**（不是静默放行，也不是一律拒绝）。越界那几处单独丢、范围内的照常处理（`src/spoolkit/cli/settle.py:80`），但范围内的**高严重度**发现同样不落。
4. 显式例外只能来自命令行开关（例如 `--allow-unverified`），默认关闭；这是开放决策，见 §9。

**谁有权放行**：**默认无人**——默认路径就是「机械判据全部通过 → 自动放行」，不打扰用户。**人工放行只有一条通道**：§3.2 的按风险升级拿到批复；没有命中触发条件的改动不请示、也不阻塞。`agent` 自己永远不能放行（它会自称完成，17/17 条记录都是 True）。第 4 条那种命令行开关只是用户**事前**显式选择的例外，不是审查放行。

### 3.2 风险分级放行（risk-tiered gate）

**[设计]** §3.1 只回答「不许静默放行」；这一节回答「那什么时候该问人」。两者是**同一套机器**：先不许静默放行，再按风险决定「自动放行」还是「请示等批复」。

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
| T11 | **不可回退** | 改动没有 Baseline 快照、给不出一条可执行的撤销命令（`src/spoolkit/tools/edit.py:1520`、`src/spoolkit/cli/commands/revert.py:10`） |
| T12 | **L1/L2 高严重度未处理** | 自动修 N 轮后（`step_ceiling`/`verdict.grants` 用尽，`src/spoolkit/agent/loop.py:1139`）阻断项仍在，且不属于 T1–T11 任一条 |

处置：**先请示、等批复，再决定落盘**（不先落盘再问）。批复「批准」才落盘并把批复记进账本；批复「撤销」走 §3.1 的拒绝分支（`src/spoolkit/cli/settle.py:58`）。自动修补仍在前面：先按 §3.1 的处理顺序自动修 N 轮，修不掉才升级——升级是「拿不准才问」，不是「一有风险就停」。

- **为什么这么定**：T1–T12 的共同点是「改坏了不便宜」——公共接口、依赖与部署、认证密钥、数据迁移、批量删除、越界路径，都是事后难回滚或会影响别人的改动，值得一次人工批复。
- **不这么定会怎样**：默认路径的「自动放行」落在这些改动上就是**静默承担不可逆风险**；反过来，如果只是把这些改动一律硬拦、不给人一次放行的机会（§7 原措辞的极端读法），在极端情形下等于白跑一趟长任务——用户既没机会放行，也拿不到产物。

#### 3.2.3 请示契约：一次说清五件

**每次升级必须一次给全五件**（缺任一即视为无效请示，记录里判不合格）：

| # | 要件 | 内容与证据要求 |
| --- | --- | --- |
| ① | **影响** | 哪些文件 / 哪些公共接口 / 哪条流程 / 谁会受影响（有上下游就写出来） |
| ② | **结论** | 自动审查与验证的结果 + 证据（`file:line`、命令与退出码、测试输出摘要）——**只给结论与证据，不贴 diff** |
| ③ | **建议** | 推荐怎么做（明确「建议批准 / 建议改方案 / 建议撤销」），有替代方案就并列 |
| ④ | **选项** | 批准 / 只批准其中几处 / 撤销 / 改方案（四档，与现有四档批复词表对齐，`src/spoolkit/bridge/agent_runner.py:35`） |
| ⑤ | **回退保证** | 一条**可直接执行**的撤销命令（`spool revert`，`src/spoolkit/cli/commands/revert.py:10`）；给不出就按 T11 明说「没有回退」 |

**呈现方式**（要短、可复制、不必读 diff）：

| 通道 | 落点 | 形态 |
| --- | --- | --- |
| 终端 | `settle()` 的确认路径（`src/spoolkit/cli/settle.py:94`）+ `ask` 注入（`src/spoolkit/cli/approval.py:22`） | 五件各一行 + 一条撤销命令；默认答案是「不落盘」（与现有 `[y/N]` 默认 N 一致）；输出必须走 `out`，否则 `--events` 的 JSON 行会被污染（`src/spoolkit/cli/approval.py:12` 记着这次事故） |
| 网页壳 | 已经在跑的等待通道 `awaiting` / `awaiting_detail`（`src/spoolkit/web/runner.py:448`）与确认卡片（`src/spoolkit/web/page.py:2516`） | 五件塞进 `awaiting_detail` 的短字段；批复走 `Runner.confirm`（`src/spoolkit/web/runner.py:390`）与 `Runner.answer`（`src/spoolkit/web/runner.py:394`） |
| 聊天通道 | `bridge` 的待确认问答（`src/spoolkit/bridge/agent_runner.py:212`、`src/spoolkit/bridge/agent_runner.py:228`） | 一条消息给五件 + 撤销命令，按四档词批复（`src/spoolkit/bridge/agent_runner.py:35`） |
| MCP / 任务状态 | `status.awaiting > 0` 已经是「需要审批」的信号（`src/spoolkit/mcp/server.py:440`） | 复用同一信号，不新增通道 |

**超时与无人值守行为**（不落盘、挂起为待批、可续跑）：

- 有人在（有 TTY / 网页壳 / 聊天通道在线）：给五件，等批复；等不到批复就不落盘。
- 无人值守（`non_interactive`，`src/spoolkit/cli/settle.py:88`）：**不弹问题、绝不静默放行、也不一律拒绝**——升级项**挂起为待批**（不落盘、不动工作区），运行以「待批复」结束；暂存跨结算保留，批复后从挂起点续跑（`src/spoolkit/cli/settle.py:113` 的 `staged_items` 读数，`src/spoolkit/tools/edit.py:1331`）。同一次运行里的非升级项照现有语义处理（越界处单独丢，`src/spoolkit/cli/settle.py:80`），升级项不跟着一起落。
- 超时只有一个含义：**挂起多久之后可以再次请示**。过期**不自动放行**（超时放行等于没有阻断），也**不当拒绝**（当拒绝会把「可续跑」变成「重跑」）。

**批复与审计**（谁批的、依据什么、记在哪）：

- **谁批的**：批复来源三通道之一（终端 / 网页壳 / 聊天通道）连同批复词原样记录；`agent` 自己无权批复。
- **依据什么**：记触发条件 ID（T1–T12）+ 五件摘要的内容指纹（复用 `_content_fingerprint`，`src/spoolkit/agent/loop.py:207`）。指纹变了说明批复针对的不是眼前这版改动，批复作废、重新请示。
- **记在哪**：事件流（沿用 `note` 事件，`src/spoolkit/agent/loop.py:1912`）+ L4 证据账本 + 探针记录字段（§6）；判错的升级写进 lessons（`src/spoolkit/agent/loop.py:426`）。

#### 3.2.4 打扰预算（升级率必须有度量）

升级本身是**新的打扰**：升级率过高等于把审阅又塞回给用户，本设计的初衷就被推翻。要记的指标（落到 §6 的探针字段）：

| 指标 | 定义 | 目标量级 |
| --- | --- | --- |
| `escalation_rate` | 升级次数 / 运行次数 | **< 1 次/运行**；≥ 0.5 视为设计失败，回去改触发条件 |
| `escalation_recall` | 命中 T1–T12 的改动里被升级的比例 | **100%**（漏升级 = 静默放行） |
| `escalation_precision` | 被升级的改动里，事后被探针判据/人工核对确认为真高风险的比例 | **100%**（每条都能回指一个具名触发条件；出现「无触发条件的升级」即不合格） |
| `escalation_by_trigger` | 按 T1–T12 分档的次数 | 找出哪个条件在刷屏，去调那个阈值，而不是调用户的耐心 |
| `escalation_wait` / `escalation_abandoned` | 人等了多久才批复 / 挂起后再没被批复的次数 | 只记录；`abandoned` 高说明提示位置不对 |
| `escalation_false_alarm` | 升了级、但验收判据全绿（同 §4 的 `review_false_alarm` 口径） | 记录 + 报告单列；被这类刷屏就调阈值 |

以上全部是 **[设计] 目标**，不是实测：探针集跑完之前不设具体阈值，先记录、再按 §8 D9 标定。

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
- 噪声控制：L1 通过就沉默；L2 只在触发条件满足时跑；L3 一次审查的发现条数设上限（超出说明改动太大，应当拆）。**升级本身也是噪声**：按 §3.2.4 度量升级率，指标超标就回去改触发条件，而不是让用户习惯点「批准」。

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

其余情况只跑 L1+L2，不调用模型。新增阈值全部走 `limits.py` 的 Knob 机制（如 `review_trigger_files`、`review_max_findings`、`review_max_tokens`），可覆盖、可标定。§3.2 的升级阈值同样走 Knob（`escalate_max_files`、`escalate_max_deleted`、`escalate_timeout_sec`），默认值先与 `review_limit` 同量级（`src/spoolkit/limits.py:209`），再按 §3.2.4 的指标标定——**阈值是「打扰预算」的旋钮，不是拍出来的数**。

---

## 6. 分期与验收（可证伪）

前提：验收台就是探针集（`scripts/probe.py --all`，`benchmarks/probes/probes.json`）。每期的验收标准都写成「跑完 `--all` 后记录里应看到什么变化」。

### P0（小，当天可验）

范围：**只做 L1.1 盲区修补 + L1.4 测试改弱检测 + L4 最小账本**（把 L1 结果与「未验证声明」写进最终结论）。不动阻断语义，不接 L3。

可证伪验收：

1. `rename-cross-file` 那条新记录里 `stale_references >= 1`，且残留包含**本文件内**的那处（对齐 220652 那条 `CHECK-FAIL: 整个沙箱里没有 get_rows 了（残留：['pipeline\\fetch.py（1 处）']）`）。
2. 新增单测：把 `tests/test_merge.py` 的一条断言放宽 / 加 `xfail` / 往 `pyproject.toml` 的 `addopts` 里塞 `-k`，`tests_weakened > 0`；不改测试时为 0。
3. `false_completion` 字段出现在记录里；P0 不改阻断，所以 5 条假完成样本**仍然**是 5 条（这是「设计没越权」的对照）。
4. L1 误报：5 条探针上 `review_false_alarm == 0`。
5. 升级率与请示质量（P0 没有分级放行，所以这里是**反向判据**）：全部记录 `escalations == 0`，且不得出现 `escalation_approved == true`（出现即「P0 偷跑放行」）；若 P0 顺手加了请示渲染却没有触发条件，`escalation_precision` 必然无法回指 T1–T12，同样判不合格。

### P1（L1 全量 + 阻断语义接线 + 风险分级放行）

范围：L1.2/L1.3/L1.5/L1.6 + §3.1 的阻断（`turn.done` 前 + 落盘前各一处）+ 无人值守不落盘 + **§3.2 风险分级放行**（默认路径 / 升级触发条件 T1–T12 / 请示契约 / 打扰预算）+ 探针新增 `blocked_by_review`、`escalations` 等字段。**为什么放在 P1**：分级放行与阻断语义是同一套机器上的两个出口——先把「不许静默放行」接通（P1 前半），再接「按风险决定自动放行还是请示」（P1 后半），顺序反了就会用「请示」掩盖「静默放行」。T1 需要的 `ast` 公开符号签名快照从 L2.3 提前到本期（L2.3 其余部分仍在 P2），T2/T4/T6 只要路径与既有判定函数，近零成本。

可证伪验收：

1. `--all` 后，原先 5 条假完成样本里，`rename-cross-file` 与 `merge-html` 的 `final_ok` 变为 `False`（被拦下），`false_completion`（`final_ok=True 且 check_ok=False`）从 5 条降到 ≤4 条。
2. `blocked_by_review >= 1`，且被阻断的运行 `settled != "auto"`（改动没进工作区）。
3. `check_ok` 不允许下降（阻断不能把本来绿的判成红）：与 P0 的 `check_ok` 逐条比较，只允许持平或上升。
4. 误报：任何 `blocked_by_review >= 1 且 check_ok=True` 的运行都要在报告里单列（`blocked_but_ok`），第一个版本目标为 0。
5. **升级要真的发生，也要真的不发生**：`rename-cross-file`（跨文件改名 + 公共符号，命中 T1/T7/T10）必须 `escalations >= 1`；不含升级触发条件的探针（如 `fix-bug`，`benchmarks/probes/probes.json:95`）必须 `escalations == 0`。两边错任一边都判不合格（该升没升 = 静默放行，不该升乱升 = 把审阅塞回用户）。`fix-bug` 是否命中 T7 的规模线要等实测——若命中，按 D9 调阈值，而不是把这条判据删掉。
6. **必须在落盘前升级**：`escalated_before_apply == escalations`；出现 `False` 即「先落盘再请示」，直接判不合格。
7. **请示质量**：所有升级记录 `escalation_summary_complete == true`（影响/结论/建议/选项/回退五件齐）；人为去掉五件里任一件（测试里改渲染器），该字段必须变 `false`——判据要能证伪。
8. **无人值守不静默放行**：无人值守运行里升级项的 `escalation_approved == "pending"`，且这些运行的 `settled != "auto"`；被挂起那条的验收判据允许是红（挂起 = 不落盘），但要在报告里单列，不混进 `check_ok` 的下降。
9. 升级率与请示质量（打扰预算）：全量跑一遍后 `escalation_rate < 0.5`、`escalation_recall == 1.0`、`escalation_precision == 1.0`；任何一条升级回指不到 T1–T12 里的具名条件，即判「升级率超标」——这条是「审查没有变回人肉审阅」的唯一可证伪判据。

### P2（L2 + L3）

范围：L2.2/L2.3/L2.4 + L3 发现清单与事实核对 + L3 触发条件。

可证伪验收：

1. `vague-optimize`：`review_findings` 里出现对照运行的结论；人为把 `logstat/report.py` 改成结果错一处的版本，`L2.2` 必须报不一致（对照实验要能证伪）。
2. `fix-bug` 探针：`tests_weakened == 0`（guard_hashes 已钉住 `tests/test_merge.py`，`benchmarks/probes/probes.json:105`），而人为放宽断言时必须 >0。
3. `review_l3_ran` 只在触发条件满足时 >0；全量跑一遍后 `review_l3_ran` 的**总次数显著小于** `verifier_ran` 的总次数（触发条件真的在省）。
4. L3 每条 finding 都能在 `ReviewFacts` 里找到支撑；`review_l3_unverified` 记录被剔除的条数。
5. `false_completion == 0`。
6. 升级率与请示质量（L3 高严重度是升级源之一，所以本期判据要靠 L3 真跑起来）：`review_l3_findings` 里 `severity=high` 的每条都必须对应 `escalations >= 1`，且 `escalation_summary_complete == true`；`escalation_approved == "pending"` 的运行 `settled != "auto"`。人为造一条高严重度 finding 却不让它升级（改触发判定），该条必须变红。

### 探针记录要新增的字段（`scripts/probe.py:710` 的组装点 + `REQUIRED_RECORD_KEYS`，`scripts/probe.py:60`）

| 字段 | 含义 | 来源 |
| --- | --- | --- |
| `review_findings` | 发现条数（或按严重度分档的对象） | 事件流里审查事件 |
| `false_completion` | `final_ok=True 且 check_ok=False` 的 0/1 | 记录内派生 |
| `tests_weakened` | 测试被改弱的条数（删用例/skip/xfail/放宽断言/改 pytest 配置） | L1.4 事件 |
| `stale_references` | 全仓残留引用处数（含本文件） | L1.1 事件 |
| `blocked_by_review` | 被判「不许宣称完成」的次数 | 阻断出口事件 |
| `review_false_alarm` | 审查报失败但验收判据通过 | 派生 |
| `blocked_but_ok` | 被阻断但验收绿 | 派生 |
| `escalations` | 因风险触发而**主动请示**的次数 | §3.2.2 升级出口事件 |
| `escalated_before_apply` | 每次升级是否发生在落盘**之前**（任一为 `False` 即缺陷；期望 `== escalations`） | 升级事件与落盘事件的先后核对 |
| `escalation_summary_complete` | 每次请示是否齐五件（影响/结论/建议/选项/回退） | 请示渲染器输出检查 |
| `escalation_approved` | 批复结果：`true`（批准）/ `false`（撤销或要求改方案）/ `"partial"`（只批准其中几处）/ `"pending"`（挂起待批） | 批复记录（`src/spoolkit/bridge/agent_runner.py:35` 的四档词表） |
| `review_l3_ran` / `review_l3_findings` / `review_l3_unverified` | L3 是否跑、发现数、被事实核对剔除的条数 | L3 事件 |
| `evidence_gaps` | 收尾时没有证据的完成声明条数 | L4 账本 |

配套：`evaluate_bounds` 要加对应的 `max_*` 键（现在只有 3 组映射，`scripts/probe.py:795`），`probes.json` 的 `lower_bounds` 里补上可证伪的判据话术（现在每个探针 3–4 条，`benchmarks/probes/probes.json:38`）。§3.2.4 的升级率指标同样要能被 `lower_bounds` 表达：`escalations`/`escalated_before_apply` 走下限，`escalation_rate` 走上限（新增 `max_escalations`），`escalation_recall`/`escalation_precision` 按比值写进判据话术。

---

## 7. 不做清单

1. **默认不做「让用户逐行/逐步审阅」**：不加 review UI、不加「请确认每一处改动」的交互、不把 diff 当交付物。**但保留按风险分级的主动请示与批复**（§3.2）：命中 T1–T12 的改动主动提出审阅并等待批复，提出时附带**影响、结论、建议、选项、回退**五件，且批复可以是「只批准其中几处」。出发点是用户不会看也不想看 diff——所以请示给的是五件摘要，不是 diff。
2. **默认不做常驻审查服务**：不新增守护进程、不做定时批处理；审查是运行内的一次动作。**但允许会话内按需挂起等待批复**（§3.2.3）：待批状态**不是常驻进程**——没有东西留在后台，暂存与账本落盘，人不在就挂在「待批」，批复后从挂起点续跑。
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
| D1 | 阻断默认强度 | (a) 硬阻断：该处改动不落盘 (b) 软降级：照落盘但结论标「未验证/有风险」 | (a) 可能丢掉本来正确的改动（`src/spoolkit/cli/settle.py:74` 已经踩过「整批作废把对的也扔掉」）；(b) 用户仍会拿到坏改动（本设计的出发点就是不能指望用户） | **分层**：L1/L2 高严重度按**处**硬阻断（只丢那一处，复用 `pending.drop`，`src/spoolkit/cli/settle.py:80`），范围内的其它改动照落；L3 高严重度先允许自动修一轮 |
| D2 | 无人值守时谁放行 | (a) 无人放行，不通过就报未完成 (b) 一个显式开关 `--allow-unverified` (c) 超时后自动放行 (d) 挂起为待批、批复后续跑（§3.2.3） | (a) 可能白跑一趟长任务；(c) 等于没有阻断；(d) 要能跨结算保留暂存 | **(a) + (b) + (d)**：默认无人放行；命中升级条件的（§3.2.2）挂起为待批而不是拒绝；(b) 只作为用户显式选择的例外，且结论里必须写明「未经机械验证」 |
| D3 | L3 触发条件 | (a) 每步 (b) 只在 L1 有发现 / L2 回归 / 改动规模超阈值 / 有未验证声明 / 显式要求 | (a) 成本×步数；(b) 可能漏掉「机械判据看不出来」的语义问题 | **(b)**，阈值走 `limits.py` 可覆盖 |
| D4 | 审查者复用还是新角色 | (a) 复用 `reviewer`（`src/spoolkit/agents/runtime.py:80`），换 findings schema (b) 新角色 | (a) 要改 schema 与解析；(b) 两套审查者会漂移 | **(a)**：一个角色、一套 schema、一处解析 |
| D5 | 对照运行的参照从哪来 | (a) 用 `Baseline.old_text` 复原「改动前」树 (b) 引入 `expected/` 目录（探针台那套） | (a) 只能比「自己前后」；(b) 要用户提供 golden，等于增加用户负担 | **(a)**；golden 属于探针台（`benchmarks/probes/expected/`），不进产品 |
| D6 | L1 接在哪 | (a) 并进现有 `make_verifier` 的同一个出口（`src/spoolkit/tools/verify.py:353`）(b) 新开一条审查通道 | (a) 出口更挤，但只有一份「验证结论」；(b) 两处结论冲突时无法仲裁 | **(a)**，`_merge_types` 的「只加不减」规则继续有效 |
| D7 | 测试改弱的判据强度 | (a) 只判「用例数/用例名集合变少」 (b) 再加「断言数变少」 (c) 再加「pytest 配置/`conftest.py`/测试数据指纹」 | (a)/(b) 会漏掉配置型作弊；(c) 会有误报（合法重构会改 `conftest.py`） | **(c) 但只报不减**：新增用例/新增 fixture 不报；删除与配置收紧要报，并给 `file:line` 让人一眼能判 |
| D8 | 误报阈值 | (a) 先不设阈值，只记录 (b) 直接设一个数就上线 | (a) 阻断可能被误报触发；(b) 没有依据的数字 | **(a)**：L1 目标 0 误报；L3 先用探针集标定再定阈值 |
| D9 | 升级触发阈值（T5/T7 的规模线、挂起超时后的重新请示间隔） | (a) 默认与 `review_limit` 同量级，再按 §3.2.4 标定 (b) 先设低值、多请示 (c) 不设阈值、全请示 | (a) 可能漏升级；(b) `escalation_rate` 直接越过 0.5 的失败线，等于把审阅塞回用户；(c) 同 (b)，且成本随步数放大 | **(a)**：阈值走 Knob，先记录后标定；升级率超标就回去改触发条件，不调用户耐心 |
| D10 | 是否引入重型静态分析（mypy/ruff/semgrep/bandit/eslint） | (a) 默认不引入，只留本决策待评估 (b) 现在就引入，补齐类型/安全判据 | (a) 部分类型与安全问题只能靠 L3 语义审查兜；(b) 新增依赖与安装体积、CI 时长、误报面，且违背「标准库 + 已有 node」的默认 | **(a)**（§7.3 的默认）；真要引入必须单独立项，写清 (b) 的代价与噪声控制，并接受它自己触发 T2 升级 |

---

## 9. 我判断不该做的方向

1. **不要靠「把验证结论端给用户看」兜底**：17 条记录里 `final_ok` 全是 True；214107 rename 那次结论里明写「最终验证：未通过」，改动照样 auto 落盘并被接受。把结论给人看不是防线——升级请示端过去的也不是结论原文或 diff，而是 §3.2.3 的五件摘要（影响/结论/建议/选项/回退）；默认路径下用户什么都不用看。
2. **不要做「模型自己审自己」**：没有独立上下文的自我审查等于附和（`src/spoolkit/agents/dispatcher.py:8` 已写明这条教训）。审查者必须独立上下文 + 只读工具 + 不看实现者自述。
3. **不要用自由文本审查结论驱动阻断**：解析不出来就得按未通过（`src/spoolkit/agents/dispatcher.py:96`），否则「看起来说了通过」会变成通过。
4. **不要为了覆盖面去验证没有判据的东西**：那只会制造噪声，而噪声的最终结果是用户学会忽略审查。
5. **不要把审查做成用户交互流程**（确认对话框、逐条 approve、审阅页面）——那是把成本推回给用户。**唯一的例外是 §3.2.2 的具名触发条件**：命中才请示一次，而且只给五件摘要、不给 diff；没命中一律自动放行；判据缺失时只记录、不请示。

---

## 10. 取证边界（我没做什么）

- **没读** `D:\workSpace\agentWorkSpace\.env`，也没读本工作区根目录的 `.env`。
- **没动** `D:\workSpace\agentWorkSpace`，没起长驻进程，没碰 8080（`benchmarks/probes/probes.json:6` 里指向的那个 `llamacpp` 服务）。
- **没跑** `scripts/probe.py --all`（需要 8080 与长任务），也没跑 `pytest`。因此 §1.6 的数字全部取自 `benchmarks/probes/records/**` 与 `benchmarks/probes/results.jsonl`；§6 里「P0/P1/P2 后应看到的变化」是**预测**，不是实测。
- 没验证 `tests/**` 当前的通过情况；没跑任何会导致副作用的命令。
- 所有行号对应**当前未提交的工作树**（`git status` 有改动），不是某个 commit 的行号。若要写进长期文档，落代码时需重新对齐。
- 未取证到的：`context/budget.py`、`index/rank.py`、`memory/**` 与本设计的关系只做了必要处的引用，没有系统通读；`web/`（网页 GUI）侧的审查展示没有取证。
- **本轮修订（§3.2 风险分级放行）新增的取证边界**：§3.2.3 引用的三处批复通道只**读了入口代码**（终端 `src/spoolkit/cli/approval.py:22`、网页壳 `src/spoolkit/web/runner.py:390`、聊天通道 `src/spoolkit/bridge/agent_runner.py:228`），**没有实跑任何一条批复路径**——升级后的呈现形态、超时行为、跨结算保留暂存全部是 **[设计]**，不是实测；`escalations` / `escalated_before_apply` / `escalation_summary_complete` / `escalation_approved` 四个字段**当前不存在**于 `scripts/probe.py` 与记录里，§6 写的是要加的东西。§3.2.4 的目标量级同样是 [设计] 目标，未跑探针标定。
