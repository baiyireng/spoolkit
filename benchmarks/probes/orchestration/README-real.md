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
