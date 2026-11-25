# 两条裁决 + 同设置重测（A/B/C）

日期：2026-10-05。跑分设置与参考轮次完全一致（`--provider llamacpp --base-url http://127.0.0.1:8080`，
预算 1500 / 2400 / 2400s，`--temperature 0 / 0.4 --seed 1 --repeat 3 / 0.7 --seed 2 --repeat 3`）。

## 1. A：指标拆分（判据本体零改动）

记录新增四个字段（都进了 `REQUIRED_RECORD_KEYS`）：

| 字段 | 口径 |
|---|---|
| `repeat_blocks` / `read_blocks` | **总数**（保持原义：检测到的原样重复调用次数，读取类另有子计数） |
| `repeat_blocks_blocked` / `read_blocks_blocked` | 其中**被硬拦下、未真正执行**的次数（事件流里的 `⛔ 未执行：…`） |
| `repeat_blocks_unblocked` / `read_blocks_unblocked` | 差值 = **真正的空转**（护栏只提醒、没有拦，那一次真的又跑了一遍） |

`benchmarks/probes/probes.json`：每个探针的 `expect` 保留 `max_repeat_blocks` / `max_read_blocks`，
新增 `max_repeat_blocks_unblocked: 0` / `max_read_blocks_unblocked: 0`；下限判定对两条「重复」线
**只看未被拦的那部分**，旧键保留、同义同线（两个都写时按新键判）。理由写在文件里新增的
`_limits_note`（`--list` 也会打出来）：**护栏自己就是拦截者之后，`⛔ 未执行` 意味着那一次调用
根本没有执行，是护栏在干活，不是空转；把它算成失分等于自己人扣自己人的分。**
`seeds/**`、`expected/**`、`checks/**`、`guard_hashes` 一个字节没动。

判据「未被拦的重复」怎么读出来：`loop.py` 在「只提醒不拦」那条路上把提醒原文拼在工具输出最前面
（`注意：这一步与上一步完全相同…如果它没有帮你推进，就换一种做法。`）。240 份历史事件流里
共命中 63 条，**63/63 都带这句尾巴**（形状 100% 对得上，无误报候选）。

## 2. B：搜索遇到不可读路径要跳过并如实报告

`search_code`：rg 退出码 2 拆成两类——认得出是权限/IO 的（`os error 5/13`、`拒绝访问`、
`Permission denied`…）→ **跳过并报告**；认不出的（正则写错等）→ **仍然报失败**，但把跳过的路径
一并说出来。结果里追加「（跳过 N 个不可读路径：…——这些地方没搜到，不是「它们里面没有匹配」）」；
搜索**起点自己读不了**时给 ok=False 的人话（「没有搜成：…一条都没搜——这不是「没有匹配」」）。
同族遍历工具一并改：`list_dir`（目录读不了给人话 + 单条读不了留痕）、`dir_stats`（跳过的路径
进 `skipped` 并渲染成「⚠ 有 N 个路径读不了，已跳过（合计里没有它们）」）、`survey`（同一条话术，
按首次出现去重）。

### 改前现场（历史事件流，不是推测）

`benchmarks/probes/records/...` 里带沙箱的记录共 240 条，其中 `search_code` 被调用过 89 次，
**2 次整条失败**，两次都是 `vague-optimize @ t=0.7 r1`：

```
2026-10-05T17:36:19 vague-optimize t=0.7 r1 steps=17 rb=7
{"type":"tool","name":"search_code","ok":false,
 "detail":"搜索失败: rg: …\\l6-set3\\vague-optimize-r1\\pytest-cache-files-rr1_3kf0: 拒绝访问。 (os error 5)",
 "intent":"找测试入口与用例"}
```

### 改后同一沙箱（改前/改后对照，同一个目录）

```
沙箱：C:\Users\32615\AppData\Local\Temp\dsh-DiZNqO\spoolkit-probes\l6-set3\vague-optimize-r1
  读不了（拒绝访问。）：pytest-cache-files-rr1_3kf0
[改前] 直接跑 rg：退出码 2 → 旧实现 returncode not in (0,1) 一律判失败，命中全丢
[改后] search_code：ok=True
  gen_log.py:22:    total = 0 …（命中照常返回）
  （跳过 1 个不可读路径：…pytest-cache-files-rr1_3kf0——这些地方没搜到，不是「它们里面没有匹配」）
```

