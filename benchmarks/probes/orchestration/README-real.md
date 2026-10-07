# 编排工具（`orchestrate`）的反事实对照 —— **真模型**轮（第二轮）

**这是什么**：把上一轮（`counterfactual.py` + `readings.json`，脚本化**假** provider）
换成**真调本机 llama.cpp**（`http://127.0.0.1:8080`，Qwen3-27B-IQ2_S，`n_ctx=8192`，
`--reasoning off`）再做一次反事实对照：`orchestrate` 到底有没有被用、写了什么、
用了之后省不省。**不是**在原脚本上改数字——原脚本与它的读数原样留着。

| 文件 | 是什么 |
| --- | --- |
| `real_counterfactual.py` | 本轮的跑分器（三臂；真 provider） |
| `real-readings.json` | **36 条逐次读数**（全量，不取最好那次）+ 中位数/极差汇总 |
| `real-run-log.txt` | 跑批日志（含 502 那一段与补偿跑） |
| `real-table.txt` | 由 `render_table.py` 生成的对照表 |
| `real-sandbox-pytest.txt` | 沙箱里那个失败测试的**红**原文 |
| `real-done.json` | 已经落进读数的格子（补偿跑时据此不重复买） |
| `.real-state.json` | 沙箱路径与失败测试的退出码 |
| `real-readings.round1-middle.json` / `real-run-log.round1-middle.txt` | **中途那批**（跑批途中 HEAD 变了）留档，不进本轮结论 |
| `inspect_readings.py` / `render_table.py` / `summary_numbers.py` / `scene_check.py` | 只读的检视与渲染工具 |
| `diag_tools.py` / `diag_tokenize.py` | 两个诊断（工具返回值、`/tokenize` 502 的触发点） |
| `probe_diagnostics.txt` | 上面两个诊断的输出留档 |

## 三臂（唯一自变量是「注册表里有没有那一件工具」）

- `off`：**一次一调**。`orchestrate` **不注册**——装配完之后从注册表里摘掉
  （禁用开关，不是提示词里求它别用）。它因此既不在工具索引里，也不在 GBNF 的
  工具名 enum 里。
- `on`：**一步多调**。只是把 `orchestrate` 留着（`assemble_loop` 本来就会注册）。
  任务描述与提示词**一个字都不加**。
- `control`：**安慰剂臂**（本轮新加，用来消歧）。摘掉 `orchestrate`，塞一件名字与
  用途都与编排无关的占位工具 `noop_tool`，但**索引里那一行的长度逐字相同**
  （`orchestrate(plan) 一步内批量执行多个工具调用` ↔ `noop_tool(plan) 占位工具，不做任何事·····`，
  都是 31 字）。于是它与 `on` 臂在「工具件数 / system 区段大小」上对齐，只差那个名字。

为什么非加这一臂不可：真模型下同一个臂的三次重复**逐字相同**（温度 0 也是），而
两臂之间**每一步都不一样**。这说明「摘掉/留下一件工具」会把这台机器的轨迹整个
岔开——那 `on` 与 `off` 的任何差别都可能是**岔开**而不是**工具**带来的。

## 沙箱（两个臂拿到逐字相同的副本）

复制 `src/spoolkit`、`tests`、`pyproject.toml`、`scripts/probe.py`（跳过
`conftest.py`——仓库那条 autouse fixture 会往平台的 `pytest-of-<user>` 建目录，
受限沙箱下建不出来，会让每条用例在 setup 就 ERROR），再预置：

- 6 条 `# TODO(orchestration-bench): …`（沿用上一轮的配方与同一批文件）；
- `src/spoolkit/service_ledger.py` + `tests/services/test_service_ledger.py`：
  **确定性失败**的测试（`stop` 少了一行 `entry.stopped = True`），跑法
  `python -m pytest -q -p no:cacheprovider tests/services/test_service_ledger.py`。
- 为什么是「另加一个模块」而不是「替换 `src/spoolkit/services.py`」（2026-10-07
  实测纠正）：上一版用精简 `services.py` 顶掉真文件，它少了 `render_status`，
  而 `spoolkit.cli.app` 一路 import 到它 → 副本里 **23 个测试文件收集即
  ImportError**，再加 3 个 `import probe` 失败，裸 `python -m pytest -q` 直接
  `Interrupted: 26 errors during collection`——**目标测试根本没跑**，模型拿到的
  失败要点就是那 26 条 `ERROR tests/cli/...`，于是照着第一条回答
  `tests/cli/test_approve_command.py`。那不是幻觉，是照着被截断的现场读出来的
  （B 靶 `0/2` 的真因，见下节 S3）。

