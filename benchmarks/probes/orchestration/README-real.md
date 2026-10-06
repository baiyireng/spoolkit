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

复制 `src/spoolkit`、`tests`、`pyproject.toml`（跳过 `conftest.py`——仓库那条
autouse fixture 会往平台的 `pytest-of-<user>` 建目录，受限沙箱下建不出来，会让
每条用例在 setup 就 ERROR），再预置：

- 6 条 `# TODO(orchestration-bench): …`（沿用上一轮的配方与同一批文件）；
- `src/spoolkit/services.py` + `tests/services/test_services.py`：**确定性失败**的
  测试（`stop_service` 少了一行 `service.stopped = True`），跑法
  `python -m pytest -q -p no:cacheprovider tests/services/test_services.py`。

## 判据（**跑之前定好**，机械，只认关键词）

- A：① 覆盖预设 6 条待办（6 个文件名逐个核）；② 有分类；③ 有优先级。
- B：① 点名涉及的文件（`services.py` + `test_services.py`）；② 给出修复计划。

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
  上一批 off 0/2、on(converge) 1/2，**有区分度**，且已知缺陷明确（认不出
  `test_services.py`，`r0` 还误报成 `tests/cli/test_approve_command.py`）。

**能力靶准入判据（加新靶时按这条判）**：同一配方下必须至少有一格两臂判据不同。
两臂全过 = 无区分度，只能当控制靶；两臂全不过 = 也量不出差别，先查难度与环境，
别急着改实现。不为「看起来更难」而造靶。

### 判词是机械算出来的，不是人肉看的

`real_counterfactual.py` 里：`TARGET_ROLES`（角色）+ `_cell_passed`（一格通过 =
该靶全部机械判据通过，`_hits` 只是明细）+ `target_verdict(readings, task)` +
`target_verdicts(readings)`。判词取值封闭在 `TARGET_VERDICTS`：
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

守护：`tests/test_counterfactual_targets.py`（16 条：角色齐全、A=control/B=ability、
控制靶两种形态、能力靶三种形态、`_hits` 不算判据、判词进读数文件与汇总表、
真读数文件只查结构不写死具体判词）。变异验收 7/7 红：控制靶无条件 `control_ok`、
能力靶无条件 `discriminating`、少一个靶的角色、`_hits` 算作判据、A 靶改回能力靶、
读数文件不写判词、汇总表不带角色。