### 重测里 B 真的被走到

三组新设置里 `search_code` 共 36 次调用：**23 成功、6 被护栏拦下（没执行）、7 参数校验失败
（模型自己传错参数）、0 真报错**；其中 `set9 vague-optimize r1`（就是当初整条失败的那条）
2 次调用都成功、**2 次带「跳过 N 个不可读路径」**，沙箱里确实有 1 个 `pytest-cache-files*`。

## 3. 重测表（逐探针三设置）

### t=0 ×1（set7-t0-l3，358.5s / 预算 1500s）

| 探针 | 过/采样 | 步数 | 输入tok | 输出tok | 墙钟s | 判据绿 | 下限过 |
|---|---|---|---|---|---|---|---|
| merge-html | 0/1 | 5 | 32363 | 1526 | 79 | 1/1 | 0/1 |
| vague-optimize | 1/1 | 3 | 12049 | 1121 | 79 | 1/1 | 1/1 |
| add-feature | 1/1 | 5 | 21735 | 1477 | 63 | 1/1 | 1/1 |
| fix-bug | 1/1 | 3 | 13613 | 586 | 34 | 1/1 | 1/1 |
| rename-cross-file | 1/1 | 8 | 38112 | 2187 | 102 | 1/1 | 1/1 |

合计 **4/5**。

### t=0.4 s1 ×3（set8-t04-s1-l3，1475.4s / 预算 2400s）

| 探针 | 过/采样 | 步数（极差） | 输入tok（极差） | 输出tok（极差） | 墙钟s（极差） |
|---|---|---|---|---|---|
| merge-html | 2/3 | 3-8（5） | 27407-35770（8363） | 1361-1839（478） | 70-85（15） |
| vague-optimize | 2/3 | 4-15（11） | 19543-74507（54964） | 1295-4835（3540） | 89-261（172） |
| add-feature | 3/3 | 8-9（1） | 37581-39870（2289） | 1998-3013（1015） | 98-127（30） |
| fix-bug | 3/3 | 4-4（0） | 17596-21349（3753） | 624-949（325） | 35-50（16） |
| rename-cross-file | 3/3 | 6-8（2） | 32103-38726（6623） | 2071-2288（217） | 94-144（50） |

合计 **13/15**。

### t=0.7 s2 ×3（set9-t07-s2-l3，1488.1s / 预算 2400s）

| 探针 | 过/采样 | 步数（极差） | 输入tok（极差） | 输出tok（极差） | 墙钟s（极差） |
|---|---|---|---|---|---|
| merge-html | 3/3 | 4-10（6） | 21508-45497（23989） | 1080-2970（1890） | 55-137（82） |
| vague-optimize | 3/3 | 6-7（1） | 22421-36207（13786） | 1408-2887（1479） | 94-202（108） |
| add-feature | 2/3 | 6-8（2） | 27040-36373（9333） | 1978-2830（852） | 86-120（34） |
| fix-bug | 2/3 | 3-4（1） | 13074-17470（4396） | 479-641（162） | 30-36（6） |
| rename-cross-file | 2/3 | 6-10（4） | 28938-65908（36970） | 2009-2773（764） | 84-154（70） |

合计 **12/15**。

### 与 27/35（4/5、13/15、10/15）逐项对照

| 探针 | 参考 t=0 | 新 t=0 | 参考 t=0.4 | 新 t=0.4 | 参考 t=0.7 | 新 t=0.7 |
|---|---|---|---|---|---|---|
| merge-html | 1/1 | 0/1 | 2/3 | 2/3 | 3/3 | 3/3 |
| vague-optimize | 1/1 | 1/1 | 3/3 | 2/3 | 1/3 | **3/3** |
| add-feature | 0/1 | **1/1** | 3/3 | 3/3 | 2/3 | 2/3 |
| fix-bug | 1/1 | 1/1 | 3/3 | 3/3 | 3/3 | 2/3 |
| rename-cross-file | 1/1 | 1/1 | 2/3 | **3/3** | 1/3 | **2/3** |
| **合计** | 4/5 | 4/5 | 13/15 | 13/15 | 10/15 | **12/15** |