## 判据（**跑之前定好**，机械，只认关键词）

- A：① 覆盖预设 6 条待办（6 个文件名逐个核）；② 有分类；③ 有优先级。
- B：① 点名涉及的文件（`service_ledger.py` + `test_service_ledger.py`）；② 给出修复计划。

⚠ 两条判据的**限度**（照实说）：A 的 ②③ 只认几个记号词（「分类/类别」「优先级/高/中/低」），
B 的 ② 只认「修复/计划/步骤…」这类词——所以 `B/t0.0/control` 那句**反问**
（「请提供失败测试的完整名称…拿到后我会定位涉及文件并给出修复计划」）被判成 ✅。
它不是修复计划，是判据太钝。

## 读数（36 条，温度 0 与 0.4，每格 3 次）

版本：**HEAD `4906942`**（整批 36 条都是它；`src/` 无未提交改动）。
装配：18 件工具（`on`）/ 17 件（`off`、`control`）；system 区段 2557 token
（`off` 臂 2057、`on`/`control` 2071–2073——同一份提示词，差的是那一件工具的索引行）。

见 `real-table.txt`。三件事一眼可见：

1. **`orchestrate` 一次都没被调用**（36/36：调用 0、子调用 0、计划 0 合法 0 被拒）；
2. 24 条里只有 3 条**模型自己说了「做完了」**（`B/t0.0/control` 那 3 条反问），
   其余 33 条都是撞步数上限；
3. `on` 在四个格子里有三个**比 `off` 与 `control` 都贵**（例如 `A/t0.0`：输入
   129135 vs 43709/47066）。

## 已知干扰（解释读数时必须带上）

- 跑批**中途**另一个写者落了一次提交（`d204da3` → `4906942`，压索引/提示词）。
  中途那批见 `real-readings.round1-middle.json`，**不进本轮结论**；本轮 36 条同版本。
  但同一份配方在 `d204da3` 上的读数与现在**形状不同**（那一批 `A/t0.4/on` 是
  8 步 / 3 条判据全 ✅），所以「本轮读数」不能外推到那个版本。
- 跑批后半段撞上**系统代理劫持本机回环**：`HTTP_PROXY=http://127.0.0.1:7890`
  被 `LlamaCppTokenCounter` 的 httpx 客户端吃进去，`/tokenize` 对 `127.0.0.1:8080`
  回 502，整段格子记成「未完成」。跑分器已在 `_isolate_local_http()` 里清掉这组
  环境变量并写 `NO_PROXY`；`diag_tokenize.py` 复现并定位了它。

## S2：靶的角色与判词（**这一节推翻了上面「A 靶」的用法**）

### 先说上面那批读数是怎么被环境弄脏的

上一批里 A 靶两臂都 0/3，一度被读成「模型连汇总待办都做不到」。真因是**测量环境**：
跑分器的子进程只拿到了 `PYTHONIOENCODING`，`PATH` 里没有 ripgrep，`search_code`
直接返回「未找到 rg（ripgrep），无法执行搜索」——8 步里根本取不到待办素材。
补上 `rg` 之后重跑，**A 靶 off/on/control 三臂全是 3/3、3 步、30–36 秒**。

这条不再靠人记着：`spoolkit.envcheck` 把「测量环境自证」做成代码，两个入口都在
开跑前拦一道——

- `scripts/probe.py --check-env`：只做自证并退出，不合格 **退出码 3**（`bench.EXIT_ENVIRONMENT_BLOCKED`）；
- `benchmarks/probes/orchestration/real_counterfactual.py::_prepare_process_env()`：
  在当前进程补 `rg`/`node` 的 PATH、摘掉代理变量、跑 `envcheck.inspect`，不合格
  打报告与建议后 `raise SystemExit(3)`，**这一批不开跑**（跑完再解释，token 已经花掉了）。
  自证结果写进每条读数与落盘 payload 的 `env` 字段。

### 裁决：A 是控制靶，B 是能力靶

- **A（`A-todo`）→ 控制靶**：三臂都做对，**量不出「编排有没有用」**。它真正的用处是
  抓环境故障（缺 rg 时三臂一起归零）——这正是它当初的价值。
- **B（`B-failing-test`）→ 能力靶**：要求认出失败测试涉及的文件并给出修复计划，
  上一批 off 0/2、on(converge) 1/2，**有区分度**，且已知缺陷明确。它当时报的
  `tests/cli/test_approve_command.py` 曾被记成「模型认错文件」——**S3 查明那是
  脚手架+证据管道造成的，见下面 S3 一节**，不是能力结论。

**能力靶准入判据（加新靶时按这条判）**：同一配方下必须至少有一格两臂判据不同。
两臂全过 = 无区分度，只能当控制靶；两臂全不过 = 也量不出差别，先查难度与环境，
别急着改实现。不为「看起来更难」而造靶。

### 判词是机械算出来的，不是人肉看的

`real_counterfactual.py` 里：`TARGET_ROLES`（角色）+ `_cell_passed`（一格通过 =
该靶全部机械判据通过，**`_` 开头的一律是明细**——`_hits` / `_paths`）+
`target_verdict(readings, task)` + `target_verdicts(readings)`。判词取值封闭在
`TARGET_VERDICTS`：
`control_ok` / `control_failed` / `discriminating` / `no_discrimination` / `no_readings`。
跑完逐靶打一行，并写进读数文件的 `target_roles` / `target_verdicts` 与汇总表的
每格 `role` / `passed`。

拿**已提交的那批 36 条读数**（环境脏的那批）算，判词正好复现了 S2 的人工诊断：

```
A-todo: role=control verdict=control_failed | control 0/6、off 0/6、on 0/6 |
        先查测量环境（缺 rg / 代理劫持），别记成模型不行
B-failing-test: role=ability verdict=no_discrimination | control 0/6、off 0/6、on 0/6 |
        两臂全不过，量不出差别：先查难度与环境，别急着改实现
```

⚠ 注意：**这批读数是脏环境下取的**，所以两条判词都在说「先查环境/难度」。它们**不是**
能力结论；S12 会用干净环境重跑并覆盖。这一节要说的是「判据已经会自己说这句话了」。

⚠ 别把两个 `control` 搞混：**三臂里的 `control` 臂**是等长安慰剂（把那一件工具的
索引行换成等长占位，量的是提示长度的作用——`B/t0.0/control` 1 步 / 2120 token，
模型直接反问「请提供失败测试名」）；**靶角色里的 `control`** 说的是这个靶该不该
拿来当能力证据。两者互不相干。

守护：`tests/test_counterfactual_targets.py`（32 条：角色齐全、A=control/B=ability、
控制靶两种形态、能力靶三种形态、明细不算判据、判词进读数文件与汇总表、
真读数文件只查结构不写死具体判词、S7 的沙箱重摆与判据收紧）。变异验收 7/7 红：
控制靶无条件 `control_ok`、能力靶无条件 `discriminating`、少一个靶的角色、
`_hits` 算作判据、A 靶改回能力靶、读数文件不写判词、汇总表不带角色。

## S3：B 靶的「认错文件」是副本与证据管道造成的（**不是能力结论**）

### 症状与追证

上一批 B 靶里 `r0` 报 `tests/cli/test_approve_command.py`，一度被读成「模型认不出
失败测试」。追证两条：

1. **模型看到的原文**：每格 B 的判词第二行逐字带着回灌进去的「失败要点」，内容就是
   一串 `ERROR tests/cli/...`——模型报的那个文件**是它被喂进去的第一行**，它没认错，
   它照着手上的材料回答。
2. **那份材料是怎么来的**：`.tmp/s3_diag.py` 造一次与跑分器逐字相同的副本，跑裸
   `python -m pytest -q`——`exit=2`、**31.8s**、25455 字符、`Interrupted: 26 errors
   during collection`。26 条红行里 17 条是 `ERROR tests/cli/*`；**唯一那条真
   `FAILED tests/services/...` 落在中间被省掉的部分**：头 2000 字、尾 2000 字、
   `_truncate(4000)`、`condense_test_output(360)` **四处都没有它**。

### 两条根因

- **脚手架**：旧 fixture 用替换式写法重写 `src/spoolkit/services.py`，比真文件少了
  `render_status` → 23 条 `cannot import name 'render_status'`；另有 3 条
  `ModuleNotFoundError: No module named 'probe'` → 收集阶段就中断，**目标测试根本没跑**。