参考 27/35 → 新 **29/35**；上一轮同设置（set4/5/6，护栏已引入）26/35（4/13/9）。

### 失分构成（新三组共 6 条失分）

| 类别 | 条数 | 明细 |
|---|---|---|
| **真病态：未被拦的重复** | 2 | `merge-html r0 @t=0`（未拦 1、被拦 2，判据绿）；`fix-bug r1 @t=0.7`（未拦 1） |
| 其它下限 | 0 | — |
| 判据红 | 4 | `merge-html r2 @t=0.4`（下限过：被拦 2/2、未拦 0）、`vague r1 @t=0.4`（未拦 1/1）、`add-feature r2 @t=0.7`（下限过）、`rename r0 @t=0.7`（未拦 1） |

「未被拦的重复」两条都是**判据绿、只输在下限**——这正是这条线该抓的东西（模型真的把同一个调用
又发了一遍、结果不会变）；而判据红那 4 条里有 2 条的下限项是被拦次数，现在**不再扣分**
（`merge-html r2 @t=0.4`：被拦 2 次重复 + 2 次读取，未被拦都是 0，下限过）。

### 口径变更本身的效应（同一批历史事件流重放，模型没变）

| 参考轮次 | 旧判 | 新判 | 变动明细 |
|---|---|---|---|
| set1 t=0 | 4/5 | 4/5 | 平 |
| set2 t=0.4 | 13/15 | **14/15** | 升：`rename r2`（被拦 6 次、未拦 0）不再扣分 |
| set3 t=0.7 | 10/15 | **8/15** | 降：`fix-bug r1`、`rename r1` 有**未被拦**的重复（旧解析器根本没数这一项） |
| set4 t=0 | 4/5 | 4/5 | 平 |
| set5 t=0.4 | 13/15 | **12/15** | 降：`rename r2` 未被拦 6/2 |
| set6 t=0.7 | 9/15 | **12/15** | 升：`merge r0`、`rename r0`、`rename r1`（都只输在被拦次数上） |
| 合计 | 53/70 | 54/70 | +1 |

所以这次改的是**尺子**：对「被拦」放宽、对「未被拦」收紧（旧尺子完全看不见后者）。

## 4. 合计升/平/降

- 对 27/35 参考：**升 +2**（27 → 29），逐组 平 / 平 / **升 +2**。
- 对上一轮同设置 26/35：**升 +3**（26 → 29），逐组 平 / 平 / **升 +3**。
- 升的证据在两处：①`t=0.7` 的 `merge-html` 3/3、`vague-optimize` 3/3（参考轮是 3/3、1/3）、
  `rename` 2/3（参考 1/3）；②原来「只输在被拦次数」的尝试不再扣分（见上表口径效应）。
- 降的部分如实报：`fix-bug@t=0.7` 2/3（参考 3/3）、`vague@t=0.4` 2/3（参考 3/3），
  以及上面口径效应里 set3/set5 各降 2/1 条——原因都是**新尺子第一次看见了「未被拦的重复」**。
- 真病态没有归零：本轮仍有 2 条尝试真的空转了一次（`merge-html@t=0`、`fix-bug@t=0.7`）。

## 5. 变异与读数

| # | 改了什么 | 哪条红 |
|---|---|---|
| 1 | `repeat_blocks_unblocked := repeat_blocks`（把两个量合并回一个） | `test_被拦的重复_记录分开两个量且下限通过`、`test_记录字段齐全且不改种子` |
| 2 | 下限取 `repeat_blocks`（判总数，回到改前口径） | 上面两条 + `test_旧下限键保留且口径同一个` |
| 3 | `search_code` 恢复「退出码不是 0/1 就整条失败」 | `test_部分路径读不了要跳过而不是整条失败` 等 4 条（含真沙箱那条） |
| 4 | 跳过但**不报告**（note 置空） | `test_部分路径读不了…`、`test_没有命中但…`、`test_真锁死的目录还在时…` |
| 5 | `dir_stats` 跳过但不报告 | `test_读不了的路径要跳过并报告而不是静默` |