- **证据管道**：`_truncate` 只写「中间省略 N 字符」，不说省掉了什么；
  `condense_test_output` 按行顺序拼到 360 字，26 条 ERROR 在前时把唯一的 FAILED 挤掉。

### 修法一：副本现场必须只有那一条红

- fixture 改**只加不减**：不再替换真源码，改为新增 `src/spoolkit/service_ledger.py`
  （`stop()` 里少 `entry.stopped = True`）与 `tests/services/test_service_ledger.py`；
  副本补 `scripts/probe.py`。
- 副本测试**裁到白名单**（`SANDBOX_TEST_KEEP`）：`tests/services/test_service_ledger.py`
  + `tests/context` + `tests/health` + `tests/fetch`。理由是量出来的：副本不抄
  `tests/conftest.py`（那条 autouse fixture 负责铺可用临时根），整棵拷进去时用
  `tmp_path` 的用例成片 `PermissionError: [WinError 5]`——memory 381 条、tools 316 条、
  cli 272 条、web 194 条……`tests/services/test_load.py` 也在这一类里。
- `stage_sandbox()` 先把副本裸跑一遍，**断言恰好 1 条 FAILED、0 条 ERROR**，不满足就
  抛异常**不开跑**（写进 `.real-state.json` 的 `bare_exit`/`bare_failed`/`bare_errors`）。
- 验收读数（`.tmp/s3/accept.json`）：副本裸跑 `exit=1`、**1.54s**、
  `1 failed, 36 passed in 1.09s`，FAILED 1 条、ERROR 0 条；目标测试单跑 0.48s。

### 修法二：截断要说清「丢了什么」

- `condense_test_output`：**FAILED 优先**、有 FAILED 时 ERROR 最多 2 条、总共最多 6 条，
  并报条数：`（共 1 条失败、26 条错误；这里列了 3 条，省略 24 条）`。
- `_truncate`：中间省略标记里点名被省掉的判据行（最多 3 条、每条 ≤120 字），
  例如 `…（输出过长，中间省略 xxx 字符；这一段里有 1 条失败/错误记录：FAILED tests/...）…`。

### 教训

靶读数先问「模型手上到底是什么」，再谈能力。**S12 重跑之前必须先过「副本裸跑只有
那一条红」这道自检**，否则量到的还是副本。

守护：`tests/test_counterfactual_targets.py`（+8 条：fixture 只加不减、抄探针脚本、
白名单就是那四项、清单里的东西得存在、裁剪函数只留白名单、清单写错就报错、
造副本时裁剪 + 阶段零把关）、`tests/tools/test_verify.py`（+2 条）、
`tests/tools/test_exec.py`（+2 条）。变异验收 **8/8 红**（`.tmp/s3_mutation.py`：
白名单塞噪声、裁剪不报错、造副本不裁剪、阶段零不把关、fixture 又替换真源码、
短摘要错误优先、短摘要不报条数、截断不点名）。

## S6：预算阶梯（先压素材，压完仍超硬线才重置）

**这一级要解决什么**：以前预算到线只有「整段重置」一条路，重置会把已经读到的失败原文
一起丢掉。S6 在重置之前插一级：把素材压成「判据原文逐字保留 + 其余按对象去重截断」的
压缩稿，装得下就不重置；装不下才重置，且重置挂的是压缩后那份。

**真机读数**（`real_counterfactual.py`，t=0，每臂 2 次，HEAD `d5b2937`，沙箱
`spool-orch-real-c2ojl3yk`；新增三个字段 `compactions` / `resets` /
`judgement_lines_kept` / `final_names_failing_test`）：

| 窗口 | 臂 | 通过 | compactions | resets | judgement_lines_kept |
| --- | --- | --- | --- | --- | --- |
| 8192（两批同配置） | off | 3/4 | 0 | 0 | 0 |
| 8192 | on | 0/4 | 0 | 0 | 0 |
| 8192 | control | 0/4 | 0 | 0 | 0 |
| 4096 | off | 0/2 | 11 / 8 | 2 / 4 | 95 / 32 |
| 4096 | on | 0/2 | 0 / 3 | 0 / 0 | 0 / 9 |
| 4096 | control | 0/2 | 0 | 0 | 0 |

- **默认窗口 8192 下这一级一次都没触发**：现在的 B 靶 4-8 步就收尾，需求没到整理线。
  重置次数确实从 S6 之前那批（旧 fixture，12-13 步、resets 1-2）降到 0，但这个 0 要归给
  「跑得短」，不能归给 S6。