五条都是「改 → 跑 → 红 → 用 `edit` 还原」（未用 `git checkout`）；还原后 `git diff` 里没有
任何 `MUTANT` 残留，相关 7 个测试文件复跑全绿。

- 全量 `tests\`：**2837 收集 / 2834 passed / 2 failed / 1 skipped**。基线 2826/2823/2/1；
  差值 +11 条正好是本次新增的用例（probe 3 + search 5 + fs/stats/survey 各 1）。
  那 2 条失败仍是 `tests/cli/test_service_command.py` 的沙箱 `taskkill` 项（按要求没动）。
- 配额：system 区段离线 **1960**（含工具索引 2125）、`effective()` **3482**，与冻结线一致；
  本轮**没有改任何模型可见提示**（`git diff` 里没有任何 `description=`/模板改动），
  `tests/test_prompt_mechanics.py` 绿。

## 6. 文件清单（未 `git add`/`commit`）

改动（13 个 tracked）：

- `benchmarks/probes/probes.json`（新键 + `_limits_note`）
- `benchmarks/probes/results.jsonl`、`benchmarks/probes/summary.json`（重测追加/覆盖）
- `scripts/probe.py`
- `src/spoolkit/tools/search.py`、`src/spoolkit/tools/fs.py`、`src/spoolkit/tools/stats.py`、`src/spoolkit/tools/survey.py`
- `tests/test_probe_runner.py`、`tests/tools/test_search.py`、`tests/tools/test_fs.py`、`tests/tools/test_stats.py`、`tests/tools/test_survey.py`

新增（untracked）：

- `benchmarks/probes/stability/l3-report.md`（本报告）
- `benchmarks/probes/stability/set7-t0-l3.{json,txt}`、`set8-t04-s1-l3.{json,txt}`、`set9-t07-s2-l3.{json,txt}`（本轮三组汇总 + 文本报告）
- `benchmarks/probes/records/20261005-192927-*.json`（5）、`20261005-193526-*.json`（15）、`20261005-200001-*.json`（15）——逐次记录
- 现场脚本（`.tmp/` 下，gitignored 草稿）：`b_search_probe.py`、`b_before_after.py`、`verify_unblocked.py`、
  `check_shape.py`、`rescore_refs.py`、`report_retest.py`、`check_new_search.py`、`check_new_search2.py`、
  `quota_measure.py`、`retest_l3.ps1`、`l3-report-full.md`

`git status --porcelain` 共 55 条：13 条 ` M`（上表前 13 个）+ 42 条 `??`（6 个 stability 文件 +
本报告 + 35 条逐次记录）。没有 `git add`/`commit`。

顺带修掉一个我自己引入的小回归：`_limits_note` 里原先写了 `⛔` 这个字符，在 GBK 控制台上
`python scripts/probe.py --list` 会 UnicodeEncodeError（`⛔` 不在 GBK 里）；换成文字描述后
`--list` 退出码 0 并正常打出这段口径说明。这只影响控制台显示，不影响判定（`--report` 走 UTF-8）。

## 7. 没验的

- 8080 只读了 `/props` 与 `/tokenize`，没有重启/停止；`.env` 与 `D:\workSpace\agentWorkSpace` 没碰。
- 「护栏放行的刷新读取」（`guard_refresh`，理由是内容已过期）**没有计入**未被拦的重复，
  因为那是有理由的重取。这是个判断，不是实测结论。
- 「未被拦的重复」靠提醒原文识别（240 份历史事件流 63/63 形状相符、无误报），
  但若某次工具输出里恰好逐字包含那句提醒，会被误计——没有构造过这种反例。
- `merge-html@t=0` 的真病态（重写 README 内容一字未变）与 `fix-bug@t=0.7` 那条只看了事件流，
  没有逐档复现模型侧的原因。