- **窗口 4096 下这一级真的触发**：12 步里压了 3-11 次，判据原文确实留在压缩后的上下文里
  （9/32/95 条），说明「压完失败断言还在」这条机制在真机上是成立的；但那两条臂都撞 12 步
  上限、判据全红——窄窗口把任务本身压垮了。压过之后仍可能重置（素材本身太大或系统段占满）。
- `ORCH_WINDOW` 以前只喂给环境自证，循环硬编码 8192，于是「换个窗口看阶梯」这件事一次都
  量不到（S6 踩到）。现在它接进 `batch_config()`，由 `tests/test_counterfactual_targets.py`
  钉住。
- **同配置两批 t=0 的 `off|r0` 轨迹不同**（6 步 vs 4 步；第 2 回合起工具结果的字符数就已经
  不一样：405 vs 374），所以「温度 0 逐字复现」这条在这条靶上这次没成立——这是 S13 的输入，
  不是能力结论。

## S7：B 靶可用性（**读数被两处仪器 bug 弄脏过**）

**这一节先修仪器，再读数**。B 靶问的是「找出这条失败测试涉及哪些文件，并给出修复计划」，
而它此前给出的红绿有两处不是模型的锅：

- **沙箱整批共用一份**：`run_once` 只从 `.real-state.json` 读路径，跑与跑之间不重摆。臂的顺序是
  off → on → control，第一跑把 `stop()` 的 bug 修好之后，后面每一跑都开始于**已经全绿**的现场，
  而这个靶问的正是「哪条测试还红着」。证据：S6 那批 8192 读数里 `off|r1` / `on|r0` / `on|r1`
  的 final 都写着「45 passed」——判据从根上问不出来。修法：`reset_sandbox()`（先删上一份再调
  `stage_sandbox()` 造一份新的，`remove_sandbox()` 只认 `SANDBOX_PREFIXES` 里的路径，认不出就
  拒绝删），`real_counterfactual.py` 与 `p0_converge_probe.py` 的 `run_once` 每跑都调。
- **判据能被「我查不出来」骗过**：p0 真机 `converge|r1` 的结论原文是「…尝试读取
  tests/test_service_ledger.py 失败，说明该路径不存在…无法确定失败测试涉及哪些文件，也无法
  给出可执行修复计划」——旧判据（裸文件名 + 宽记号）给了它 **2/2**。修法：只认**带目录的完整
  路径**（`CHECK_B_PATHS`），且计划记号后面必须**紧跟冒号或数字**（`CHECK_B_PLAN_RE`）；
  `_` 开头的一律是明细（`_hits` / `_paths`），永远不参与判据。

**收紧判据后的读数**（t=0，每格 2 次，每跑前重摆沙箱、裸跑不变量 `exit=1 / FAILED 1 条 /
ERROR 0 条` 逐次成立）：

| 探针 | 臂 | 通过 | 步数 | 结论 |
| --- | --- | --- | --- | --- |
| real | off | **2/2** | 4 / 4 | 都点名 `tests/services/test_service_ledger.py` + `src/spoolkit/service_ledger.py`，都有 `修复计划：` |
| real | on | 0/2 | 6 / 6 | 两份 final **逐字相同**：「修复完成…全部 45 个测试通过。」——答的是「我修好了」 |
| real | control | 0/2 | 1 / 1 | 反问「请提供失败测试的完整名称或文件路径」 |
| p0 | base（无收束） | 0/2 | 8 / 8 | r0 路径打错成 `src/spspoolkit/service_ledger.py`；r1 撞步数上限、无结论 |
| p0 | converge | **2/2** | 8 / 4 | r0 一次重置后写了 `修复计划：`（但注明「无法给出具体代码改动」），r1 是教科书式答复 |

靶判词 **`discriminating`**。口径提醒：**判据在 S7 收紧了**，这一节之前的 B ② 读数
（`checks_definition` 里是 `b_files` 的那些）与这里**不可直接比**；另外 `on` 臂连起来看是
0/8（本批 0/2、S6 那批 0/4、再上一批 0/2），而 `orchestrate` 真机 0/36 次被调用——这条
交给 S10 按证据处理，别记成「模型不行」。

**顺带否掉的一件事**：曾按「提示层没说清完整答复指什么」给 SYSTEM / `CONVERGE_NOW` 各补一句
「先照着用户问的逐条答」，A/B（同一靶、off 臂、每版 2 次、每跑重摆、pre/post 交替）四跑**全部
2/2**，两份 final 都把两条路径写全并给出修复计划 ⇒ 那句补丁**没有区分度**（n=4 的零结果），
按「无实测证据不建」**撤回**，`PROMPT_VERSION` 仍是 `2026-10-06.1`。

变异验收 **5/5 红**（`.tmp/s7_mutation.py`：重摆不删不造、删沙箱认出谁都删、判据退回裸文件名、
计划记号退回宽匹配、明细键按旧办法排除）；对应用例在 `tests/test_counterfactual_targets.py`
（32 条，含三段**逐字取自真机结论**的夹具：答不出东西的、真答出来的、工作汇报式的）。


## S8：收束期还准不准写成果（**默认值由对照实验定，取「拦」**）

**机制**：收束态（剩余步数 ≤ `converge_at_steps`）里**不再受理新的「交内容」写动作**
（`replace_lines` / `replace_text` / `write_file` / `delete_file`）。`confirm_rewrite` 不算——
它只是把**已经暂存**的正文落盘，拒它等于把这一轮的工作作废；心算三件（`calc` / `tool_help` /
`remember`）与那一次补素材的读取也不受影响。**已经写下的东西一件都不会少**：拒绝话术里就写着
「东西仍在盘上、仍可回退，现在该做的是给结论」。旋钮 `converge_write_grace`：`0` = 默认（拦），
`1` = 收束期只准写一次，`-1` = 不设上限（旧行为，做对照用）。

**对照实验**（p0 探针、B 靶、t=0、每跑前重摆沙箱、裸跑不变量 `exit=1 / FAILED 1 条` 逐次成立）：

| 步数上限 | 臂（唯一差别 = 写额度） | 通过 | 撞步数上限 / 无结论 |
| --- | --- | --- | --- |
| `max_steps=5`（第 3 步进入收束） | `converge`（`-1` 不设上限） | 1/3 + 2/5 = **3/8** | 5 跑 |
| `max_steps=5` | `converge-nowrite`（`0` 默认） | 3/3 + 5/5 = **8/8** | 0 跑 |
| `max_steps=5` | `converge-onewrite`（`1`） | **2/3** | 1 跑 |
| `max_steps=8`（这一档几乎不被触发） | `converge` / `converge-nowrite` / `converge-onewrite` | 3/3 / 2/3 / 3/3 | 0 跑 |

机制在读数里看得见：**拦写那臂 4–5 步就写出了结论**；不设上限那臂在收束态继续写/改，用光步数、
`concluded=False`、`terminated_by=步数上限`。宽松那批 9 跑只拦到 1 次写——这一档在步数充足时
几乎用不上，所以「宽松批里两臂都在天花板上」不等于它对宽松现场有害。

**B 靶在新默认下复测**（t=0、每臂 2 次、每跑前重摆）：`control 0/2、off 1/2、on 0/2`，判词
`discriminating`。红的 `off|r0` 是 12 步撞上限、23 次工具调用、`write_blocked=1`——它从第 5 步
起就在 `replace_lines` 修补（**前面那些写全是放行的**，没被这一档拦），收束期只拦到 1 次，所以
这一格的红是「它去修而不是去答」，不是这一档造成的；`off|r1` 4 步、两条路径齐全。

顺带修了一处测试卫生：`remove_sandbox()` 的拒绝话术会走 `say()`，而 `say()` 写的是仓库里的
`real-run-log.txt`——跑一次用例就让 `git status` 多一行脏改动、混进提交前的路径清单。
`tests/test_counterfactual_targets.py` 加了 autouse 夹具，把探针模块的 `LOG_PATH` 指到临时目录。

**这一档和「被拒的调用不占步数」会碰在一起**（全量跑出来的）：收束期被拒的写会走既有的
「这一步的调用全部被硬拦 → 不占步数预算（已退还 N/3）」那条路。于是一个**不听收束指令、
继续写**的模型能拿到最多 3 次白跑的步数，直到额度用尽——`tests/agent/test_repeat_guard.py`
的预算用例原本靠「脚本耗尽 ⇒ `finished is False`」证明预算没被放大，现在改成钉
「步数没超预算 + 被拒的写一次都没执行 + 被拒的调用确实走的是『不占步数预算』那条路」。
沿用而不是新造规则：退还额度早就有上限（3），而 S8 的 8/8 读数正是**带着这条规则**量出来的。

口径：读数规模 n ≤ 8 且只有 t=0，只能说**在这几批里**拦写明显更好；`-1` 随时可退回旧行为
（p0 的 `converge` 对照臂**显式**钉着 `-1`，不靠默认值——否则两臂会静默同配）。变异验收
**7/7 红**（`.tmp/s8_mutation.py`：额度永远够、确认落盘也占额度、默认值漂回 -1、放行不留痕、
对照臂不只差一个变量、汇总明细口径退回旧写法、对照臂不再显式钉 -1）。

## S9：关掉「判不了就放行」这条通道（**判不出与无用重复同等处置：拒绝**）

**机制**：待判的重复调用分三类，S9 把中间那一类从「照常执行、只提醒」改成**拒绝**。

- **指纹**（`_call_state`）：`run_command` 的可比对象 = **命令 + 工作目录 + 参数逐字渲染**
  （此前它取不到任何可比对象，所以每一次「同一条命令再跑一遍」都落进『判不出』）。现场
  变没变仍由 `_pending_state_key()`（暂存重写的路径 / 长度 / 行数）单独判。
- **指纹相等** → 无用重复，拒绝；话术里带上**上一次那条命令的输出摘要**，但摘要**只进消息、
  不进判据**——输出是跑完才知道的东西，塞进可比对象会让每次重复都看起来「变了」，那正是
  要关掉的通道。
- **拿不到可比对象、或这个签名从没有真的执行过** → 判不出，**拒绝**。并记
  `kind=guard_repeat_undecided` + `refused=True`（探针读数看得见），话术给出三条出路：
  换一组参数 / 换成能给可比对象的路（读文件、看目录、写文件、把问题写成一条命令）/ 素材
  够了直接给结论。
- **例外**：上一次**执行失败**了（签名在 `_failed_output` 里）→ 判成「有理由的重取」，
  归既有的 `failed_repeat_block_at` 那道闸管（否则「重试」会被这里的拒绝吃掉）。为此顺手
  修了一处**漏账**：放行路（`_guard_allow_repeat`）在失败时也要记回 `_failed_calls`，不记的话
  上游闸数不到重试，「允许一次重试」会变成「允许无限重试」。

**真机读数**（当天 26 跑：A 靶 6 跑、B 靶 6 跑、p0 紧步数 14 跑，每跑前重摆沙箱、t=0）：
`refused_undecided=0`、`repeat_executions=0` —— **S9 新改的那一档在真机上一次都没被踩到**
（这 26 跑里同一条命令没有出现过逐字重发）。踩到的是**既有**那一档：p0 `base|r5` 里模型把
同一条 `find_symbol` 连发 3 次，3 次全被拒（`refused_useless=3`，
`refused_useless_tools={'find_symbol': 3}`），一次都没重跑。所以这一节的真机结论只能写成：
「拒绝这条路在真机上确实落地、被拒的调用确实一次都没执行；而**新档没有被触发过**」。
S9 的正面依据是定向用例（`tests/agent/test_repeat_guard.py` 的「判不出…拒绝」与
「同一条命令现场没变时第二次不再执行」）、**变异 7/7 红**（`.tmp/s9_mutation.py`），以及旧口径
留下的既有真机取证：`list_dir` 连发 15 次、`confirm_rewrite` 连发 18 次时它们**真的执行了**
（那时它们同样没有可比对象）。

**没有回归**：A 靶（控制靶）`control_ok`（control / off / on 各 2/2）；B 靶（能力靶）
`discriminating`（off **2/2**、on 0/2、control 0/2）——off 臂这一批是迄今最好的一批。

**读数口径**：`instrument_guards` / `guard_readings` 两个探针共用（`real_counterfactual.py`
里定义，p0 直接 import），只读地包一层 `registry.invoke`（按「工具 + 参数逐字」归一，数
「真的重跑了几件」）与 `_refuse`（收话术 + 工具名，按判据分成两档）。四列主读数
（`repeat_executions` / `refused_useless` / `refused_undecided` / `command_runs`）加三条明细
（`refused_useless_tools` / `refused_undecided_tools` / `repeat_tools`）——只给总数等于没说，
报告要说清「拦的是哪一件」。


