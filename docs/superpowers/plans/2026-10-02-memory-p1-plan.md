# 记忆架构 P1 实施计划（显式写入 · 相关性三步走 · 五维读取 · 状态与时序 · 注入三层 · 评测）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让记忆从"一个 Markdown 文件 + 一张混装表"变成**有来源、有门禁、可审计、可解释、能推出当前值**的一层：模型与用户有显式写入口（`remember` 工具 + 五道门禁落到新表 `semantic`）；检索从"逐字 OR + 命中即返回"换成"双字 shingle 口径 → bm25 排序 → 五维可解释重排"；`spool memory why` 能说出**每一条为什么进/没进上下文**；`(entity, attribute)` 的事件链能**确定性地推出当前值**（余额例子：100 → 花 20 → 赚 50 = **130**），冲突**不静默选一个**；三层注入（工作/热/检索）的时机与预算**写死**，只有任务级状态默认直注。

**Architecture:** 全部改动落在三条线上，且**顺序是定案的一部分**：

1. **写入侧收口**：`user_version` 从 2 步进到 4——v3 建 `semantic` / `semantic_fts` / `memory_link` / `memory_write` / `memory_audit` / `memory_decision`，v4 建 `memory_event` / `memory_state` / `entity_attribute` / `entity_alias` / `attribute_alias` / 事件归档表；**旧 `memory` 表与 `memory_fts` 一个字节不动**（决议 7）。五道门禁（脱敏/J1–J5/幂等/去重/冲突）做成**一处写入口** `writer.write_memory`，`archive_task`、`remember`、CLI、迁移全部走它。
2. **相关性按定案顺序走**（§13.11 / §14.8）：① 中文分词口径先上**双字 shingle**（零依赖、纯 SQLite）→ ② 再换 **bm25** 排序 → ③ 嵌入**不引入**（`w_embed` 默认 `0.0`，只在三个出口里**可见地**报"关"）。
3. **读取侧成为纯函数**：`rank.py` 吃候选、吐 `chosen`/`dropped` + 每条的**分值分解**；两阶段（N 候选 → K 重排，**K ≤ recall_limit**，重排只能减少不能增加）；事件链作为**一个单元**参与；`memory_decision` / `--events memory` / `spool memory why` 是**同一份数据**的三个视图。
4. **状态与时序**（§15）：`event` append-only、`state` 只能由**确定性归约器**写、时钟**可注入**（生产真实、测试伪造）、更新走"关闭 + 取代"而不是改值、六类不确定**不许静默选一个**。

**Tech Stack:** Python ≥ 3.12，标准库 + 现有依赖（`sqlite3` 自带 FTS5 与 `bm25()`、`argparse`、`fractions`/`decimal`）；**零新依赖**（`pyproject.toml` 一个字不改）；测试 pytest，命令行入口 `.\.venv\Scripts\spool.exe`。

**设计文档：** [`docs/superpowers/specs/2026-10-02-memory-architecture-design.md`](../specs/2026-10-02-memory-architecture-design.md)

**P0 已收官并提交**（`git log`：本计划写就时 HEAD = `c13e68e`，工作树干净；P0 的 8 个任务见 [`2026-10-02-memory-p0-plan.md`](./2026-10-02-memory-p0-plan.md)）。**P1 不重复建设 P0 已落地的东西**：`gate.py` 的 J1/J3/J4/J5 与 `normalize_key`/`content_hash`/`sanitize`、`hot.py` 的 `importance_hint` + 重要度淘汰 + 条数/字数上限、`store.py` 的 `user_version` 0→2、`insert_memory`、`memory_fts(text,kind,trigger,source)`、`open_db_readonly`、`lessons.py`、`cli/commands/memory.py` 的 `migrate`——**只扩展，不重写**。

**本计划只写 P1**，对应设计稿 §9.5 的 P1 行，并落实 §13.13 的 8 条已定案与 §15 全章。本文件**不含任何代码改动**，**不声称任何一条已经实现**，**不声称任何评测已经跑过**。

---

## 范围：P1 做什么、不做什么

| 做（本计划 16 个任务） | 不做（属 P2 或用户明确排除，一条都不许顺手做） |
|---|---|
| ① `semantic` 六表 + `content_hash` **唯一部分**索引（§4.3） | 嵌入真引入：`embed_model`/`embed_dim`/`embed_vec` 三列、`embed-light`/`embed-onnx` extra、`sqlite-vec`/`vec0`、`spool memory embed setup/warm/clean`（§13.13 第 6 条 + 用户指令） |
| ② 时序六表 + `event_archive_threshold`（§15.7 / §13.13 第 5 条） | 巩固（两级，`spool memory consolidate`）——决议 4 + §9.5 P2 ① |
| ③ 五道门禁落到 `semantic`：脱敏 / J1–J5（**新增 J2**）/ 幂等 `round_key` / 精确+近似去重 / 冲突（§5.3） | 衰减与 `semantic_max_per_kind` 容量上限——§9.5 P2 ② |
| ④ `remember` 工具（模型可调用的中途写入口，§5.2） | 机器级 `scope`（`%APPDATA%\spoolkit\memory.db`、跨工作区检索）——决议 2 + §9.5 P2 ④ |
| ⑤ ① 双字 shingle 分词口径 + M2' 三臂对照仪器（§14.8 方案 A） | 网页壳记忆面板（**只读面板也不做**，用户 P1 范围未列，见「口径裁决 7」） |
| ⑥ ② `bm25()` 排序（§14.8 第②步） | `episode_archive` 表与 `episode_archive_threshold`（§9.5 P2 ④）——**注意**：`memory_event` 的归档表**做** |
| ⑦ 五维可解释排序 + 两阶段候选→重排（§6.1 / §14.4 规矩 3） | 技能自动执行（`skill` 变成可运行脚本）——§12 第 6 条 |
| ⑧ 事件链作为**一个单元**参与候选与重排（§6.7） | 自动物理删除 / 自动遗忘（`--purge` 之外的任何 DELETE 路径）——§12 第 3 条 |
| ⑨ 三层注入策略 + 直注门槛 + 预算与让位 + 工作记忆白名单（§6.6 / §13.10） | 用户画像 / 偏好推断（偏好只能显式写）——§12 第 5 条 |
| ⑩ 可注入时钟 + 相对时间解析（§15.5 / §13.13 第 2、3 条） | `spool memory audit --undo`（反向执行 edit/delete/supersede）——§7.3，属 P2 ④ |
| ⑪ 注册表 / 别名 / 五个归约器 / 有效期与取代链 / 冲突六类（§15.2–§15.6） | 第二级去重的**语义**版本（同义改写抓不到，**如实写在文档与 `--explain` 里**，不假装） |
| ⑫ `memory_decision` + `--events memory` + `spool memory why/list/search/show`（§6.3） | 多用户 / 鉴权 / 云同步（§6.5） |
| ⑬ 状态与时序的**纠错入口**（P1 只给 CLI，§13.13 第 4 条） | 「复活 `doing` 分节」——它是 §2.1 的历史坑（`hot.py:23-30` 定义了、`hot.py:36-43` 把它排在下沉顺序首位、`archive.py:139` 主动 `pop`） |
| ⑭ 旧库迁移（`memory` → `semantic`，幂等）+ 回放走同一门禁（§9.3 第 2、4 步 / §9.4） | `spool memory doctor`——设计稿 §13.6/§14.6 说 P1 要加，但**用户给的 P1 范围未列**，见「口径裁决 7」 |
| ⑮ 迁移与回滚演练（不许有不可逆动作） | 为记忆单独做常驻服务 / 后台进程——§12 第 7 条 |
| ⑯ M1 两轮仪器 + M4-T6 跨天脚本（§10.1 / §10.4） | 网页可写面、`spool memory edit/forget/trust/merge` 的写动作（§7.4 的 P2 列） |

---

## 口径裁决：与设计稿的偏差、以及必须由用户确认的地方（先摆明，别默默改口径）

1. **`semantic_fts` 不用 SQL 触发器**（与 §4.3 原则二偏差）。理由与 P0 在 `memory_fts` 上绕过的那条**完全相同**：中文检索依赖写入前把中文字逐字拆开（`store.py:96-98` 的 `normalize_for_fts` 是 Python 函数，触发器里没有它）；P0 已经把同步责任收成"一处 Python 写入口"（`store.py:281-319` 的 `insert_memory`，注释明确写了这条）。P1 沿用同一形态：`insert_semantic` 一处写主表 + 索引，并配用例 `test_semantic_fts_不用触发器`（与 P0 的 `tests/memory/test_schema_version.py:246` 同形）。**这条从 P0 风险清单（P0 计划「风险 9」/「待拍板 4」）挂到 P1，必须在开工前定。**
2. **嵌入相关的旋钮与列一律不进 P1**（用户指令 + §13.13 第 6 条）。只保留 `w_embed` 一个旋钮、默认 `0.0`——因为它是五维公式 `rel = normalize(w_bm25*bm25_norm + w_embed*embed_cos)`（§14.4 规矩 1）里的系数，且 §6.3 把 `embed` 列为 `why` 的**必备字段**。P1 里这个字段只会是"关"（`off:disabled`），但**必须打得出来**（§14.4 规矩 6 的出口③在 P1 的形态）。`memory_decision` 从建表起就带 `embed_status` 列（§14.4 规矩 6 的出口①）。
3. **两阶段的 N/K 旋钮改名**：设计稿 §14.4 规矩 3 把它们叫 `embed_candidates_n` / `embed_rerank_k`（它们当初是和嵌入一起设计的）。P1 不引嵌入但**需要**这两个数，本计划登记为 **`recall_candidates_n=50`** / **`recall_rerank_k=5`**（中性名，语义与硬约束完全相同：`K ≤ recall_limit`、重排只能减少不能增加）。第③步落地时要么保留别名、要么按 §14.4 的名字对齐。**待确认。**
4. **两个默认值是本计划自取的**（设计稿没给）：`w_bm25`（取 `1.0`，单信号时等价于纯 bm25）与 `state_direct_inject_max_age_days`（§13.13 第 7 条只写"最近 N 天"，本计划取 30，与 `rank_recency_half_life_days` 同量级）。两者都在 `limits.py` 的 `note` 里写明"**本计划初值，不是设计稿数值**"。
5. **`semantic.status` 需要多一个取值 `needs_review`**：§9.3 第 2 步要求"`evidence` 为空 → 标 `needs_review`（不静默丢弃）"，但 §4.3 的 `semantic.status` 枚举只有 `active|archived|superseded`。本计划的做法是**加取值而不是加列**（`status` 是 `TEXT`、无 `CHECK` 约束），在 `semantic.py` 里把 `SEMANTIC_STATUSES = ("active","archived","superseded","needs_review")` 登记成常量。**待确认。**
6. **`state` 不跨工作区 = 口径确认，不加机制**（§13.13 第 8 条）：`memory_state` **不设 `scope` 列**，CLI/工具**不接受** `machine`（显式给了就明确报"state 不跨工作区"并非零退出）。同理 `semantic.scope='machine'` 的**写入**在 P1 一律拒绝并给可读理由（不静默降级）。
7. **两处"设计稿说 P1、用户范围没列"的东西不进本计划的 16 个任务**：① 网页壳记忆面板（决议 3 说 P1 只读）；② `spool memory doctor`（决议 6 / §14.6 说 P1 加）。两者都**单列在「待用户拍板」**，不塞进任务里冒充范围。
8. **`imp` 维度的"+0.1"表述有歧义**：§6.1 写"被 `supersedes` 过或通过验证的 +0.1"。字面读会把**已被取代的旧条目**加分，与 §15.4"旧行不删、两种问法都要能答"组合起来会让 `superseded` 行排上来。本计划按"**成功取代过别人**的条目 +0.1；被取代的旧条目不加分"实现，并在任务报告里标注这是**本计划对歧义表述的解释**。**待确认。**
9. **`memory.md` 不因 P1 改变形态**（决议 6）：P1 只把数据从热记忆往 `semantic` 搬，**不给 `memory.md` 加任何新元数据、不改分节标题**（`hot.py:21-46` 的 `HOT_TITLE` / `SECTION_TITLES` / `DEMOTE_ORDER` 一律不动）。

---

## Global Constraints

- **只做 P1**：上表右列一条都不做。**新增依赖 0 个**（`pyproject.toml` 一个字不改）；新增表 12 张（v3 六张 + v4 六张）、**不删任何表、不改任何既有表结构**。
- **回归底线**：本轮**只复跑了收集**——`pytest --collect-only -q` → **2203 tests collected**（本轮实测，4.56s）；`tests/memory` → **266 tests collected**（本轮实测）。P0 计划里记的 `2096 passed / 2 failed / 1 skipped`（收集 2099）是 **P0 收尾时的读数**，本轮**没有复跑全套**。执行者每批开工前必须先跑一次全套拿到**当轮**基线，不许拿本计划的数字当基线。
- **两条既有失败不许去修**，nodeid 是：`tests/cli/test_service_command.py::test_能拉起再停掉自己的孩子`（`tests/cli/test_service_command.py:183`）、`tests/cli/test_service_command.py::test_停的时候信号杀不掉也要能停`（`tests/cli/test_service_command.py:204`）。除它们之外任何失败都算回归，必须当轮处理。
- **跑测试只用脚本**：`.\scripts\pytest.ps1`（worktree 加 `-Repo <路径>`，只跑一部分就把路径透传给它）。⚠️ 本机 PowerShell 执行策略会拦 `.ps1`（P0 实测 `UnauthorizedAccess`）——被拦时按脚本体里点名的五件事手抄，一件都不能漏：`PYTHONPATH=<repo>\src;<repo>\scripts`、`PYTEST_ADDOPTS=-p pytest_sandbox_compat -p pytest_hang_guard`、`PYTEST_DEBUG_TEMPROOT=<仓库外新建空目录>`、`PYTHONUNBUFFERED=1`、`PATH` 里有 `rg`。
- ⚠️ **`rg` 在本轮这个 shell 里不在 PATH**（`Get-Command rg` 为空，`scripts/pytest.ps1` 的说明第 4 条点名这件事）。跑全套前先确认 `rg` 可用，否则 `tests/tools/test_search*` / `test_view` / `test_read_roots` 会红出 6 条环境失败，被误当回归。
- **真实工作区 `D:\workSpace\agentWorkSpace` 只读**：只许把 `memory.db` **复制到本仓 `.tmp\`** 后对副本操作；不许在原库上跑迁移、不许写、不许改名、不许生成 `.bak`/`-wal`/`-shm`。
- ⚠️ **绝对不许读该工作区的 `.env`**（真实密钥）。不许 `Get-Content`/`cat` 它，不许跑 `spool env` / `spool config`，不许跑任何需要密钥解析的命令指过去。`spool memory` 子命令一律**保持"不定义 `provider` 属性"这个口径**（`cli/app.py:1041-1046` 的理由）：`main()` 里"缺供应商向导 + `settings.describe_missing()` + `resolve_provider_args`"整段都在 `hasattr(args, "provider")` 之后，没有这个属性就一个字节的 `.env` 都不会被读。每个新增子命令都要有用例钉住这一点。
- **不动 8080 上的模型服务（pid 43096）**：P1 的所有施工**不重启、不启停、不探测**它。M1 / M4-T6 需要模型服务时，**何时重启由使用者决定**（§10.1 的原话），脚本自身**不许**重启任何服务。
- **不许起长驻进程**（不跑 `spool web` / `spool serve` / `spool bench` 的常驻形态）。
- 上限与阈值**不写死**：一切新旋钮进 `limits.KNOBS`（`CAPABILITY`，可被 `.agent/limits.json` 覆盖），沿用既有机制（`limits.py:49` / `limits.py:331`）。**`limits.py` 在本批里是单写点**——只有 Task 1 碰它。
- 中文注释与面向人的文案；标识符英文。
- **不 `git add` / 不 `git commit`**。**本计划交付时只新增这一个文件**（不改代码、不改设计稿、不改其它文档）；施工期若需要文档回写（`docs/cli.md` 一条新命令、`docs/dogfooding.md` 一条实测），单列在 Task 13 / Task 15 里，**不许在计划阶段预写**。
- 每个任务**先写会红的用例（红）→ 再实现（绿）**；不可证伪的验收不算验收；**没有测量就不许写结论**。

---

## 文件结构

| 文件 | 动作 | 职责 | 归属任务 |
|---|---|---|---|
| `src/spoolkit/limits.py` | 改（**唯一一次**） | P1 全部新旋钮（22 个）集中登记，只追加不改既有 | T1 |
| `src/spoolkit/memory/store.py` | 改（**单写者，严格串行**） | `_MIGRATIONS` 追加 v3/v4；`semantic_fts` 的 shingle 写入/查询；`search_semantic` 的 `bm25()` 排序 | T2 → T3 → T4 → T5 |
| `src/spoolkit/memory/semantic.py` | 新增 | `Semantic` 行类型、`SEMANTIC_STATUSES`、`insert_semantic`/`touch_semantic`/`get_semantic`/`list_semantic`/`search_semantic` | T2 |
| `src/spoolkit/memory/rank.py` | 新增 | 五维打分的**纯函数**：`rank(candidates, now, overrides) -> RankedResult`（`chosen` + `dropped` + `parts`） | T6 → T11 |
| `src/spoolkit/memory/writer.py` | 新增 | 五道门禁的**唯一写入口**：`WriteRequest`/`WriteResult`/`write_memory` | T7 |
| `src/spoolkit/memory/gate.py` | 改 | `RULES` 追加 J2；`judge` 增 `evidence` 参数 | T7 |
| `src/spoolkit/memory/distill.py` | 改 | `DISTILL_SCHEMA` 增必填 `evidence`；`_parse_entries` 解析它 | T7 |
| `src/spoolkit/memory/archive.py` | 改 | 通过判据的条目改走 `writer.write_memory`；`ArchiveResult` 明确"单写 semantic" | T7 |
| `src/spoolkit/memory/timeline/__init__.py`、`clock.py`、`parse.py` | 新增 | 可注入时钟（`SystemClock`/`FakeClock`）+ §15.5 四步时间解析 | T8 |
| `src/spoolkit/memory/timeline/registry.py`、`reduce.py` | 新增 | `entity.attribute` 注册表与别名；五个归约器 + 有效期/取代链 + 冲突六类 | T9 |
| `src/spoolkit/memory/timeline/chain.py` | 新增 | 链单元的构造与"可变数量类问题"的机械判据 | T11 |
| `src/spoolkit/memory/decision.py` | 新增 | `DecisionRecord` + `record_decision`（三出口的唯一序列化） | T13 |
| `src/spoolkit/memory/session.py` | 改 | `recall` 输出分值分解；`hot_text` 交出被淘汰项；链单元进候选 | T11 → T13 |
| `src/spoolkit/memory/tools.py` | 改 | 新增 `remember_spec`（形制照 `recall_spec`） | T10 |
| `src/spoolkit/agent/state.py` | 改 | `TaskState` 追加 `decisions`/`constraints`；`render()` **末尾**追加两行 | T12 |
| `src/spoolkit/agent/loop.py` | 改（**单写者，串行**） | 三层注入与让位；直注门槛；`memory_decision` 落库；`--events memory`；任务结束收掉 | T12 → T13 |
| `src/spoolkit/cli/runtime.py` | 改 | 在 `runtime.py:457-475` 的同一处注册 `remember` | T10 |
| `src/spoolkit/cli/commands/memory.py` | 改（**单写者，串行**） | `why`/`list`/`search`/`show`；`state`/`event`/`attr`/`alias`；旧库迁移与回放 | T13 → T14 → T15 |
| `src/spoolkit/cli/app.py` | 改（**同上一行**） | `_add_memory_command` 的子命令与参数扩展；**保持不定义 `provider`** | T13 → T14 → T15 |
| `benchmarks/memory/corpus.jsonl`、`queries.jsonl` | 新增 | M2' 的 16 条语料（G1–G4）+ 13 条查询与相关 id，从 `.tmp/embed-bench/bench_common.py` **逐字固化** | T4 |
| `scripts/memory_relevance.py` | 新增 | M2' 三臂仪器（`store-char` / `bigram` / `bigram+bm25`），指标 `p@5`/`r@5`/`top1` | T4 |
| `scripts/memory_bench_twowheel.py` | 新增 | M1 两轮编排与读数（**不起服务、不重启服务**） | T16 |
| `benchmarks/tasks/temporal/` | 新增 | M4-T6 的跨天多轮任务脚本 + 伪造时钟 | T16 |
| `src/spoolkit/bench.py` | 改（最小） | 只加"两轮同会话"的开关，**默认仍 `--no-memory`** | T16 |
| `tests/memory/test_schema_semantic.py`、`test_bigram.py`、`test_relevance_harness.py`、`test_semantic_search.py`、`test_rank.py`、`test_writer.py`、`test_timeline_clock.py`、`test_timeline_parse.py`、`test_timeline_reduce.py`、`test_timeline_registry.py`、`test_remember_tool.py`、`test_schema_timeline.py`、`test_event_chain.py`、`test_injection_policy.py`、`test_decision.py`、`test_memory_cli_readonly.py`、`test_memory_cli_state.py`、`test_migrate_db.py`、`test_replay.py`、`test_temporal_fixture.py` | 新增 | 每个任务自己的红→绿证据 | T2–T16 |

---

## 任务顺序与依赖

```
                ┌─ T1 旋钮登记（limits.py，单写点）
批次 1 ─────────┤
                └─ T2 schema v3 ─→ T3 schema v4
                                    │
批次 2 ─────────────────────────────┼──────────────────────────────────────────┐
   store.py 线（单写者）: T4 ①shingle+M2' ─→ T5 ②bm25 ─→ T6 五维+两阶段          │
   写入线:               T7 五道门禁落到 semantic                              │
   时序线:               T8 时钟+时间解析 ─→ T9 注册表+归约器+取代链+冲突         │
                                    │                                          │
批次 3 ─────────────────────────────┼──────────────────────────────────────────┤
   T10 remember 工具（tools.py+runtime.py）                                     │
   T11 事件链单元（rank.py+session.py+chain.py）                                 │
   T12 三层注入+直注门槛+预算让位+工作记忆（loop.py+state.py）                    │
        └─ 三条线并行 ─→ T13 可解释性三出口（loop.py+session.py+decision.py+CLI） │
                                    │                                          │
批次 4 ─────────────────────────────┼──────────────────────────────────────────┘
   T14 状态与时序纠错 CLI ─→ T15 旧库迁移与回放 ─┬─ 并行
                                                └─ T16 评测仪器（M1 + M4-T6）
```

依赖理由（逐条）：

- **T3 必须等 T2**：两者都改 `store.py` 的 `_MIGRATIONS` / `SCHEMA_VERSION`（`store.py:211-216`）。版本号必须**严格递增**（`store.py:210` 的注释），并发改同一个元组一定会互相覆盖。
- **T4 → T5 → T6 严格串行**：全都在 `store.py` 的检索路径上（`_match_query`（`store.py:101-108`）/ `search_semantic` 的 `ORDER BY` / 候选池），而且**顺序本身是 §13.11 的定案**——先分词口径、再 bm25、最后才谈嵌入。**颠倒就说不清"这点增益本来该由谁拿到"**（§14.8 第 2 条）。
- **T7 与 T4/T5/T6 可并行**：T7 只碰 `writer.py`/`gate.py`/`distill.py`/`archive.py`，**不直接改 `store.py`**（只调用 `semantic.py` 的写函数）。
- **T8 → T9**：T9 的归约器要用 T8 的时钟接口写 `computed_at`；两者分别是新包里的不同文件，但接口先定。
- **T11 与 T12 可并行**：T11 碰 `rank.py`+`session.py`+`timeline/chain.py`，T12 碰 `loop.py`+`state.py`——**文件不重叠**。
- **T13 必须等 T11 与 T12**：它同时碰 `loop.py`（决策落库与 `--events`）与 `session.py`（渲染分解）。
- **T14 → T15 必须等 T13**：三者都改 `cli/commands/memory.py` + `cli/app.py`（同一个 `_add_memory_command`，`app.py:1036-1074`）。
- **T16 与 T15 可并行**：T16 只碰 `benchmarks/`、`scripts/`、`bench.py`。

---

## 并行与串行：建议的批次派工表

**单写者资源清单（碰了就不能并行）**

| 资源 | 谁碰 | 串行规则 |
|---|---|---|
| `src/spoolkit/memory/store.py`（`_MIGRATIONS` / `SCHEMA_VERSION` / FTS / 排序 SQL） | T2、T3、T4、T5 | **严格串行 T2→T3→T4→T5**；一批里只许一个任务持有它 |
| `src/spoolkit/limits.py`（`KNOBS`） | T1**仅此一次** | T1 之后**只读**；后来的任务若发现缺旋钮 → 回 T1 追加，不许各自追加 |
| `src/spoolkit/agent/loop.py` | T12、T13 | 串行 T12→T13 |
| `src/spoolkit/memory/session.py` | T11、T13 | 串行 T11→T13 |
| `src/spoolkit/cli/commands/memory.py` + `src/spoolkit/cli/app.py` | T13、T14、T15 | 串行 T13→T14→T15 |
| `src/spoolkit/memory/rank.py` | T6、T11 | 串行 T6→T11 |
| `src/spoolkit/memory/timeline/` | T8、T9 | 串行 T8→T9 |
| `src/spoolkit/memory/writer.py` + `gate.py` + `distill.py` + `archive.py` | T7 | 单写；T15 **不许**改 `archive.py` |
| `src/spoolkit/agent/state.py` | T12 | 单写 |

**批次**

| 批次 | 任务 | 并行度 | 为什么这么排 |
|---|---|---|---|
| 1 | **T1 ∥ T2**，然后 **T3** | 2 路并行，T3 收尾 | T1 只动 `limits.py`；T2/T3 同动 `store.py:_MIGRATIONS`，必须串行 |
| 2 | **(T4→T5→T6) ∥ T7 ∥ (T8→T9)** | 3 路并行 | 三条线的文件集合互不相交（见上表）；`store.py` 只被第一条线持有 |
| 3 | **T10 ∥ T11 ∥ T12**，然后 **T13** | 3 路并行，T13 收尾 | `tools.py`+`runtime.py` / `rank.py`+`session.py`+`chain.py` / `loop.py`+`state.py` 三组不重叠；T13 要同时动 `loop.py` 与 `session.py`，必须等 T11/T12 都空出 |
| 4 | **T14 → (T15 ∥ T16)** | T15/T16 并行 | T14 与 T15 都动 `cli/commands/memory.py`；T16 只动 `benchmarks/`+`scripts/`+`bench.py` |

**派工纪律**：同一批里**不许两个任务同时打开 `store.py`**；跨批的任务若发现需要改上一批的文件，**回上一批的重写，不许在下一批顺手改**（那会让"哪条验收对应哪次改动"失配）。

---

### Task 1: `limits` 旋钮一次登记（P1 全部 22 个）

**目标：** 把 P1 需要的全部旋钮**一次性**登记进 `limits.KNOBS`（`limits.py:49`，`CAPABILITY`，可被 `.agent/limits.json` 覆盖，访问器 `limits.resolve`（`limits.py:331`））。之后所有任务**只消费不追加**——`limits.py` 因此可以从所有人的写范围里摘出去，批次 2/3/4 才能真正并行。

**改动面：**

- Modify: `src/spoolkit/limits.py`（在 `KNOBS` 末尾**追加**；既有条目（`limits.py:137` `hot_memory_ratio`、`:140` `task_state_ratio`、`:146` `lessons_ratio`、`:171` `history_search_limit`、`:181` `history_snippet_chars`、`:190` `remember_max_chars`、`:197` `remember_table_lines`、`:233` `hot_max_items`、`:240` `hot_item_chars`、`:247` `hot_importance_long_step`）**一字不改**）
- Test: Modify 既有 `limits` 用例文件（`tests/` 下按 `limits` 搜索现成文件；若只有一个通用文件就加进去），新增 `test_p1_旋钮全部登记且可覆盖`

**旋钮清单（名字 / 默认 / 依据 / 谁消费）：**

| 旋钮 | 默认 | 依据 | 消费 |
|---|---|---|---|
| `w_bm25` | `1.0`（**本计划初值，设计稿未给**） | §14.4 规矩 1 | T5/T6 |
| `w_embed` | `0.0`（定案：不引入） | §13.11 / §13.13 第 6 条 | T6 |
| `rank_w_relevance` | `0.40` | §6.1 权重块 | T6 |
| `rank_w_recency` | `0.20` | §6.1 | T6 |
| `rank_w_importance` | `0.25` | §6.1 | T6 |
| `rank_w_usage` | `0.10` | §6.1 | T6 |
| `rank_w_penalty` | `0.05` | §6.1 | T6 |
| `rank_recency_half_life_days` | `30` | §6.1 | T6 |
| `recall_candidates_n` | `50` | §14.4 规矩 3（设计稿名 `embed_candidates_n`，见「口径裁决 3」） | T6 |
| `recall_rerank_k` | `5` | §14.4 规矩 3（设计稿名 `embed_rerank_k`；硬约束 `≤ recall_limit`） | T6 |
| `recall_limit` | `5` | §6.2 表 | T6/T11 |
| `recall_entry_chars` | `240` | §6.2 表（对齐既有 `history_snippet_chars=240`） | T11 |
| `dedupe_similarity` | `0.90` | §5.3 ④ / §6.1 | T7 |
| `remember_min_chars` | `8` | §4.3 容量上限段 | T7 |
| `retrieval_ratio` | `0.15` | §6.6 表 | T12 |
| `memory_budget_ratio` | `0.30` | §6.2 表 | T12 |
| `event_chain_max_units` | `2` | §6.7 / §6.2 | T11 |
| `event_chain_render_max_chars` | `400` | §6.7 / §6.2 | T11 |
| `state_direct_inject_min_confidence` | `0.8` | §6.6 / §13.13 第 7 条 | T12 |
| `state_direct_inject_max` | `1` | §6.6 | T12 |
| `state_direct_inject_max_age_days` | `30`（**本计划初值，设计稿只写"最近 N 天"**） | §13.13 第 7 条 | T12 |
| `state_auto_pick` | `0`（默认关） | §15.6 第 3 条 | T9 |
| `event_archive_threshold` | `1000`（**待标定初值**） | §13.13 第 5 条 | T3/T9 |

**要点：**

- `note` 必须写清"依据章节"，两个自取默认值（`w_bm25` / `state_direct_inject_max_age_days`）必须在 `note` 里写"**本计划初值，不是设计稿数值**"——否则后来人会把它当设计稿的定案。
- `event_archive_threshold` 的 `note` 写"**待标定初值**：事件增长速率无实测（§13.13 第 5 条 / §7.1 的 `episode` 读数 26 条 18 天不能外推到 event）"。
- **不登记** `semantic_max_per_kind` 的五个上限旋钮，也不登记 `episode_archive_threshold`——它们是 P2（§9.5 P2 ②④）。
- **不登记** `embed_candidates_n` / `embed_rerank_k` / `embed_min_cos` / `embed_timeout_ms` / `embed_chunk_chars`——第③步不落地（§14.4 规矩 3 的四个旋钮属嵌入选型，见「口径裁决 2」）。
- 只追加；不改既有任何一项的默认值与类型。

**验收标准（可证伪）：**

1. 清单里**每个**名字：`limits.knob(name)` 非 `None`、`.kind == limits.CAPABILITY`、`.overridable` 为真。
2. **既有旋钮一个不少**：把改造前 `KNOBS` 的名字集合写成固定元组，断言它是改造后名字集合的子集。
3. `limits.resolve("rank_w_relevance") == (0.40, "默认")`；把 `.agent/limits.json` 写成 `{"rank_w_relevance": 0.5}` 后 `resolve` 返回 `(0.5, "配置")`。
4. `limits.resolve("w_embed") == (0.0, "默认")`。
5. 无重名：`len(limits.KNOBS) == len(limits._BY_NAME)`（`limits.py:324`）。

**证据要求：** `pytest <limits 用例文件> -q`（经 `.\scripts\pytest.ps1` 口径）；打印 `len(limits.KNOBS)`（改造前 vs 改造后）与新增的 22 个名字。

**依赖：** 无。**可与 Task 2 并行**（文件不重叠）。

**风险：** ① 一次加 22 个旋钮、其中若干在 P1 里只用一次 → 用"消费"列逼出归属，收尾时按 Self-Review 第 1 条复查；② 两个自取默认值被后人当成设计稿数值 → `note` 里写明；③ 万一造出重名 `_BY_NAME` 会静默取后者 → 验收 5 钉住。

**怎么证伪：** 把 `w_embed` 的默认改成 `1.0` → 验收 4 红；删掉 `rank_w_penalty` → 验收 1 红；把某个既有旋钮重命名 → 验收 2 红。

- [ ] 先写"清单全登记 + 既有不丢 + 无重名"三条用例（红）
- [ ] 追加 22 个 `Knob(...)`，`note` 写清依据与"本计划初值"
- [ ] 跑局部测试转绿，打印前后名字集合

---

### Task 2: schema v3 —— `semantic` + `semantic_fts` + 四张账本表（`user_version` 2→3）

**目标：** 按 §4.3 建新表：`semantic`（含 `content_hash` 的**唯一部分索引**、`evidence`、`scope`/`scope_id`、`status`/`superseded_by`）、`semantic_fts(text,trigger,evidence)`、`memory_link`、`memory_write`、`memory_audit`、`memory_decision`；`memory` 与 `memory_fts` **一个字节不动**（决议 7）；重跑幂等；旧库升级后旧代码照读。

**改动面：**

- Modify: `src/spoolkit/memory/store.py`（`_MIGRATIONS`（`store.py:211-214`）追加 `(3, "建 semantic 与四张账本表", _migrate_to_v3)`；`SCHEMA_VERSION`（`store.py:216`）自动跟到 3；新增 `_migrate_to_v3`，沿用 `_add_column_if_missing`（`store.py:151-158`）与 `_apply_step`（`store.py:219-240`）的失败语义）
- Create: `src/spoolkit/memory/semantic.py`（`Semantic` 行类型；`SEMANTIC_STATUSES`；`insert_semantic` / `touch_semantic` / `get_semantic` / `list_semantic` / `search_semantic`（本任务只给"查得到"的最简版，排序留给 T4/T5/T6））
- Test: Create `tests/memory/test_schema_semantic.py`

**要点：**

- DDL 逐字照 §4.3 第 1、2、4、5、6、7 块，**唯一两处偏差**：① `semantic_fts` 不用触发器（「口径裁决 1」）；② `memory_decision` **一次建就带 `embed_status` 列**（`TEXT NOT NULL DEFAULT 'off:disabled'`，§14.4 规矩 6 的出口①）。
- **去重索引必须是"部分"唯一索引**：`CREATE UNIQUE INDEX idx_semantic_dedupe ON semantic(content_hash) WHERE status='active'`（`§4.3:360-361`）。写成普通唯一索引会让 `superseded` / `archived` 的旧条目**永久挡住**同类新条目——这正是 §5.3 ⑤ 冲突链要的反面。
- `semantic_fts` 走**应用层显式写索引**：`insert_semantic` 一处写主表 + 索引（形制照 `insert_memory`（`store.py:281-319`）与 `_fts_values`（`store.py:174-176`））；配用例 `test_semantic_fts_不用触发器`（查 `sqlite_master` 里 `tbl_name='semantic' AND type='trigger'` 的行数为 0，与 `tests/memory/test_schema_version.py:246` 同形）。
- 索引三列 `text`/`trigger`/`evidence`（§4.3:364-366；`evidence` 进 FTS 是 §14.4 规矩 2 明确要求的）；`kind`/`scope`/`status` **不进 FTS**，按列过滤走 SQL 索引。
- **本任务不许顺手换分词口径**：仍然逐字拆（`normalize_for_fts`，`store.py:96-98`）；bigram 是 T4 的事。
- `SEMANTIC_STATUSES = ("active","archived","superseded","needs_review")`（「口径裁决 5」），并在模块里写清第四个取值是 P1 为迁移补的。
- 前向兼容沿用 `init_memory_schema`（`store.py:243-257`）：库里版本高于代码 → 什么都不做、不报错、不写回低值。

**验收标准（可证伪）：**

1. 空库 → `init_memory_schema` 后 `PRAGMA user_version == 3`，`sqlite_master` 里存在 `semantic` / `semantic_fts` / `memory_link` / `memory_write` / `memory_audit` / `memory_decision` 六个名字与 §4.3 列出的全部索引。
2. 旧库 fixture（P0 结构、`user_version=2`、`memory` 与 `episode` 各有数据）→ 升级后：`memory` 的建表 SQL **逐字不变**（同 `tests/memory/test_schema_version.py:167` 的口径）、`episode` 行数不变、`user_version=3`。
3. 幂等：连跑两次 → 表/索引集合与 `user_version` 均不变。
4. **部分索引生效**：先插一条 `status='active'` + `content_hash='h'`；第二条同 hash 的 `active` → `sqlite3.IntegrityError`；把第一条改成 `superseded`（或 `archived`）→ 第二条同 hash 可写入。
5. `test_semantic_fts_不用触发器`：`SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='semantic'` == 0。
6. 前向兼容：`PRAGMA user_version=4` → 再跑不报错、版本号不被写回（同 `tests/memory/test_schema_version.py:196` 的口径）。
7. 升级失败给人话：monkeypatch 让 `_migrate_to_v3` 抛异常 → `MemorySchemaError`、消息含"第 3 步"、`user_version` **没被记上**（同 `store.py:219-240` 的语义）。

**证据要求：**

- `pytest tests/memory/test_schema_semantic.py tests/memory/test_schema_version.py -q`。
- 在**真实 `memory.db` 的只读副本**上跑一次升级：`Copy-Item D:\workSpace\agentWorkSpace\.agent\memory.db .tmp\p1-schema-probe.db`（副本落在**本仓** `.tmp\`，原库不动）→ 打印 `PRAGMA user_version`、六张表的 `table_info`、`memory` 表行数前后一致。⚠️ **不许在原库上跑**。

**依赖：** 无（与 T1 并行）。**T3 必须等它**（同改 `store.py:_MIGRATIONS`）；T4/T5/T6/T7/T9 都依赖它。

**风险：** ① 一次建六张表，DDL 抄错一处就长期错 → 用例逐名字逐列比对；② 部分唯一索引若写成普通唯一索引，`superseded` 链会被挡住（验收 4 就是钉这条）；③ 建表用 `IF NOT EXISTS` 混在一步里，中途失败靠 `_apply_step` 回滚（`store.py:234-240`）——**不许**把 `PRAGMA user_version` 挪到步骤之外去改；④ `semantic` 是**新表**，`memory` 的"结构一个字不改"这条老规矩（`plans/2026-09-24-workspace-session-kernel.md:1156`）本轮不能破。

**怎么证伪：** 把 `_migrate_to_v3` 从 `_MIGRATIONS` 里删掉 → 验收 1 红；把 `idx_semantic_dedupe` 的 `WHERE status='active'` 去掉 → 验收 4 红；给 `semantic` 加 FTS 触发器 → 验收 5 红。

- [ ] 先写"六表就位 / 旧库升级 / 部分索引 / 无触发器 / 前向兼容 / 失败给人话"六条用例（红）
- [ ] 实现 `_migrate_to_v3` 与 `semantic.py`
- [ ] 跑局部测试转绿，在 `memory.db` 副本上复验，留下输出

---

### Task 3: schema v4 —— 状态与时序六表（`user_version` 3→4）

**目标：** 按 §15.1 / §15.2 / §15.7 / §13.13 第 5 条建六张表：`memory_event`（§15.1 字段逐条）、`memory_state`（`(entity, attribute)` 联合主键 + `derived_from` / `reducer` / `valid_from` / `valid_to` / `superseded_by` / `status` / `reason` / `computed_at` / `is_derived`）、`entity_attribute`（注册表）、`entity_alias`、`attribute_alias`、`memory_event_archive`（归档表）。**五张新表与 §4.3 的六张同库、同迁移路径**（§15.7「表与迁移」）。

**改动面：**

- Modify: `src/spoolkit/memory/store.py`（`_MIGRATIONS` 追加 `(4, "建状态与时序六表", _migrate_to_v4)`）
- Create: `src/spoolkit/memory/timeline/models.py`（`Event` / `StateRow` / `AttributeSpec` 行类型 + 取值枚举 + `FIELD_SPECS` 常量；DDL 仍在 `store.py`）
- Test: Create `tests/memory/test_schema_timeline.py`

**要点：**

- `memory_state` 主键是 **`(entity, attribute)` 联合主键**（§15.1）；`is_derived` **DDL 默认 1**（§15.1 原话："DDL 默认 1，恒为真"）——它不是装饰：让"这是推导值"**在数据里可查**。
- **金额不许浮点**（§15.3 第 4 条）：`value` 用 `TEXT`（整数分或 `Decimal` 序列化字符串），**不建 REAL 列**；比较用精确相等，输出带 `unit`。
- `memory_event` 的必带字段按 §15.1 表逐条：`id`/`entity`/`attribute`/`op`/`value`/`unit`/`at_abs`/`at_source`/`time_precision`/`source_ref`/`raw_text`/`speaker`/`confidence`/`content_hash`/`status`；外加 §15.5 硬约束 3 要求的 **`tz_offset_min`（必填）** 与 `tz_name`（可空）。
- `memory_event` **没有 DELETE 路径**（§15.4）：`status` 只允许 `active` / `retracted`。
- **`memory_state` 不设 `scope` 列**（「口径裁决 6」/§13.13 第 8 条）。
- 索引至少：`(entity, attribute, at_abs)`、`(entity, attribute, status)`、`content_hash`、`at_abs`、`alias`（在两张别名表上是**唯一**索引）。
- 归档表与 `event_archive_threshold`（T1 已登记）同批就位，**本任务不写归档逻辑**（写的是 T9 的"超限搬最旧"）。

**验收标准（可证伪）：**

1. 升级后 `PRAGMA user_version == 4`；六张表在 `sqlite_master` 里；`PRAGMA table_info(memory_state)` 的列清单与 `FIELD_SPECS` 逐字一致，且 `is_derived` 的默认值是 1。
2. 幂等：连跑两次结构与行数不变。
3. `memory_event.status` 的合法取值只有 `active`/`retracted`（`models.py` 的枚举常量 + 用例断言两者相等）。
4. **前三步的表一字未动**：`memory` / `semantic` / `episode` 的建表 SQL 与升级前逐字一致。
5. 联合主键生效：插两条同 `(entity, attribute)` 的 `memory_state` → 第二条 `IntegrityError`（取代链走"插新行 + 改旧行 `valid_to`"，不是插第二行同键）。
6. 前向兼容：`user_version=5` → 不执行、不报错。

**证据要求：** `pytest tests/memory/test_schema_timeline.py tests/memory/test_schema_semantic.py -q`；在**副本**上打印 `table_info(memory_event)` / `table_info(memory_state)` 与 `user_version`。

**依赖：** Task 2（`store.py` 串行）。

**风险：** ① `at_abs` 用 `REAL` 还是 `INTEGER`（epoch 秒）必须先定死：本计划用 **`REAL` 存 epoch 秒 + `INTEGER tz_offset_min`**，顺序与精度判定都按 `at_abs` 升序；② 别名表的唯一索引是 §15.2「一个 alias 只能指一个 canonical」的**机制**，写成普通索引等于没实现；③ 六张表里有五张在 P1 只被 CLI 与归约器用，**不许提前写 P2 的归档/衰减列**。

**怎么证伪：** 把 `is_derived` 的 `DEFAULT 1` 去掉 → 验收 1 红；把别名表的唯一索引改成普通索引 → 验收里那条"同一 alias 指两个 canonical 报错"红；删掉 `tz_offset_min` → 验收 1 的列清单红。

- [ ] 先写"六表就位 / 幂等 / 联合主键 / 别名唯一 / 前三步不动"五条用例（红）
- [ ] 实现 `_migrate_to_v4` 与 `timeline/models.py`
- [ ] 跑局部测试转绿，在副本上复验

---

### Task 4: ① 中文分词口径换双字 shingle + M2' 三臂对照仪器

**目标：** 检索侧从**逐字 OR**（`store.py:96-108` 的 `normalize_for_fts` + `_match_query`）换成 §14.8 方案 A 的**相邻双字 shingle**（零依赖、纯 SQLite、FTS5 与 `bm25()` 照用）；并把 M2' 的**夹具与仪器固化进仓**——它现在只存在于 `.tmp\embed-bench\`，而 `.gitignore:22` 忽略了 `.tmp/`，**随时会没**。

**改动面：**

- Modify: `src/spoolkit/memory/store.py`（新增 bigram 规范化函数；`semantic_fts` 的写入与查询走 bigram；**`memory_fts` 的逐字口径本任务不动**）
- Create: `benchmarks/memory/corpus.jsonl`（16 条语料 + 分组 G1–G4，**逐字照抄** `.tmp/embed-bench/bench_common.py:15-32`）
- Create: `benchmarks/memory/queries.jsonl`（13 条查询 + 相关 id 集合 + note，**逐字照抄** `bench_common.py:46-61`）
- Create: `scripts/memory_relevance.py`（三臂：`store-char`（现状逐字）/ `bigram`（本任务）/ `bigram+bm25`（T5 才有读数）；指标 `p@5`/`r@5`/`top1`，定义照 `bench_common.py:70-91`）
- Test: Create `tests/memory/test_bigram.py`、`tests/memory/test_relevance_harness.py`

**要点：**

- **顺序不许颠倒**（§13.11）：本任务只改**分词口径**，**不许动 `ORDER BY`**。bm25 是 T5——证据必须能分开：本任务结束时排序仍是现状形态，只有召回那两项可能变化。
- 双字口径的三条边界必须处理：① **单字查询**（"归"）无 bigram 可切 → **回退到单字匹配**，否则"归"的召回直接归零（它正是 §14.8 点名的"粒度"案例）；② 长度 1 的条目同理；③ **不许跨标点/停用字造双字**（"的任"、"档，"这类），按标点切开。
- 夹具**必须逐字固化**，不许顺手"改好一点"——改了基线就不可比。固化后的 `test_relevance_harness.py` 要断言读出来的 16 条文本与 13 条查询**与 `.tmp/embed-bench/bench_common.py` 一致**（若那个临时文件还在；不在就以固化版为准，并在任务报告里写明这一点）。
- 基线读数**已实测、可核对**（在 `.tmp/embed-bench/results_fts5_store.json` 的 `summary`）：`precision_at_5=0.3384615384615385`、`recall_at_5=0.5961538461538461`、`top1_accuracy=0.7692307692307693`。**复制这份读数进仓**（例如 `benchmarks/memory/baseline_fts5_store.json`）作为回归锚。
- 接受判据（§14.8「① 要跑什么才算数」第 4 条，已定案）：**bigram 臂的 P@5 与 R@5 都不低于现状逐字臂**。
- ⚠️ 设计稿 §14.8 同时要求"语料 ≥200 条"；现在只有 16 条（分组查询的 P@5 上限只有 0.8，§14.3.7 第 3 条明说**绝对值不可外推**）——本任务只承诺"**相对排序不低于基线**"，**不许**把 16 条上的读数写成"分词口径已达标"。
- 方案 B（另建"已分词检索列"、引分词器）**本任务不实现**：仪器里留一个 `--arm tokenized` 桩，打印 `skipped: P1 不引依赖（§13.13 第 1 条）`。

**验收标准（可证伪）：**

1. `bigram("归档旧的任务记录")` 的 token 序列逐字等于 `归档 档旧 旧的 的任 任务 务记 记录`；`bigram("归档") == "归档"`。
2. 单字查询不回归：查 `"归"` 仍能命中 `归档旧的任务记录`（走回退路径）。
3. 不造假双字：`bigram("归档，删除")` 不含 `"档，"` / `"，删"` 这类跨边界 token。
4. **仪器可信**：`python scripts/memory_relevance.py --arm store-char` 打印的 P@5/R@5/top1 与 `results_fts5_store.json` 的 `summary` **逐位相同**（这是"夹具抄对了"的证明）。
5. **判据有读数**：`--arm bigram` 打印另一组读数并给出一行结论（`P@5 ≥ 基线? / R@5 ≥ 基线?`）。
6. **判据可证伪**：把 bigram 口径改回逐字 → `--arm bigram` 的读数与 `--arm store-char` **完全相同**，结论行报"未跑出差异"（红）。

**证据要求：** `pytest tests/memory/test_bigram.py tests/memory/test_relevance_harness.py -q`；**贴出 `store-char` 与 `bigram` 两份完整输出**；并如实标注"16 条语料上的相对读数，绝对数字不可外推（§14.3.7 第 3 条）"。

**依赖：** Task 2（`semantic` + `semantic_fts`）；在 T3 之后做（同改 `store.py`）。

**风险：** ① 索引 token 数约 `n−1`（条目 ≤ `remember_max_chars=400`，量级可接受但**未实测**）；② 假双字带来的噪声量级**未实测**（§14.8 取舍列点名）；③ **若 bigram 的 P@5/R@5 反而低于基线** → **不许调夹具或标签凑数**，如实报告，并把"是否改用方案 B（引分词器）"**升回用户拍板**（§13.13 第 1 条的定案前提就是"A 与 B 的对照读数"，没有读数时方案 A 只是建议）。

**怎么证伪：** 把语义路径的 `_match_query` 改回逐字 OR → 验收 1/6 红；删掉单字回退 → 验收 2 红；把标点切开去掉 → 验收 3 红。

- [ ] 先把夹具与基线读数固化进仓，写 `test_relevance_harness.py`（红：仓里还没有这套文件）
- [ ] 写 `test_bigram.py`（红）
- [ ] 实现 bigram 口径与回退，接进 `semantic_fts` 写入/查询
- [ ] 跑三臂，贴读数，如实写结论（含"16 条不可外推"）

---

### Task 5: ② `bm25()` 排序（`semantic` 检索）

**目标：** `search_semantic` 从"命中即返回 + Python 侧频次/新近打分"换成 SQLite **内置** `bm25()` 排序（§14.8 第②步），并把**原始 `bm25()` 值**一并返回——§6.3 要求 `why` 把原始值与归一化分量**两个都打**（只打分量就分不清是"排序错"还是"归一化错"）。

**改动面：**

- Modify: `src/spoolkit/memory/store.py`（`search_semantic` 的 SQL 加 `ORDER BY bm25(semantic_fts)`；返回 `(row, bm25_raw)`）
- Modify: `scripts/memory_relevance.py`（启用 `--arm bigram+bm25` 臂）
- Test: Create `tests/memory/test_semantic_search.py`

**要点：**

- **`bm25()` 的符号约定必须先实测确认，本计划不假设**：SQLite 的 `bm25()` 返回值**越小越相关**（通常是负数），而 §6.1 写的是 `rel = 1 - exp(-bm25)`（`§6.1:638`）。Task 的第一步是写一条**把原始值与排序方向钉住**的用例（先红后绿），据此定义 `bm25_norm`——**取 `-bm25()` 还是 `bm25()` 由这条用例决定，不许照抄公式猜符号**。
- **只改排序、不改分词**（第①步已在 T4 做完）；判据是 §14.8 第②步的实测形状：**top1 上升、P@5/R@5 不变**——用 T4 的仪器跑 `--arm bigram+bm25` 并贴读数。
- `memory` 表的 `_rank`（`store.py:394-404`）与 `FREQUENCY_WEIGHT`/`RECENCY_WEIGHT`（`store.py:390-391`）**本任务一个字节不动**：旧表旧口径照旧（决议 7 的只读精神 + 不扩大改动面）。新旧两套在同一文件里并存，**不是**替换。
- 旧行为可分辨的锚（**逐字口径下的既有实测**，可核对）：查 `"存档"` → 现状逐字臂排 `[1,2]`、bm25 臂排 `[2,1]`；查 `"依赖管理"` → 现状 `top1=0`、bm25 `top1=1`；13 条查询的 `top1_accuracy` 从 `0.7692307692307693` 升到 `1.0`（`.tmp/embed-bench/results_fts5_store.json` vs `results_fts5_bm25.json`）。⚠️ **这两份是"逐字口径"下的读数**；**bigram 口径下的 bm25 读数本轮没有**——T5 的 `--arm bigram+bm25` 是**新读数**，不许把它写成"复现了 §14.3.4"。

**验收标准（可证伪）：**

1. `bm25()` 原始值与方向用例（先红后绿）：同两份语料 + 一个只出现在其中一条里的词 → 该条排第一，且打印出的原始值与断言的大小关系一致（把符号搞反 → 红）。
2. `--arm bigram+bm25` 的 **P@5/R@5 与 `--arm bigram` 逐位相同**（§14.8 第②步"只动排序不动召回"），且 top1 不下降。
3. 旧路径不回归：`tests/memory/test_recall_ranking.py` 全绿，`FREQUENCY_WEIGHT`/`RECENCY_WEIGHT` 仍在且值不变。
4. `search_semantic` 返回的每行都带 `bm25_raw`（用一个非空断言 + 一个"两条不同相关度 → 两个不同原始值"的断言钉住）。

**证据要求：** `pytest tests/memory/test_semantic_search.py tests/memory/test_recall_ranking.py -q`；贴出 `--arm bigram` 与 `--arm bigram+bm25` 两份读数；贴出 `bm25()` 原始值的符号实测输出。

**依赖：** Task 4。**T6 依赖它。**

**风险：** ① 符号约定猜错会让**整个排序反向**且用例看不出来（所以第 1 条验收是硬前置）；② `bm25()` 是 `semantic_fts` 的函数，若 T4 的 bigram 索引把 token 数量级抬高，`bm25` 的绝对值会变（**相对排序**才是判据）。

**怎么证伪：** 把 `ORDER BY bm25(...)` 删掉（退回插入序/ID 序）→ 验收 2 的 top1 断言红；把 `-bm25()` 与 `bm25()` 对调 → 验收 1 红。

- [ ] 先写"符号与方向"用例（红）
- [ ] 加 `ORDER BY bm25(...)` 并返回原始值
- [ ] 跑两臂仪器，贴读数；跑旧路径用例确认不回归

---

### Task 6: 五维可解释排序 + 两阶段候选→重排（`rank.py`，纯函数）

**目标：** 按 §6.1 实现 `score = w_rel*rel + w_rec*rec + w_imp*imp + w_use*use − w_pen*penalty`，**每一维都能单独打出来**；按 §14.4 规矩 3 实现两阶段：候选池 `recall_candidates_n=50` → 合并去重 → 五维重排 → 取 `recall_rerank_k`（默认 = `recall_limit=5`）；**硬约束：`K ≤ recall_limit`，且重排只能减少进上下文的条数、不能增加**。

**改动面：**

- Create: `src/spoolkit/memory/rank.py`（`Candidate` / `Scored` / `Dropped` / `RankedResult`；`rank(candidates, *, now, overrides) -> RankedResult`；`Scored.parts: dict[str,float]`）
- Test: Create `tests/memory/test_rank.py`
- **不碰 `session.py` / `loop.py`**：接线是 T11/T13 的事——这是本任务能与 T4/T5 之外的东西并行的原因。

**要点：**

- 五个维度的定义逐条照 §6.1 表：`rel = 1 - exp(-bm25_norm)`；`rec = exp(-age_days / half_life_days)`（`age_days` 基于 `last_used_at or created_at`，**用哪一天必须带在 `parts` 里**）；`imp` 写入时判定 + 用户可改（用户改过的在输出里标 `(user)`，不许数字裸奔）；`use = log1p(use_count) / log1p(max_use)`（`max_use` 取候选集内最大值，**除零要有保护**）；`penalty = applied_count / (use_count + 1)`。
- `imp` 基线：`fact`/`decision` 0.6、`preference` 0.7、`lesson`/`skill` 0.5；"+0.1"按「口径裁决 8」的解释实现（**成功取代过别人**或**通过验证**的 +0.1），并在任务报告里标注这是对歧义表述的解释。
- 权重与半衰期**全部从 `limits.resolve` 取**（T1 已登记），**一个都不写死**。
- `dropped` 的 reason 枚举必须是 §6.3 那一套：`below_k` / `over_budget` / `demoted` / `superseded` / `conflict`。
- 候选池是**内存里**过一遍的，**不渲染进 prompt**（§6.2 表："重排候选池（不进上下文）… **0 token**"）。
- **本任务不实现**：事件链单元（T11）、token 预算与让位（T12）、`memory_decision` 落库（T13）。

**验收标准（可证伪）：**

1. 维度真的在起作用：同一批候选，`rank_w_relevance=0` + `rank_w_usage=1` → 顺序变成"用得多在前"；**所有权重置 0** → 顺序 = 输入顺序（稳定排序）。
2. `parts` 能加回总分：对任一 `chosen`，`Σ weights[k]*parts[k] == score`（浮点容差 1e-9）。
3. 两阶段：造 60 条候选 + `recall_candidates_n=50` → 只有 50 条进重排；`recall_rerank_k=5` → `chosen` 5 条、`dropped` 45 条且**每条都有 reason**。
4. 硬约束：`recall_rerank_k=10` 而 `recall_limit=5` → 按钉住的行为处理（拒绝或夹到 5 并写 reason），用例固定其中一种。
5. 旋钮可覆盖：`.agent/limits.json` 覆盖 `rank_recency_half_life_days=7` → `parts["rec"]` 可观测地变化。
6. 边界：候选为空 → 空结果不抛；`max_use == 0` → 不除零；`use_count=0` 时 `penalty` 有定义。

**证据要求：** `pytest tests/memory/test_rank.py -q`；贴出至少一条 `Scored.parts` 的原始输出（五个分量 + 总分）。

**依赖：** Task 1（旋钮）、Task 2（行字段，`importance`/`use_count`/`applied_count`）。

**风险：** ① `1 - exp(-bm25_norm)` 的 `bm25_norm` 定义跨任务（T5 给原始值）——**必须由 T5 的符号用例定死**，本任务不许自带一套归一化；② 权重默认值是设计稿给的，但**它们合起来是否合理没实测**（真实语料上的排序变化属 §9.5 P1 风险列的"既有用户看到的 `recall` 顺序会变"，缓解手段是权重可覆盖 + T13 的 `why` 可见）。

**怎么证伪：** 把 `use` 从 `log1p` 归一化改回 `use_count * 0.6` 的线性乘 → 验收 1 的用例（一条 `use_count=1` 的新条目 vs 一条 `use_count=0` 的老条目，断言老条目仍可能胜出）红；把 `recall_rerank_k` 的夹取去掉 → 验收 4 红。

- [ ] 先写"权重可分辨 / parts 可加 / 两阶段 / 硬约束 / 边界"五条用例（红）
- [ ] 实现 `rank.py` 纯函数
- [ ] 跑局部测试转绿，贴一条 `parts` 原始输出

---

### Task 7: 五道门禁落到 `semantic`（脱敏 / J1–J5 / 幂等 / 去重 / 冲突）

**目标：** 把 §5.3 的五道门禁做成**一处写入口** `write_memory(request) -> WriteResult`——所有写入路径（自动蒸馏、`remember` 工具、CLI、迁移）都必须经过它；**J2（可核对）在 P1 落地**（P0 明确没做：`gate.py:200-205` 的 `RULES` 里只有 J1/J3/J4/J5，P0 计划的「范围裁决 4」点名 J2 属 P1）。

**改动面：**

- Create: `src/spoolkit/memory/writer.py`（`WriteRequest`（kind/text/evidence/trigger/scope/source/round_key/…）、`WriteResult`（`written: bool` / `ref: str` / `skipped: str` / `reason: str`）、`write_memory(conn, request, *, clock, overrides)`）
- Modify: `src/spoolkit/memory/gate.py`（`RULES`（`gate.py:200-205`）追加 `Rule("J2", "可核对", _j2)`；`judge`（`gate.py:210-235`）增 `evidence` 参数；`RULES_BY_ID`（`gate.py:207`）自动跟上；**J1/J3/J4/J5 的规则表与 `normalize_key`/`content_hash`/`sanitize`（`gate.py:350`/`:389`/`:407`）不改**）
- Modify: `src/spoolkit/memory/distill.py`（`DISTILL_SCHEMA`（`distill.py:36-53`）把 `evidence` 加为**必填**；`_parse_entries`（`distill.py:118`）解析它）
- Modify: `src/spoolkit/memory/archive.py`（通过判据的条目改走 `writer.write_memory`；`ArchiveResult`（`archive.py:40-51`）增 `written`，并写清"**P1 起单写 `semantic`**，旧 `memory` 表冻结"）
- Test: Create `tests/memory/test_writer.py`；Modify `tests/memory/test_gate.py`、`tests/memory/test_distill.py`、`tests/memory/test_archive.py`

**要点：**

- **门禁顺序写死**（§5.3 的流程图）：① 脱敏（**只在写入侧一处**调用 `gate.sanitize`（`gate.py:407`），不是每个调用点各写一遍）→ ② 判据 J1–J5 → ③ 幂等 `round_key = sha1(session_id|task_id|goal|final|step)`（**用 `memory_write` 的唯一索引挡，不许先查后写**——先查后写有竞态，且"漏查"这类 bug 不报错）→ ④ 去重（精确 `content_hash` 命中 → `touch` 既有 + `skipped='duplicate'`；近似 → 见下）→ ⑤ 冲突。
- **近似去重**（§5.3 ④ 第 2 级）：规范化后编辑距离 / Jaccard ≥ `dedupe_similarity=0.9` → 保留**较早的 `created_at`** 与**较高的 `confidence`**、`text` 取更完整的那一份、写 `memory_link(relation='duplicate_of')`、记 audit。**第 2 级不引嵌入**（零新依赖在 P1 仍是基线，§4.3 的注）；**同义改写抓不到这件事必须写进模块注释与 `--explain` 文案**（"不能假装它等同语义去重"）。
- **冲突**（§5.3 ⑤）：同 `kind` + 主题键（`trigger` 与 `text` 前 N 个字符规范化后的交集非空）+ 文本规范化后**不同** + 两者都非 `archived` → 潜在冲突 → 新条 `active`、旧条 `superseded` + `superseded_by` + `memory_link(relation='supersedes')`；**旧条不删**（`show` 仍能看到"我们曾经这么认为"）。判定由一次**独立上下文的判定调用**给出；**判定失败 → 默认保留两条 `active` 并标 `needs_review`**（§5.3 ⑤ 原话："静默丢一条比留着两条更危险"）。
- **判据不过 = 降级为 `episode`，不是丢弃**（§5.3 ②）：降级出口沿用 P0 的下沉路径（`archive.py:149-190`），并写 `memory_write.skipped='denied'` + `memory_audit(action='add', reason=J#…)`。
- **J2 的两条机械检查**（§5.1）：`evidence` 非空**且**能指到工作区内的文件/命令/工具输出（路径落在工作区内，或匹配 `工具名\(` / 命令前缀的形状）；`evidence` 里出现工作区外的绝对路径 → 只保留文件名与相对位置（§5.3 ⑥）。
- **单写语义**（§9.4「P1 起单写」）：`write_memory` 成功后**不写旧 `memory` 表**；但要有一条用例证明旧表仍可读、旧代码路径（`search_memories`）仍能跑。
- 函数级约束：`write_memory` 是**唯一**写 `semantic` 的地方（`semantic.insert_semantic` 只被它调用；用一条源码级用例或私有名检查钉住）。

**验收标准（可证伪）：**

1. **J2 落地**：`evidence=""` 的 `fact` → 拒（`Verdict(False, "J2 …")`）、`memory_write.skipped='denied'`、`memory_audit` 有一行；`evidence="src/spoolkit/memory/store.py:105"` → 收。
2. **幂等**：同一 `round_key` + 同一 `source` 写两次 → `memory_write` 一行、`semantic` 一条；另有一条**绕过应用层直接插第二条**的用例 → `IntegrityError`（证明是唯一索引在挡，不是应用层自觉）。
3. **精确去重**：同文本第二次写 → 不新增、既有行 `use_count` +1、`skipped='duplicate'`。
4. **近似去重**：`"用 uv 管理依赖"` vs `"用uv管理依赖。"` ≥ 0.9 → 合并成一条 + `memory_link(relation='duplicate_of')`；而 `"用 uv 管理依赖"` vs `"本仓统一用 uv"` → **不合并**（并有一条用例断言"这两条不合并"是**已知代价**而不是 bug）。
5. **冲突不静默**：两条互斥 `fact`（同 kind、主题键交集非空、规范化后不同）→ 旧条 `superseded` + `superseded_by` + `memory_link(relation='supersedes')`、**旧条仍可查**；判定失败路径 → 两条都 `active` + `needs_review`。
6. **脱敏在写入侧一处**：`text` 里放密钥形状的哨兵 → `semantic` 里查不到该值、`memory_write.skipped='redacted'`、报告里只有形状名**不回显值**。
7. **单写**：`write_memory` 成功后 `SELECT count(*) FROM memory` 不变；`search_memories` 仍能查到旧行。
8. **蒸馏 schema**：模型返回的条目缺 `evidence` → 该条**不入 `semantic`**（降级或拒绝），既有 `distill` 用例按新契约改写后全绿。

**证据要求：** `pytest tests/memory/test_writer.py tests/memory/test_gate.py tests/memory/test_distill.py tests/memory/test_archive.py -q`；贴出五道门禁各一条的 `WriteResult` 原文（含 `skipped`/`reason`）；贴出 J2 正反例表（各 ≥3 条）。

**依赖：** Task 2。**可与 T4/T5/T6 并行**（只调用 `semantic.py`，不直接改 `store.py`）。

**风险：** ① J2 的"路径落在工作区内"这条过严会误杀（例如命令形式的出处）→ 用正例表兜住（P0 的 J4 已经踩过一次"规则过宽"的坑）；② 近似去重引入一个**新的可失败点**（相似度算法没有依赖但需要标定）→ 阈值进 `limits`（T1 已登记）；③ 冲突判定要一次额外模型调用 → **P1 只在 `remember` 与蒸馏路径上做**，`CLI add` 与迁移**不做**（避免在迁移时烧模型），这条要在任务报告里写明。

**怎么证伪：** 从 `RULES` 里删掉 J2 → 验收 1 红；把幂等改成"先 SELECT 再 INSERT" → 验收 2 的直接插入路径红；把冲突改成"静默留新删旧" → 验收 5 红；让 `write_memory` 顺手写旧 `memory` 表 → 验收 7 红。

- [ ] 先写 J2 正反例 + 幂等直接插入 + 冲突不静默 + 脱敏在写入侧（红）
- [ ] 实现 `writer.py`、`gate.py` 的 J2、`distill.py` 的必填 `evidence`
- [ ] 把 `archive.py` 接到新写入口，改写受影响的既有用例
- [ ] 跑局部测试转绿，贴五道门禁的 `WriteResult` 原文

---

### Task 8: 可注入时钟 + 相对时间解析（§15.5 / §13.13 第 2、3 条）

**目标：** 落地已定案的**可注入时钟**（生产用真实时钟、**测试可伪造**，注入点在**会话级参数**上，§13.13 第 2 条选 A——理由是硬性的：不可注入则 M4-T1 / M4-T6 无法自动化）；落地 §15.5 的四步解析顺序（绝对时间 / 相对词 / 没提时间 / 解析失败），失败落 `time_precision='unknown'`，**不猜、不阻塞写入、不反问用户、不拒收**（§13.13 第 3 条选 A，C 不做）。

**改动面：**

- Create: `src/spoolkit/memory/timeline/__init__.py`
- Create: `src/spoolkit/memory/timeline/clock.py`（`Clock` 协议：`now() -> float`（epoch 秒）+ `tz_offset_min() -> int` + `tz_name() -> str | None`；`SystemClock`；`FakeClock(at_abs, tz_offset_min=480, tz_name="Asia/Shanghai")` 带 `advance()`）
- Create: `src/spoolkit/memory/timeline/parse.py`（`TimeResult`（`at_abs` / `tz_offset_min` / `tz_name` / `at_source` / `time_precision`）；`parse_time(text, *, clock, session_started_at, turn_at)`）
- Test: Create `tests/memory/test_timeline_clock.py`、`tests/memory/test_timeline_parse.py`

**要点：**

- **禁止在归约/解析时现取 `datetime.now()`**（§15.5 硬约束 1）：会话当前时间必须来自注入的时钟（`session.started_at` + 本轮 turn 时间）。配一条**源码级用例** `test_新路径没有裸datetime_now`：`timeline/` 里不许出现 `datetime.now(` / `time.time(` —— **白名单只有 `clock.py` 的 `SystemClock` 一处**。
- `at_abs` 存 epoch；**同时存 `tz_offset_min`（必填）与 `tz_name`（可空）**；**日界线按 `tz_offset_min` 算，不许按 UTC 算**（§15.5 硬约束 3——否则"今天花了 20"在 UTC 下会跨到前一天）。
- 相对词至少覆盖 `今天` / `昨天` / `前天` / `刚才` / `上周`；`time_precision` 取 `day`（日期级）或 `minute`（"刚才"级），逐条断言。
- **歧义不猜**：跨年（"10 月 3 日"没给年）、跨时区、会话跨午夜（"昨天"指的是哪一天）→ `at_abs = None` + `at_source='unknown'` + `time_precision='unknown'`。**不阻塞写入**：句子照样进 `event`（原始观察不可丢），只是**不能当"精确时刻"用**（不参与 §15.6 第 3 条的"同刻冲突"判定）。
- 解析失败的渲染表现（与 §6.7 的单元渲染对齐）：链上这类事件**排在已解析事件之后**、**仍进证据链**、渲染带 `?` 标记（`event:5 时间未知「前几天花了 20」`）——本任务只提供 `TimeResult`，渲染在 T11。

**验收标准（可证伪）：**

1. **时钟真的被用**：同一份文本 `parse_time("昨天", …)` 在 `FakeClock(2026-10-03 10:00 +08:00)` 下得到 `2026-10-02` 的 `at_abs`，在 `FakeClock(2026-10-05 10:00 +08:00)` 下得到 `2026-10-04` —— **同一文本、两个时钟、两个结果**。
2. 四步映射逐条：`explicit` / `relative` / `turn` / `unknown` 各一条，且 `time_precision` 与 §15.5 表逐条一致。
3. 歧义不猜：`"10 月 3 日"` 无年 → `at_abs is None`、`at_source='unknown'`、`time_precision='unknown'`；**不抛异常、不返回猜测值**。
4. 时区按 `tz_offset_min` 算日界：`"今天"` 在 `+08:00` 与在 `UTC`（`tz_offset_min=0`）两个时钟下 `at_abs` 差 8 小时；跨午夜的那条观测落到不同 `day`。
5. 源码级：`timeline/` 里除 `SystemClock` 外没有裸 `datetime.now()` / `time.time()`（验收清单里的白名单要逐文件写进用例）。

**证据要求：** `pytest tests/memory/test_timeline_clock.py tests/memory/test_timeline_parse.py -q`；贴出验收 1 的两次结果（同一文本、两个时钟）与验收 3 的 `TimeResult` 原文。

**依赖：** Task 3（表结构里要用到 `tz_offset_min`/`at_source`/`time_precision` 的名字）。**可与 T4/T5/T6/T7 并行**（只写新包）。

**风险：** ① 相对时间解析的**词表覆盖**是开放集（"前几天"/"上周三"这类没定型）→ 只承诺 §15.5 表里列出的那几个，其余落 `unknown`（**这是设计要的行为，不是缺陷**）；② 时区数据依赖系统（本机 `+08:00`）→ 用例里**全部显式传 `tz_offset_min`**，不许依赖运行机器的本地时区。

**怎么证伪：** 把 `parse_time` 改成内部 `datetime.now()` 现取 → 验收 1 红（两个时钟得到同一结果）；删掉歧义分支（让它猜当年）→ 验收 3 红；把日界按 UTC 算 → 验收 4 红。

- [ ] 先写"两个时钟两个结果 / 四步映射 / 歧义不猜 / 日界随时区 / 无裸 now"五条用例（红）
- [ ] 实现 `clock.py` 与 `parse.py`
- [ ] 跑局部测试转绿，贴两次结果

---

### Task 9: 注册表 / 别名 / 五个归约器 / 有效期与取代链 / 冲突六类

**目标：** §15.2 的 `entity.attribute` 键与别名映射；§15.3 的五个确定性归约器（`sum_delta` / `latest` / `set` / `max` / `min`，未注册 → `none`）；§15.4 的有效期半开区间与取代链（**更新不是删除**）；§15.6 的六类不确定与三条出口。**M4-T1 的"应得 130"由本任务的归约器推出**，且**不调用任何模型**。

**改动面：**

- Create: `src/spoolkit/memory/timeline/registry.py`（`register_attribute` / `resolve_key` / `resolve_alias` / `add_alias` / `move_alias`；**键规范化只在写入侧做一次**（§15.2）；别名冲突 → 拒绝合并 + `memory_audit(action='alias_conflict')` + 两条都留 + 新写入 `status='needs_review'`；`alias move --force` 必须写 audit）
- Create: `src/spoolkit/memory/timeline/reduce.py`（`append_event` / `reduce_key` / `recompute_all` / `retract_event`；`StateRow` 产出；六个 `reason`）
- Test: Create `tests/memory/test_timeline_reduce.py`（**M4-T1..T4 就是它的单测**）、`tests/memory/test_timeline_registry.py`

**要点：**

- `sum_delta` 算法逐字照 §15.3：① 取该 `(entity, attribute)` 全部 event，按 `at_abs` 升序（同刻按 id 升序）→ ② 过滤 `status='retracted'` 的（**软撤销不进归约，但保留在证据链里**）→ ③ `base` = 最后一条 `op='set'` 的值（**若没有 `set` → 缺基准**）→ ④ `value = base + Σ(base 之后（含同刻）的 `delta`)` → ⑤ 只有一个 `unit` 才允许相加 → ⑥ 输出 `derived_from = [参与的全部 event id，升序]`。
- **四条硬规矩**（§15.3）：值必须能由事件链重算（`state` 里不许存不能重算的东西）；重算优先于缓存（每次写新 event **只重算受影响的 `(entity, attribute)`**，不是全库）；**重算与缓存不一致 → 以重算值为准** + `memory_audit(action='recompute_mismatch', before=缓存, after=重算)` + 标 `needs_review` **直到有人看过**（**不许静默盖掉**）；**不许浮点累加金额**（整数分或 `Decimal`，比较用精确相等）。
- **有效期与取代**（§15.4）：`valid_from` / `valid_to` 是**半开区间** `[from, to)`，`valid_to IS NULL` = 至今有效；新值到来 → 旧行 `valid_to = 新行.valid_from`、`superseded_by = 新行 id`、**旧行不删不改值**；`op='set'` 只在链上**记一个新值**，不改写历史；`event` 表**没有 DELETE 路径**；纠错唯一路径是 `retract`（软撤销，保留可恢复）。
- **六类不确定**（§15.6 表）逐条实现：`missing_base`（`value` **留空**，不给猜的数）、`unit_mismatch`（拒绝相加）、`same_instant`（**两条都留**；`speaker` 优先级 `user > tool > agent` **只用来给建议**，除非显式打开 `state_auto_pick`（默认关）**不许用来删除**）、事件晚到（**重算整条链** + `memory_audit(action='recompute_backfill')`）、`no_reducer`（**不许默认套 `sum_delta`**）、`time_unknown`（**不假装它在链首或链尾**）。
- **三条出口缺一不可**（§15.6）：`state.status` + `reason`；`memory_audit`（含参与判定的 event id）；`spool memory why <entity.attribute>`（T14 的 CLI）。
- **`state` 只能由归约器写**（§15.1）：把写函数做成"只被 `reduce` 调用"（模块私有或 `_write_state`），并用例钉住。
- **`state` 不跨工作区**（§13.13 第 8 条 / 「口径裁决 6」）：本模块不接受 `machine` scope，也不读别的库。
- **`event_archive_threshold`**（T1 已登记）：超限把最旧的 event 搬进 `memory_event_archive`，检索默认不含已归档；阈值标"待标定初值"。

**验收标准（可证伪）——M4-T1..T4 逐条机械可判：**

1. **M4-T1（余额例子）**：3 条 event（`set 100` / `delta -20` / `delta +50`，跨两天，`FakeClock`）→ ① `state('wallet.balance').value == 130`；② **同一份 event 链在完全不调用模型的条件下重算出 130**；③ `derived_from` 恰好是那 3 条 event id、升序、**每条都能回溯 `raw_text`**；④ 三种问法都对：`100`（第 1 天那一刻的那条 `set`）/ `80`（第 1 天结束当日重算）/ `130`（现在）。
2. **M4-T2**：`昨天`/`今天`/`刚才` 解析后 `at_abs` 是绝对时间戳（含 `tz_offset_min`）、`at_source='relative'`；失败 → `time_precision='unknown'`、**不猜**、不参与需要精确时刻的断言。
3. **M4-T3**：同键新值到来后旧 `state` 行 `valid_to` 非空 + `superseded_by` 指向新行、**旧行仍可查**、`event` 一条不少（append-only）。`retract` 后：`status='retracted'` 的行**仍在库里**、`event` 总数不变、值被重算。
4. **M4-T4**：两条互斥 `op='set'` → `state.status == 'conflict'`、**两条 event 都在**、`reason='same_instant'`、`why` 能说出"为什么判冲突"、注入时带"未定"标签（渲染在 T11）。
5. 另三条：缺基准 → `value` 留空 + `reason='missing_base'`；单位不一致 → `reason='unit_mismatch'`；未注册键 → `reason='no_reducer'` 且**不许默认 `sum_delta`**。
6. 金额不浮点：`Decimal("0.1") + Decimal("0.2")` 的用例断言精确等于 `0.30`；把实现改成 `float` → 该用例红。
7. 别名唯一：同一个 `alias` 想指另一个 `canonical` → 拒绝合并 + audit + 两条都留；`--force` 才能改且写 audit。
8. 幂等：同一 `round_key` 的 event 写两次 → 一条（与 T7 的账本口径一致）。

**证据要求：** `pytest tests/memory/test_timeline_reduce.py tests/memory/test_timeline_registry.py -q`；贴出 `state show wallet.balance` 那一份数据（推导值 + `reducer` + 3 条 event 的时间/op/value/raw_text/speaker，逐字照 §15.1 的示例形制）。

**依赖：** Task 3（表）、Task 8（时钟与解析）。

**风险：** ① `same_instant` 的"同一精度窗口"定义（`exact` vs `minute` vs `day`）**设计稿没有给死数值** → 本计划按"同精度窗口相等"实现并在任务报告里标注实现口径，请用户在 T14 的 `why` 输出上复核；② 事件晚到走"整链重算"，链长会随对话线性增长（`event_archive_threshold` 是待标定初值）；③ `time_precision='unknown'` 的 event **不参与同刻冲突判定**，但**仍进 `derived_from`** —— 这条组合要在用例里钉住（否则"不确定"会悄悄变成"确定"）。

**怎么证伪：** 把 `sum_delta` 改成"取最新一条 `set`"（丢掉 delta 累加）→ 验收 1 的 130 变 100 → 红；把取代改成"直接 UPDATE 旧行" → 验收 3 红；把 `same_instant` 改成"按 `speaker` 选一个" → 验收 4 红；把 `no_reducer` 改成默认 `sum_delta` → 验收 5 红；把金额改成 `float` → 验收 6 红。

- [ ] 先写 M4-T1..T4 与另三条的用例（红）
- [ ] 实现 `registry.py` 与 `reduce.py`
- [ ] 跑局部测试转绿，贴 `wallet.balance` 的完整输出

---

### Task 10: `remember` 工具（模型可调用的中途写入口）

**目标：** 按 §5.2 的形状加 `remember` 工具：`kind` / `text` / `evidence` **必填**，`trigger` / `scope` 可选；**同一套门禁**（走 T7 的 `write_memory`）；拒绝时**返回可读理由**（`ok=False` + 理由），与 `search_history` 的 `limit 最多 N 条（…）`（`tools.py:81-86`）同一口径——"让小模型知道为什么没记住，比静默丢弃或静默记住都好"。

**改动面：**

- Modify: `src/spoolkit/memory/tools.py`（新增 `remember_spec(session, *, overrides) -> ToolSpec`，形制照 `recall_spec`（`tools.py:44-61`）与 `search_history_spec`（`tools.py:151-221`）；`group="记"`）
- Modify: `src/spoolkit/cli/runtime.py`（在 `runtime.py:457-475` 的 `if parts.memory is not None:` 块里注册——**与 `recall` 同一处**，否则提示词会提到一个只有派发路径才有的工具）
- Test: Create `tests/memory/test_remember_tool.py`

**要点：**

- 参数 schema 逐字照 §5.2：`kind` 枚举 `fact|preference|decision|lesson|skill`；`scope` 枚举 `session|workspace|machine`；`evidence` 的 `description` 写"出处：file:line / 命令 / 工具输出摘要"。
- **写默认 `workspace`**；`scope='machine'` 在 P1 **一律拒绝**并给可读理由（不静默降级，§13.2/§8.2 + 「口径裁决 6」）。
- 拒绝理由**直接复用** `WriteResult.reason`（T7），**不许在工具层另写一套判据**。
- 工具描述要写清适用场景（§6.4 的"给提示不给内容"同一条线）："记下**跨任务仍有用**的事实/偏好/决策/教训，**必须给出处**"。
- 工具**不碰热记忆文件**（决议 6：`memory.md` 仍可手改）；`remember` 只写 `semantic`。

**验收标准（可证伪）：**

1. `remember(kind="fact", text="构建命令是 pytest -q", evidence="pyproject.toml:1")` → `ToolResult(ok=True)` 且 `semantic` 有 1 条、`memory_write` 有 1 行。
2. 缺 `evidence` → `ok=False` 且理由含 `J2`；`kind` 非法 → `ok=False`；`text` 空 → `ok=False`（复用 T7 的理由，不新造）。
3. 其余四道门禁在工具路径上同样生效：脱敏 / 幂等 / 精确去重 / 冲突各一条。
4. 工具不写热记忆：调用后 `read_hot()` 不变。
5. **注册位置正确**：装配出的 registry 里含 `remember`；`--no-memory` 时**不含**（与 `recall` 同口径）。
6. `scope='machine'` → `ok=False` 且理由明确（不是静默当 `workspace` 处理）。

**证据要求：** `pytest tests/memory/test_remember_tool.py -q`；贴出五条拒绝路径的 `ToolResult.content` 原文。

**依赖：** Task 7（`write_memory`）。

**风险：** ① 小模型不一定会调它（本仓实测 `dispatch` 在 50 题里调用 0 次，`docs/benchmarking.md:116-118`）→ **自动蒸馏必须留着兜底**（§5.2 明确），这条要写进任务报告；② 工具描述过长会吃系统提示的 token → 描述控制在"用途 + 必填项 + 上限"三句内。

**怎么证伪：** 让 `remember` 直接调 `semantic.insert_semantic`（绕过 `write_memory`）→ 验收 2/3 红；把注册挪到派发路径 → 验收 5 红；把 `machine` 静默当 `workspace` → 验收 6 红。

- [ ] 先写"五条拒绝路径 + 五道门禁在工具上生效 + 不写热记忆 + 注册位置"用例（红）
- [ ] 实现 `remember_spec` 并在 `runtime.py:457-475` 注册
- [ ] 跑局部测试转绿

---

### Task 11: 事件链作为**一个整体单元**参与候选→重排（§6.7）

**目标：** 落地 §6.7 的硬规则：**可变数量类问题不许只取"最相似的一条"**；要取 `(entity, attribute)` 的**有序事件链**（`at_abs` 升序，**含已被取代的旧 `state` 行与全部 event**），当作**一个整体单元**参与候选 → 重排；`rel` 取**链内最高** `bm25`（**不许把 N 条分数加起来**——那会让长链永远赢）；进上下文时**整链一起进或整链一起不进**。

**改动面：**

- Create: `src/spoolkit/memory/timeline/chain.py`（`is_variable_quantity(question, registry) -> bool`；`build_chain(conn, key, *, now, overrides) -> EventChainUnit`）
- Modify: `src/spoolkit/memory/rank.py`（候选类型加 `unit='event_chain'`；`rel` 取链内最高；**时序完整度**：`derived_from` 是否覆盖写链，**缺基准值时扣分并标 `needs_review`，不假装完整**）
- Modify: `src/spoolkit/memory/session.py`（`recall`（`session.py:54-73`）的候选池加入链单元；渲染 `event_chain(N)`）
- Test: Create `tests/memory/test_event_chain.py`（**M4-T5 就是它的用例**）

**要点：**

- **可变数量类问题的机械判据**（§6.7）：问句同时命中 ① 疑问词（`多少`/`几`/`还剩`/`现在`/`目前`）与 ② 一个**已注册**的 `entity.attribute` 键，或 ③ 一个时间副词（`到今天`/`目前`）。
- 链单元算 **1 个候选，不是 N 个**（§6.7 的流程图）。
- 渲染上界：`event_chain_max_units=2` × `event_chain_render_max_chars=400`（T1 已登记）；**与 `recall` 共用同一通道预算，不叠加**（§6.2）。
- 渲染要列链上每条的 `at_abs` / `op` / `value` / `raw_text` / `speaker`；`time_precision='unknown'` 的**排在已解析事件之后**并带 `?` 标记（§15.5 末段）。
- `state.status` 为 `conflict` / `needs_review` 时渲染带标签（`wallet.balance = ?（未定：2 条互斥事件，见 event:7, event:9）`）；**不许只注入一个数**（§15.6 末段）。
- **顺序与轮次**：链单元把"一次取一条、取几轮凑"变成"一次取一个结论"——**减少的是轮次不是并发**（§6.7 末段），这条写进模块注释。

**验收标准（可证伪）——M4-T5：**

1. 问「现在还剩多少」→ 候选里出现 **1 个 `unit='event_chain(3)'`**，而**不是**"最相似的 1 条"（断言候选里没有把链上 3 条 event 拆成 3 个候选）。
2. `why` 同时给出 `channel` / `bm25` / `importance` / `age_days` / `unit='event_chain(3)'`，并列出链上每条的 `at_abs` 与分母分。
3. **整链进/出**：`event_chain_max_units=0` → 一个链单元都不进，且 `dropped` 里每条链有 reason；`=1` 而候选有 2 条链 → 进 1 条、另 1 条有 reason。
4. `conflict` 的链注入时带"未定"标签（含互斥 event id），**不注入单个数**。
5. **长链不加分**：造一条 1 事件链与一条 10 事件链、链内最高 `bm25` 相同 → 两者 `rel` 相同（不是 10 倍）。
6. 渲染不超界：单个链单元的渲染长度 ≤ `event_chain_render_max_chars`（超了要截断并留标记，不是硬切）。

**证据要求：** `pytest tests/memory/test_event_chain.py -q`；贴出「现在还剩多少」这一问的候选清单与 `why` 原文（含 `unit='event_chain(3)'`）。

**依赖：** Task 6（`rank.py`）、Task 9（归约器与 `state` 行）。**T13 依赖它。**

**风险：** ① 链单元与单条候选混在一个候选池里比分会失衡（链的 `imp` 怎么取？）→ 本计划取"链内最高 `importance`"并在 `parts` 里注明是"链内最高"，**这是本计划的口径，设计稿未给**；② 链的 token 占用比单条大 → 它和 `recall` 共用预算（T12 的让位顺序里 `event_chain` 算在 `recall` 通道内）。

**怎么证伪：** 把 `rel` 改成链内求和 → 验收 5 红；把链拆成 N 个候选 → 验收 1 红；把 `max_units=0` 的检查去掉 → 验收 3 红。

- [ ] 先写 M4-T5 的五条断言（红）
- [ ] 实现 `chain.py` 与 `rank.py` 的链单元支持
- [ ] 把链接进 `session.recall`，渲染 `event_chain(N)`
- [ ] 跑局部测试转绿，贴 `why` 原文

---

### Task 12: 三层注入策略 + 直注门槛 + 预算与让位 + 工作记忆白名单

**目标：** 落地 §6.6 / §13.10 定案的三层：**工作记忆**（任务级，每轮注入，任务结束**收掉 + 降级为 `episode`**）、**热记忆**（每轮注入，按重要度淘汰）、**检索式**（默认不注入，按需取）；`task_state` **计入** `memory_budget_ratio=0.30` 的单轮总额但**不在让位序列里**（只做**自身截断**）；让位顺序 **`recall` > 教训推送 > 热记忆**；**直接注入的门槛 = 重要度达标（`confidence ≥ 0.8`）+ 时间窗内（最近 N 天）+ 过冲突检查**（§13.13 第 7 条选 D = B+C），且**只有任务级状态（`task_state` 与 `status='active'` 的 `state` 行）默认直注**。

**改动面：**

- Modify: `src/spoolkit/agent/state.py`（`TaskState`（`state.py:27-37`）**追加** `decisions: list[str]` 与 `constraints: list[str]`；`render()`（`state.py:47`）在**既有行序之后**追加两行 `已定: …` / `约束: …`；**既有字段与行序一律不动**）
- Modify: `src/spoolkit/agent/loop.py`（`_sections`（`loop.py:448-471`）里加 `memory_budget_ratio` 的总额封顶与让位顺序；`task_state` 的自身截断保持 `loop.py:460-463` 的既有形态；直注 `state` 行的门槛与"**必须带 `derived_from` 引用**"；`_archive`（`loop.py:1274-1300`）把 `decisions`/`constraints` 带进 `episode.summary` 并**收掉**任务级状态）
- Test: Create `tests/memory/test_injection_policy.py`；Modify `tests/agent/test_state.py`（`TaskState` 的既有用例）

**要点：**

- **白名单**（§6.6 表）：工作记忆只注入 目标 / 当前步骤 / **约束** / **已确认的决策** / 未决问题；**不在白名单里的不许注入**。
- 与现有 `TaskState` 的差（§6.6 [实测] `state.py:27-37`）：现在六个字段是 `goal`/`done`/`current`/`verify`/`excluded`/`hypothesis`，**缺"已确认的决策"与"约束"**——它们被挤在 `excluded`（"已排除"）与 `hypothesis`（"下一步假设"）里，而 **"我选了 A" ≠ "我排除了 B" ≠ "我猜下一步是 C"**。
- **检查点兼容性**（§6.6 [实测] `state.py:93-106`）：`asdict` 写、`TaskState(**json.loads(...))` 读 → **旧检查点被新代码读 = 缺字段取默认值，兼容**；**新检查点被旧代码读 = 未知字段 `TypeError`，不兼容**。本计划**明确接受"加字段之后不支持中途降级续跑"**（检查点是过程状态，`clear_state`（`state.py:109-116`）在成功结束时会删掉它），并**把这条写进任务报告**。
- **直注三条出口的引用**：注入 `state` 行时**必须带 `derived_from` 的引用**（读者一眼看到"这是由哪几条事件推出来的"，§6.6）。
- `state_direct_inject_max_age_days` 是 T1 登记的**本计划初值**（待标定）。
- **`semantic` 普通条目不做直接注入**（§6.6）——配一条用例：`confidence=1.0` 的 `fact` **不**出现在直注区。
- **不许顺手复活 `doing` 分节**（§2.1 的历史坑：`hot.py:23-30` 定义了它、`distill.KINDS`（`distill.py:30`）不含它、`archive.py:139` 还主动 `pop("doing")`）——工作记忆的载体是 `TaskState`，不是热记忆的 `doing`。
- 本任务**不写** `memory_decision` 与 `--events memory`（那是 T13）：本任务只把"取哪些、砍哪些、为什么砍"算出来并交给调用方。

**验收标准（可证伪）：**

1. **让位顺序**：构造同时超总额的 hot + lessons + recall → 先砍 `recall`、再砍 lessons、**热记忆最后让**；`task_state` **只自身截断**（`done_inline` 从 N 降到 1）且**不被让掉**（四通道同时超总额时 `task_state` 仍在）。
2. **总额封顶**：`memory_budget_ratio=0.30` × 有效预算为上界，四通道之和超上界时**实际占用 ≤ 上界**。
3. **直注三关**：`confidence=0.79` → 不直注；`confidence=0.9` 但 `age_days > N` → 不直注；`confidence=0.9` 且窗内但 `status='conflict'` → 不直注；三关全过 → 直注 **1 条**且**带 `derived_from` 引用**；上界 1 条（`state_direct_inject_max=1`）→ 两条都达标时只进 1 条。
4. **只有任务级状态直注**：`confidence=1.0` 的 `semantic` 普通条目**不进**直注区，只能按需检索。
5. **`TaskState.render()` 的既有行序不变**（既有用例全绿），新两行**追加在末尾**；**旧检查点**（缺 `decisions`/`constraints`）能读（`load_state`（`state.py:102`）不报错）。
6. **任务结束**：下一轮装配里**没有** `task_state` 分节；`decisions`/`constraints` 能在 `episode.summary` 里按 LIKE 查到。

**证据要求：** `pytest tests/memory/test_injection_policy.py <TaskState 既有用例> -q`；贴出让位顺序的四次装配输出（`chosen`/`dropped` 与 token 数）；贴出旧检查点能被读回的输出。

**依赖：** Task 1（旋钮）、Task 6（`parts`/预算口径）、Task 9（`state` 行）。**可与 T10/T11 并行**（`loop.py`+`state.py` 只有本任务碰）。**T13 依赖它。**

**风险：** ① `task_state` 的 348 token 是**上限推算**（设计稿 §6.2 自己标注"实测占用未测"）→ 任务报告里不许把它写成实测；② 加字段导致的"新检查点旧代码读不了"是**已知的不兼容**（§6.6 明说），必须在报告中作为**已知代价**列出，不许说成"完全向后兼容"；③ 让位逻辑与既有 `Assembler`（`loop.py:439`）的裁剪可能**双重裁剪** → 用例要断言"不会出现同一通道被砍两次却算两次"。

**怎么证伪：** 把让位顺序改成"先砍热记忆" → 验收 1 红；把 `task_state` 放进让位序列 → 验收 1 红；把直注门槛改成只判 `confidence` → 验收 3 红；把 `semantic` 条目也直注 → 验收 4 红。

- [ ] 先写"让位顺序 / 总额封顶 / 直注三关 / 只有任务级直注 / 旧检查点可读"五条用例（红）
- [ ] 给 `TaskState` 追加两字段并在 `render()` 末尾追加
- [ ] 在 `loop.py` 实现总额封顶、让位顺序、直注门槛
- [ ] 跑局部测试转绿，贴四次装配输出

---

### Task 13: 可解释性三出口（`memory_decision` / `--events memory` / `spool memory why`）

**目标：** §6.3 的**三处出口写同一份数据**；`why` 的必备字段**缺一个就算不合格**：`channel`（命中哪一层，指出落在哪个分节的哪一条）/ `bm25`（**原始值 + 归一化分量两个都打**）/ `importance`（用户改过的标 `(user)`）/ `age_days` + `rec`（含 `half_life_days`）/ `unit`（单条 or `event_chain(N)`）/ `in`/`out` + `reason`（`below_k`/`over_budget`/`demoted`/`superseded`/`conflict`）/ `embed`（`ok` / `off:<reason>`）。

**改动面：**

- Create: `src/spoolkit/memory/decision.py`（`DecisionRecord` + `to_dict()`；`record_decision(conn, session_id, task_id, channel, candidates, chosen, dropped, embed_status, *, clock)`）
- Modify: `src/spoolkit/memory/session.py`（`recall`（`session.py:54-73`）的输出从 `[kind] text（来源 …）` 改成"每行带分值分解 + 命中词 + 候选总数"；`hot_text()`（`session.py:39-52`）把被淘汰项一起交出去，好让 `dropped` 有内容）
- Modify: `src/spoolkit/agent/loop.py`（每个通道装配后写一条 `memory_decision`；`--events` 模式新增 `{"type":"memory","channel":…,"count":…,"tokens":…,"budget":…,"dropped":…,"embed":"off:disabled"}`——**只加字段、不改既有事件语义**，与心跳 `wait` 同一做法）
- Modify: `src/spoolkit/cli/commands/memory.py`（新增 `why` / `list` / `search --explain` / `show`）
- Modify: `src/spoolkit/cli/app.py`（`_add_memory_command`（`app.py:1036-1074`）的 `choices` 从 `["migrate"]` 扩成 `["migrate","why","list","search","show"]`；**保持"不定义 `provider` 属性"这条离线口径**（`app.py:1041-1046`））
- Test: Create `tests/memory/test_decision.py`、`tests/memory/test_memory_cli_readonly.py`

**要点：**

- 三出口**共用同一个序列化**（`DecisionRecord.to_dict()`）：`why` 与 `--events` 不许各写一套字段（否则"同一份数据"就成了口号）。
- **`embed` 字段在 P1 只有"关"这一种状态**（`w_embed=0.0`）——但**必须打得出来**：`off:disabled`（§14.4 规矩 6 的出口③在 P1 的形态）。**这是本计划新增的一个取值**（§14.4 枚举的是 `ok`/`off:not_installed`/`off:no_model`/`off:offline`/`off:timeout`/`off:below_threshold`，没有"配置关闭"），见「口径裁决 2」。
- 新增子命令**保持离线**：只 `--root`、**不定义 `provider` 属性**、不触发 `settings.describe_missing()`；沿用 `test_migrate_不解析工作区env`（`tests/memory/test_migrate_command.py:328`）的钉法，新增 `test_memory_子命令_不解析工作区env`。
- 网页壳**不做**（「口径裁决 7」）。
- 施工期文档回写（**若用户允许**）：`docs/cli.md` 记一条新命令（现在是 `docs/cli.md:316-324` 的 `spool memory` 一节）。

**验收标准（可证伪）：**

1. 每轮每通道一条 `memory_decision`：一次 3 通道的装配 → 3 行，`candidates` 数与实际候选数一致，`chosen`/`dropped` 是合法 JSON。
2. `why` 的**七个必备字段全在**（逐字段断言，缺一个即红）。
3. 原始 `bm25` **与**归一化 `rel` **两个都打**（断言输出里两个数都在）。
4. `--events` 的既有字段语义不变（既有事件用例全绿），只多一个 `memory` 事件。
5. `in`/`out` 的 reason **覆盖五个枚举**（`below_k`/`over_budget`/`demoted`/`superseded`/`conflict` 各造一条）。
6. **三出口一致**：`spool memory why <ref>`、`why --session <id>`、`memory_decision` 行**逐字段一致**（同一份数据三个视图）。
7. 离线：新增子命令在含哨兵 `.env` 的 tmp 工作区里跑，不读密钥（`test_memory_子命令_不解析工作区env`）。

**证据要求：** `pytest tests/memory/test_decision.py tests/memory/test_memory_cli_readonly.py tests/memory/test_migrate_command.py -q`；贴出 `spool memory why <ref>` 的完整原文与对应的 `memory_decision` 行原文。

**依赖：** Task 11（单元）、Task 12（装配处）。**T14 依赖它**（同改 CLI 文件）。

**风险：** ① `why` 的输出会进人眼，字段多容易变成一坨 → 按 §6.1 的示例形制排版（"分 0.71 = 相关 0.38 + 新近 0.12 + …；候选 7 条，命中词「构建」「命令」"）；② `memory_decision` 每轮每通道一条 → 表会随对话增长，**P1 不给它归档**（不在用户列的范围），要在报告里如实标注这一点。

**怎么证伪：** 删掉原始 `bm25` 只留分量 → 验收 3 红；把 `why` 另写一套字段 → 验收 6 红；给 `memory` 子命令加上 `provider` 属性 → 验收 7 红。

- [ ] 先写"七字段齐 / 两个 bm25 数 / 三出口一致 / 五枚举 / 离线"用例（红）
- [ ] 实现 `decision.py` 与 `session.py` 的渲染
- [ ] 在 `loop.py` 落库并在 `--events` 加 `memory` 事件
- [ ] 扩 `cli/commands/memory.py` + `app.py` 的子命令
- [ ] 跑局部测试转绿，贴 `why` 与 decision 行原文

---

### Task 14: 状态与时序的纠错入口（P1 只给 CLI）

**目标：** §13.13 第 4 条定案：**P1 先给 CLI 入口**（"看 / 改 / 作废"三件事都要有），**网页可写面留 P2**；改值**默认走"`event retract` 软撤销 + 重算"**（A 为默认），`--force` 才直改 `state.value` 且**必须写 `memory_audit`**。

**改动面：**

- Modify: `src/spoolkit/cli/commands/memory.py`（新增 `state show` / `state verify` / `event retract` / `attr add` / `alias add` / `alias move`）
- Modify: `src/spoolkit/cli/app.py`（继续扩 `choices` 与参数；**保持离线口径**）
- Test: Create `tests/memory/test_memory_cli_state.py`

**要点：**

- `spool memory state show wallet.balance` 的输出**逐字照 §15.1 的示例形制**：推导值 + `reducer` + "由 3 条事件重算" + 每条 event 的时间（含时区）/`op`/`value`/`raw_text`/`speaker`。
- **两种问法都要能答**（§15.4）：`state show <key>`（现在是多少）与 `state show <key> --at <日期>`（我当时说的）——`--at` 取 `at_abs` 落在 `[valid_from, valid_to)` 的历史行。
- `state verify`：**全量重算比对**（§15.3 第 2 条）；不一致 → **以重算值为准** + `memory_audit(action='recompute_mismatch')` + 标 `needs_review`；**不许静默盖掉缓存值**。
- `event retract <id>`：软撤销（`status='retracted'`，不进归约，**保留、可恢复**）→ 触发重算；**不做物理删除**（真要删要 `--purge` 且写明后果，§15.4）。
- `alias move --force`：强行改必须写 audit（§15.2）。
- **不接受 `--scope machine`**：显式给了 `machine` → 明确报"`state` 不跨工作区（§13.13 第 8 条）"并非零退出（「口径裁决 6」）。

**验收标准（可证伪）：**

1. `state show` 的输出含 `reducer=sum_delta`、`derived_from` 的 3 个 event id、每条 event 的 `raw_text`（逐条断言）。
2. `state show --at <第 1 天>` → `80`（**边界写死**：`--at` 落在 `[valid_from, valid_to)` 的历史行，包含关系在用例里明确）。
3. `event retract` 后：值被重算、`status='retracted'` 的行**仍在库里**、`event` 总数**不变**。
4. **`--force` 的可观测后果**：直改 `state.value` 后 `memory_audit` 有一行；**下一次 `state verify` 会把它标成 `recompute_mismatch`** 且值被重算覆盖（这正是"推导值与事件链不一致"的可见形态）。
5. `--scope machine` → 非零退出 + 明确文案。
6. 既有 `migrate` 行为不回归（`tests/memory/test_migrate_command.py` 全绿）。

**证据要求：** `pytest tests/memory/test_memory_cli_state.py tests/memory/test_migrate_command.py -q`；贴出 `state show wallet.balance` 的完整原文（照 §15.1 示例形制）与 `state show --at` 的两次输出。

**依赖：** Task 13（同改 `cli/commands/memory.py` + `app.py`）。**T15 依赖它。**

**风险：** ① `--at` 的边界语义（是"那一刻的历史值"还是"截至那一刻的重算值"）设计稿给了两种问法但没给 CLI 语义 → 本计划把 `--at` 定为"截至该时刻的**重算值**"并在用例里写死，同时保留 `--state-at` 这种"取那一刻的历史行"的行为？**不**——**只做一种**（`--at` = 取 `[valid_from, valid_to)` 的历史行，与 `state show` 的"现在"对称），并在报告里标注这是本计划选的语义。**待确认**。
- ② `--force` 直改后必然引发 `recompute_mismatch` —— 这是**有意**的（让"人改的"和"链推的"分得开），任务报告要写清这不是 bug。

**怎么证伪：** 把 `retract` 改成物理 `DELETE` → 验收 3 红；把 `--force` 改成不写 audit → 验收 4 红；让 `--scope machine` 静默退化成 workspace → 验收 5 红。

- [ ] 先写"show 形制 / --at 边界 / retract 保留 / --force 留 audit / machine 拒绝"五条用例（红）
- [ ] 实现六个子命令与 `app.py` 注册
- [ ] 跑局部测试转绿，贴 show 原文

---

### Task 15: 旧库迁移与回放（幂等）+ 回滚演练

**目标：** §9.3 第 2、4 步 + §9.4：把旧 `memory` 表的每一行按 `kind` 复制进 `semantic`（继承 `confidence`/`use_count`/`applied_count`/`success_count`/`created_at`；`content_hash` 现算；`evidence` 为空 → 标 `needs_review`，**不静默丢弃**）；迁移**幂等**（`memory_write(source='migrate', round_key=sha1(old_id))`）；旧 `memory` / `memory_fts` **保留只读**（决议 7）；`restore <JSONL>` **逐行走同一个写入门禁**——所以回放不会重新引入已被拒的内容，也不会产生重复。

**改动面：**

- Modify: `src/spoolkit/cli/commands/memory.py`（`migrate`（`cli/commands/memory.py:325-361`）增库侧迁移：`--yes` 时既做文件迁移（P0 行为不变）也做 `memory` → `semantic` 复制并导出 JSONL；新增 `restore <文件>`）
- Modify: `src/spoolkit/cli/app.py`（`choices` 继续扩；**保持离线口径**）
- Test: Create `tests/memory/test_migrate_db.py`、`tests/memory/test_replay.py`
- 复用：`scripts/p0t8_rollback_drill.py`（P0 留下的回滚演练脚本）

**要点：**

- 迁移**幂等**：`content_hash` 的唯一部分索引 + `memory_write(source='migrate', round_key=sha1(old_id))` 两道一起挡（§9.3 第 4 步）。
- **登记进 `semantic.status` 的 `needs_review`** 是「口径裁决 5」那个新取值；**不许**为了绕开这个口径而给 `semantic` 加列。
- **回放走门禁**：`restore` 逐条调 `write_memory`（T7），**不许**直接 `insert_semantic`；回放报告要像 `migrate` 的 dry-run 报告一样分栏（收 / 判重 / 判据拒 / 脱敏），并且**驳回的条目不许入库**。
- 迁移**不烧模型**：库侧复制**不做** §5.3 ⑤ 的冲突判定调用（那是 T7 在 `remember`/蒸馏路径上的事）——冲突只在同 hash/近似去重两级处理。**这条要在报告里写明**，否则会被当成"迁移把冲突漏了"。
- **旧文件迁移的行为一个字不改**（P0 的 `--dry-run` 默认、`--yes` 先改名备份再写，`cli/commands/memory.py:340-360`）。
- **回滚**（§9.4）：`semantic` 是**追加的新表**，回滚 = 把代码回到旧版本；旧表与旧文件都在；**不做 `DROP TABLE`**。

**验收标准（可证伪）：**

1. 旧库 fixture（`memory` 有 N 行，其中一条的 `evidence` 不可得）→ 迁移后 `semantic` 有 N 行；`confidence`/`use_count`/`applied_count`/`success_count`/`created_at` **逐字段**与旧行相同；`content_hash` 与 `gate.content_hash(text)` 相同；无出处的那些 `status='needs_review'`。
2. **幂等**：连跑两次 `--yes` → `semantic` 行数不变、`memory_write` 里 `source='migrate'` 的行数不翻倍。
3. **旧表只读保留**：`memory` 的行数与内容不变、`memory_fts` 名字还在；旧路径 `search_memories` 仍能跑且结果不变。
4. **回放**：导出 JSONL → `restore` → 与导出前逐字段一致（除 `status`）；**故意在 JSONL 里放一条过不了判据的条目 → 回放报告里出现 `denied` 且库里没有它**（"导出再导入不会绕过判据"，§5.4）。
5. **回滚演练**（在 tmp 工作区做，不碰真实工作区）：文件层（`memory.md.bak-<日期>` 改回）、库层（新表留着、旧表旧索引只读保留、`sqlite_master` 里 `memory`/`memory_fts` 两个名字都在）、代码层（回退本批改动后全套回到基线）——三步都留输出。

**证据要求：** `pytest tests/memory/test_migrate_db.py tests/memory/test_replay.py tests/memory/test_migrate_command.py -q`；在**真实 `memory.db` 的只读副本**（`.tmp\` 下）上跑一次 `migrate --yes` 并打印迁移前后 `semantic` 行数、`memory_write` 行数、`user_version`（⚠️ **原库不动**）；回滚演练的三步输出。

**依赖：** Task 2、Task 3、Task 7、Task 14（同改 CLI 文件）。**与 T16 可并行**。

**风险：** ① 真实库的 `memory` 表当前 **0 行**（设计稿 §1 实测）→ 迁移是**空操作**，这本身是要如实报告的证据，**不许**因为"没数据"就跳过幂等与回放的用例；② `needs_review` 这个新取值与 §4.3 枚举的偏差必须在上报里点名；③ 迁移写 `semantic` 会让**热记忆的来源**从 `memory.md` 变成"库 + 文件"两处 → 决议 6 的"两处状态需要一致性检查"在 P1 **只有 T13 的只读视图**，`doctor` 不在 P1（「口径裁决 7」）。

**怎么证伪：** 把 `content_hash` 改成 `sha1(text)`（不归一化）→ 验收 1 里"与 `gate.content_hash` 相同"红；把迁移写成 `INSERT OR IGNORE` 绕过 `memory_write` 账本 → 验收 2 红；让 `restore` 直接 `insert_semantic` → 验收 4 红。

- [ ] 先写"逐字段继承 / 幂等 / 旧表只读 / 回放走门禁"四条用例（红）
- [ ] 实现库侧迁移与 `restore`
- [ ] 在 `memory.db` 副本上复验，做三步回滚演练并留证

---

### Task 16: 评测仪器（M1 两轮 + M4-T6 跨天脚本）

**目标：** 把 §10.1 的 **M1**（同一批前 N 道铺进一个工作区、**同一条会话分两轮**、只看**第 2 轮**输入 token）与 §10.4 的 **M4-T6**（跨天多轮 + **伪造时钟**）做成**可重复、可回归**的脚本与夹具。**本任务不产出任何结论**——它交付"怎么跑"，读数由使用者决定何时采集。

**改动面：**

- Create: `scripts/memory_bench_twowheel.py`（M1 编排与读数采集；复用 `src/spoolkit/bench.py`（`bench.py:45` `DEFAULT_BENCH_ROOT = "benchmarks/tasks"`，50 道）的既有读数路径）
- Create: `benchmarks/tasks/temporal/`（M4-T6 的跨天多轮任务脚本 + 夹具；会话时间与 turn 时间都是**参数**）
- Modify: `src/spoolkit/bench.py`（**只加开关**：两轮同会话的编排；**默认仍 `--no-memory`**——`docs/benchmarking.md:598` 的既有默认不许改）
- Test: Create `tests/memory/test_temporal_fixture.py`

**要点：**

- **不许重启 8080**（本机 pid 43096 在跑）：脚本必须**自身不重启、不启停任何服务**，并在需要服务时打印"请先确认服务状态"；M1 的"两次跑都要冷启动 llama-server"（§10.1）**由使用者决定时机**。
- 判据**写进脚本的输出**、**不写进本计划的结论**：第 2 轮输入 token **降 ≥15%**、通过数**不低于基线 −1 道**（±1 道的噪声容忍来自 `docs/benchmarking.md:32-37`）。
- **`Δ ≈ 0` 时要打印"这一层没做出来"**，而不是"量不准"（§10.2 第 2 条：现状在 M1 上预期 Δ≈0，这正好是一个可证伪的起点）。
- **M4-T6 的断言**：唯一可接受的答案来源是**注入的 state / 事件链**；若模型绕过它自行算术（"数字对得上、时间不对"那类）→ **记一次失败**。
- 夹具必须**时钟可注入**：会话时间与 turn 时间都是参数，**不许 `datetime.now()`**（§10.4「要跑起来还缺什么」第 1 条）——否则同一份数据两次重算日期不同、不可回归。
- 保留既有口径（§10.4「保留（不改）」）：**第 2 轮起输入 token 下降 ≥15%、通过率不降**这条主判据**不替代**，M4-T* 是它的**前置**。
- ⚠️ **`--no-memory` 基线不能去掉**——M1 的基线本身就是"没有记忆"（§10.2 第 1 条），M1 与 M0 的差就是记忆的全部贡献。

**验收标准（可证伪）：**

1. `--dry-run`（只准备夹具、不起模型）能跑通，并打印"需要什么、会花多少、需要哪个服务"。
2. **仪器自身的单测**（不是 M1 的读数）：用一个假的 bench 响应注入两轮 token 读数 → 脚本正确算出"第 2 轮 token 变化"与判据行（含 `≥15%` 与 `≥ 基线−1` 两条）。
3. **夹具可回归**：同一份跨天夹具 + `FakeClock` 跑两次，`at_abs` **逐条相同**；把夹具里的时间改成现取 → 该用例红。
4. **不重启服务**：源码级用例断言脚本里没有 `subprocess`/`http` 启停服务的调用。
5. **`--no-memory` 仍是默认**（`bench.py` 的既有默认不变，用例钉住）。

**证据要求：** `pytest tests/memory/test_temporal_fixture.py -q`；贴出 `--dry-run` 的完整输出与仪器单测的两轮读数演算。

**依赖：** Task 9（夹具要归约器）、Task 12/13（要读 `memory_decision` 才能算通道占用）。**与 T15 可并行**。

**风险：** ① M1 需要 50 题的长跑与冷启动服务 → **本轮与施工期都不跑**，何时跑由使用者决定；② 50 题是单函数小题（`docs/benchmarking.md:116-118`），**M1 测不出"记忆对复杂长任务的价值"**（§10.3）——这条边界要写进脚本输出的抬头；③ 15% 是**待验证的预期，不是实测值**（§10.3）——脚本里不许把它写成"应达到的标准"，只写成"判据"。

**怎么证伪：** 把 `--no-memory` 默认改掉 → 验收 5 红；把夹具改成 `datetime.now()` 现取 → 验收 3 红；让脚本顺手重启服务 → 验收 4 红。

- [ ] 先写夹具可回归 + 仪器单测 + 不重启服务 + 默认不变四条用例（红）
- [ ] 写 `scripts/memory_bench_twowheel.py` 与 `benchmarks/tasks/temporal/`
- [ ] 给 `bench.py` 加两轮开关（默认不变）
- [ ] 跑 `--dry-run` 与仪器单测，贴输出

---

## 证伪速查表（一行一条）

| 任务 | 改回旧行为 / 去掉机制的**具体命令或用例名** |
|---|---|
| T1 旋钮登记 | `w_embed` 默认改成非 0 → `test_p1_旋钮全部登记且可覆盖` 红；删 `rank_w_penalty` → 同用例红 |
| T2 schema v3 | `_migrate_to_v3` 从 `_MIGRATIONS` 删掉 → `test_schema_semantic.py::test_six_tables_present` 红；`idx_semantic_dedupe` 去掉 `WHERE status='active'` → `::test_dedupe_index_is_partial` 红；给 `semantic` 加 FTS 触发器 → `::test_semantic_fts_不用触发器` 红 |
| T3 schema v4 | `is_derived` 去掉 `DEFAULT 1` → `test_schema_timeline.py::test_state_columns` 红；别名表唯一索引改普通 → `::test_alias_unique` 红 |
| T4 ① shingle + M2' | 语义路径的 `_match_query` 改回逐字 OR → `test_bigram.py::test_双字切分` 红 + `python scripts/memory_relevance.py --arm bigram` 与 `--arm store-char` 读数完全相同（判据行报"未跑出差异"）；删单字回退 → `::test_单字查询不回归` 红 |
| T5 ② bm25 | 删 `ORDER BY bm25(...)` → `test_semantic_search.py::test_排序由bm25决定` 红（锚：查 `存档` 的 top1 会变）；把 `-bm25()` 与 `bm25()` 对调 → `::test_bm25_符号与方向` 红 |
| T6 五维 + 两阶段 | `use` 改回 `use_count * 0.6` → `test_rank.py::test_一次使用不吃掉全部权重` 红；去掉 `K ≤ recall_limit` 夹取 → `::test_rerank_k_硬约束` 红 |
| T7 五道门禁 | `RULES` 删 J2 → `test_writer.py::test_缺证据被拒` 红；幂等改"先查后写" → `::test_幂等由唯一索引挡` 红；冲突改静默留新 → `::test_冲突不静默选一个` 红；`write_memory` 顺手写旧表 → `::test_单写semantic` 红 |
| T8 时钟 + 时间解析 | `parse_time` 内部 `datetime.now()` → `test_timeline_clock.py::test_同一文本两个时钟两个结果` 红；删歧义分支 → `test_timeline_parse.py::test_歧义不猜` 红；日界按 UTC → `::test_日界随时区` 红 |
| T9 归约器 + 取代链 | `sum_delta` 改成取最新 `set` → `test_timeline_reduce.py::test_M4_T1_余额130` 红（130 → 100）；取代改 `UPDATE` 旧行 → `::test_M4_T3_取代链不是删除` 红；`same_instant` 改按 `speaker` 选 → `::test_M4_T4_冲突不静默` 红；金额改 `float` → `::test_金额不浮点` 红 |
| T10 `remember` 工具 | 直接 `insert_semantic` 绕过 `write_memory` → `test_remember_tool.py::test_缺证据被拒` 红；注册挪到派发路径 → `::test_注册在装配处` 红 |
| T11 事件链单元 | `rel` 改成链内求和 → `test_event_chain.py::test_长链不加分` 红；把链拆成 N 个候选 → `::test_M4_T5_一个链单元` 红 |
| T12 三层注入 | 让位顺序改"先砍热记忆" → `test_injection_policy.py::test_让位顺序` 红；`task_state` 放进让位序列 → 同用例红；直注只判 `confidence` → `::test_直注三关` 红 |
| T13 三出口 | 删原始 `bm25` 只留分量 → `test_decision.py::test_两个bm25数都在` 红；`why` 另写一套字段 → `::test_三出口逐字段一致` 红；加 `provider` 属性 → `test_memory_cli_readonly.py::test_不解析工作区env` 红 |
| T14 纠错 CLI | `retract` 改物理 `DELETE` → `test_memory_cli_state.py::test_retract_保留可恢复` 红；`--force` 不写 audit → `::test_force_留audit` 红；`machine` 静默退化 → `::test_state_不跨工作区` 红 |
| T15 迁移与回放 | `content_hash` 改 `sha1(text)` → `test_migrate_db.py::test_逐字段继承` 红；迁移绕过 `memory_write` 账本 → `::test_迁移幂等` 红；`restore` 直接 `insert_semantic` → `test_replay.py::test_回放走门禁` 红 |
| T16 评测仪器 | `--no-memory` 默认改掉 → `test_temporal_fixture.py::test_基线默认不变` 红；夹具改现取时间 → `::test_夹具可回归` 红；脚本顺手重启服务 → `::test_不重启服务` 红 |

---

## 迁移与回滚

| 步 | 动作 | 回滚 | 残留 | 可逆性 |
|---|---|---|---|---|
| T2 v3 六表 | DDL（纯追加） | 代码回退（旧代码不看新表） | 6 张空表 + 索引留在库里 | 只增不减，无害 |
| T3 v4 六表 | DDL（纯追加） | 同上 | 6 张空表 + 索引 | 只增不减，无害 |
| T4 bigram 口径 | 代码 + `semantic_fts` 索引重建 | 用逐字口径重建一次 `semantic_fts` 并回填（底账是 `semantic` 表） | 无（索引可重算） | 可重建 |
| T5 bm25 排序 | 代码 | 代码回退（`memory` 表旧排序口径**从未改过**） | 无 | 完全可逆 |
| T6/T11 排序与链单元 | 代码 + `limits` 权重 | 代码回退；或把 `rank_w_*` 调回旧形态的等价取值 | 无 | 完全可逆 |
| T7 五道门禁 | 代码 + `limits` 阈值 | 代码回退；写进 `semantic` 的行**不删**（记忆是底账） | `semantic` 里的既有行 | 可逆（不丢数据） |
| T8/T9 时序 | 代码 + DDL | 代码回退 | event/state 行保留（append-only） | 完全可逆（`retract` 才改语义） |
| T12 工作记忆字段 | 代码 + 检查点格式 | 代码回退；**新检查点旧代码读会 `TypeError`**（已知不兼容，§6.6） | 旧检查点文件 | 部分可逆（见风险） |
| T13/T14 CLI | 代码 | 代码回退 | `memory_decision` / `memory_audit` 行保留 | 完全可逆 |
| T15 旧库迁移 | DDL + 数据复制 | 代码回退；`semantic` 里的复制行保留（`memory` 表只读不动） | `semantic` 的迁移行 + `memory_write` 的 `migrate` 行 | 可逆（不删任何东西） |

一句话：**P1 没有不可逆动作**——新表全部 `CREATE TABLE IF NOT EXISTS` 纯追加、旧 `memory` / `memory_fts` 与旧 `memory.md` 只读保留、`semantic_fts` 是可重算的派生索引、`event` 表没有 DELETE 路径；回滚 = **代码回退**（必要时加一次索引重建）。**全程不许 `DROP TABLE`、不许 `DROP COLUMN`、不许物理删除任何记忆行。**

新表全部经 `PRAGMA user_version` 步进（`store.py:211-216`、`init_memory_schema`（`store.py:243-257`）），每一步幂等、失败不记版本号（`_apply_step`，`store.py:219-240`）；`user_version` 高于代码时**什么都不做也不报错**（前向兼容，`store.py:246-253`）。

---

## 评测口径的落地方式

| 编号 | 落在哪个任务 | 需要什么夹具 / 脚本 | 怎么跑 | 当前状态 |
|---|---|---|---|---|
| **M1**（主判据：第 2 轮输入 token 降 ≥15%，通过率不降） | **T16** 出仪器；被测对象是 T12/T13 的注入与决策 | 50 道题（`benchmarks/tasks/`，`bench.py:45`）前 N 道铺进一个工作区 + **同一条会话两轮**；**服务端** token 计数 | `scripts/memory_bench_twowheel.py`（本计划命名，T16 定稿）；⚠️ **两次跑都要冷启动 llama-server、时机由使用者决定**；本批**不重启 8080**（pid 43096） | **没跑**（器械都还不存在）；`Δ` 的期望值 `≈0` 是 §10.2 的推断，**不是实测** |
| **M2'**（16 条固定语料 P@5/R@5 不低于纯 FTS5 基线） | **T4** 出仪器与基线臂；**T5** 出 `bigram+bm25` 臂 | `benchmarks/memory/corpus.jsonl`（16 条，G1–G4）+ `queries.jsonl`（13 条查询 + 相关 id）——**必须从 `.tmp/embed-bench/bench_common.py:15-61` 逐字固化**（`.gitignore:22` 忽略了 `.tmp/`，不固化就会丢）；基线读数固化自 `.tmp/embed-bench/results_fts5_store.json` | `python scripts/memory_relevance.py --arm {store-char,bigram,bigram+bm25}`；指标 `p@5`/`r@5`/`top1`（定义照 `bench_common.py:70-91`） | **仪器要新建（T4）**；**基线已有实测读数**（P@5 `0.3384615384615385` / R@5 `0.5961538461538461` / top1 `0.7692307692307693`，见 `results_fts5_store.json` 的 `summary`）；**bigram 臂的读数没跑** |
| **M4-T1**（100 → 花 20 → 赚 50 **应得 130**，断言"由事件链推出"） | **T9** | 3 条 event + `FakeClock`；**纯归约器单测，不要模型、不要 8080** | `pytest tests/memory/test_timeline_reduce.py -k M4_T1` | **没跑** |
| **M4-T2**（相对时间解析） | **T8** | 相对词表 + 两个 `FakeClock` 日期 | `pytest tests/memory/test_timeline_parse.py -k M4_T2` | **没跑** |
| **M4-T3**（取代链不是删除） | **T9** | 同键两条 `set` + 一条晚到的 event | `pytest tests/memory/test_timeline_reduce.py -k M4_T3` | **没跑** |
| **M4-T4**（冲突不静默选一个） | **T9** | 两条互斥 `set` | `pytest tests/memory/test_timeline_reduce.py -k M4_T4` | **没跑** |
| **M4-T5**（事件链整体检索） | **T11** | 余额 event 链 + 检索；需要 T6 的重排 | `pytest tests/memory/test_event_chain.py -k M4_T5` | **没跑** |
| **M4-T6**（端到端：模型会不会自己猜） | **T16** | `benchmarks/tasks/temporal/` 跨天多轮脚本 + **伪造时钟** + 一条能跨轮保留的会话 + 服务端 | 需 8080 服务（**不重启**）；判据："唯一可接受的答案来源是注入的 state / 事件链" | **没跑**；**夹具与脚本都还不存在** |

**纪律**：上表"当前状态"列里所有"没跑"在跑通之前**不许在任何文档里写成结论**；M2' 的 16 条语料上的读数是**相对**读数（P@5/R@5 上限 0.8），**不许外推**到真实库（§14.3.7 第 3 条）；M1 的 15% 是**待验证预期**（§10.3），**不是实测值**。

---

## 不做清单（P1 明确不做，一条都不许顺手做）

1. **嵌入真引入**：`semantic.embed_model`/`embed_dim`/`embed_vec` 三列、`embed-light`/`embed-onnx` extra、`model2vec`/ONNX/`torch`/`sqlite-vec`/`vec0`、`spool memory embed setup/warm/clean`、`embed_min_cos`/`embed_timeout_ms`/`embed_chunk_chars`、`HF_HUB_OFFLINE` 那套环境变量（§13.13 第 6 条 + 用户指令：**等 ① ② 之后再按增益换成本评估**）。P1 只保留 `w_embed=0.0` 的**可见关闭状态**。
2. **巩固**（两级、`spool memory consolidate`）——决议 4 + §9.5 P2 ①。
3. **衰减与容量上限**（`semantic_max_per_kind` 的五个旋钮、`status='archived'` 的自动触发）——§9.5 P2 ②。
4. **`episode` 归档表与 `episode_archive_threshold`**——§9.5 P2 ④。⚠️ **`memory_event` 的归档表与 `event_archive_threshold` 做**（§13.13 第 5 条已定案），两者别混为一谈。
5. **机器级 `scope`**：`%APPDATA%\spoolkit\memory.db`、跨工作区检索、读一次写一条 audit（决议 2 + §9.5 P2 ④）。`state` 不跨工作区在 P1 是**口径确认**（不给 `machine` 入口），**不是**新机制。
6. **网页壳记忆面板**（**只读面板也不做**）——决议 3 说 P1 只读，但**用户给的 P1 范围未列**，见「口径裁决 7」。
7. **`spool memory doctor`**——决议 6 / §14.6 说 P1 加，但**用户给的 P1 范围未列**，见「口径裁决 7」。
8. **`spool memory audit --undo`**（反向执行一条 edit/delete/supersede）——§7.3，属 §9.5 P2 ④ 的"归档/恢复/撤销"。
9. **技能自动执行**（把 `skill` 变成可运行脚本）——§12 第 6 条：要引入沙箱、权限、版本三套新机制。
10. **自动物理删除 / 自动遗忘**——§12 第 3 条；除 `--purge`（且写明后果）外**没有任何 DELETE 路径**。
11. **用户画像 / 偏好推断**——§12 第 5 条；偏好只能显式写。
12. **第二级去重的"语义"版本**：同义改写（"用 uv 管理依赖" vs "本仓统一用 uv"）**抓不到**，这条要**如实写在模块注释与 `--explain` 文案里**，不假装、不引依赖补。
13. **多用户 / 鉴权 / 云同步**——§6.5。
14. **复活 `doing` 分节**——§2.1 的历史坑（`hot.py:23-30` 定义了、`distill.py:30` 不含它、`archive.py:139` 主动 `pop`）。
15. **给 `memory.md` 加自动格式迁移 / 新元数据**——决议 6：热记忆仍是人类可读 Markdown，P1 只搬数据不改形态。
16. **为记忆单独做常驻服务 / 后台进程**——§12 第 7 条。
17. **`memory_decision` / `memory_audit` 的归档**——用户 P1 范围未列；它们的增长要在 T13 的报告里如实标注。
18. **`transcript` 与语义记忆合并成一张表**——§12 第 4 条。

---

## 风险与未知（未取证 / 待测量，**不许当结论用**）

1. **`bm25()` 的符号约定未实测**：本计划**不假设**它返回负数；T5 的第一条验收就是把它钉住。搞反会让整个新排序反向且用例可能看不出来。
2. **M2' 的夹具与基线读数存在被清掉的风险**：它们在 `.tmp\embed-bench\`（已确认 `.gitignore:22` 忽略了 `.tmp/`），**不进仓**。设计稿 §14.3.6 说"脚本与语料都在仓里"这句话与 `.gitignore` 的事实不符——T4 必须把它们**逐字固化进 `benchmarks/memory/`**。
3. **16 条语料太小**：分组查询的 P@5/R@5 上限只有 **0.8**，`包管理`/`uv` 这类符号级查询各只有 1 条样本（§14.3.7 第 3、4 条）。bigram 臂的读数**只能相对比，不能外推**。
4. **`semantic_fts` 不用触发器 = 与 §4.3 原则二的偏差**：同一个冲突 P0 已在 `memory_fts` 上绕过（P0 计划「范围裁决 5」与「风险 9」），P1 的 `semantic_fts` 上**仍未解**。要么接受"应用层写入口"（本计划），要么改设计。
5. **`semantic.status` 缺 `needs_review` 取值**：§9.3 第 2 步要求标它，§4.3 的枚举里没有（「口径裁决 5」）。
6. **`imp` 的"+0.1"表述有歧义**（"被 `supersedes` 过"）：本计划按"成功取代过别人"实现（「口径裁决 8」），**待确认**。
7. **`event_archive_threshold` 是待标定初值**：事件增长速率**没有实测**（真实工作区 `episode` 26 条 / 18 天、整库 77KB，**不能外推到 event**，§13.13 第 5 条）。
8. **`w_bm25` 与 `state_direct_inject_max_age_days` 的默认值是本计划自取的**（设计稿没给，见「口径裁决 4」）。
9. **四通道上界之和 3288 > 2089 是刻意的**（§6.2），让位顺序就是它的执行方式——**实机让位行为未实测**。
10. **`task_state` 的 348 token 是上限推算**：设计稿 §6.2 自己标注"实测占用未测"。同理 `hot` 的 1044 是 `hot_memory_ratio × 有效预算`，**当前 1011/1044 的读数来自设计稿 §1，本计划没有复看真实工作区**。
11. **真实工作区本轮没有复看**：`D:\workSpace\agentWorkSpace` 的 `memory.md` / `memory.db` 内容与 P0 计划记的"9 条 fact / `memory` 表 0 行"是否仍然如此，**本计划没有验证**（只读了仓内文件）。T2/T15 要用的副本必须**当场重新生成**。
12. **全套测试的 passed/failed 本轮没复跑**：只跑了 `--collect-only` → **2203 collected**（本轮实测）；`tests/memory` → **266 collected**（本轮实测）。P0 计划的 `2096 passed / 2 failed / 1 skipped`（收集 2099）是 **P0 收尾读数**。执行者必须每批自取基线。
13. **`rg` 不在本轮这个 shell 的 PATH 里**（`Get-Command rg` 为空）：跑全套前必须确认，否则会多出 6 条环境失败（`scripts/pytest.ps1` 说明第 4 条点名的那些）。
14. **M4-T6 需要能跑模型的会话且不许重启 8080**：何时跑由使用者决定；本计划与施工期**都不跑**。
15. **`same_instant` 的"同一精度窗口"没有定案数值**（§15.6 第 3 条只说"同一精度窗口"）：本计划按"同精度窗口相等"实现（T9 风险 ①），**待确认**。
16. **链单元的 `imp` 取值口径未定**（T11 风险 ①）：本计划取"链内最高 `importance`"，设计稿只定了 `rel` 取链内最高。
17. **`memory_decision` / `memory_audit` 会随对话增长**：P1 不给它们归档（不在范围），增长速率**未测**。
18. **工作记忆加字段导致"新检查点旧代码读不了"**（§6.6 明说的不兼容）：本计划明确接受并记录，**不假装完全向后兼容**。

---

## 待用户拍板 / 口径确认

1. **`semantic_fts` 用应用层写入口而不用 SQL 触发器**（「口径裁决 1」）——与 §4.3 原则二的偏差，§4.3 与 P0 的实现事实冲突。**开工前必须定。**
2. **两阶段 N/K 旋钮的命名**：`recall_candidates_n` / `recall_rerank_k`（本计划）vs 设计稿 §14.4 规矩 3 的 `embed_candidates_n` / `embed_rerank_k`（「口径裁决 3」）。
3. **`semantic.status` 增加 `needs_review` 取值**（「口径裁决 5」）——或者改成加列 / 只写 audit，三选一。
4. **`imp` 的"+0.1"到底加给谁**（"被取代过" vs "成功取代过别人"，「口径裁决 8」）。
5. **`w_bm25` 与 `state_direct_inject_max_age_days` 的默认值**（「口径裁决 4」，本计划取 1.0 / 30）。
6. **`state show --at` 的语义**（T14 风险 ①）：本计划定为"取 `[valid_from, valid_to)` 的历史行"。
7. **`same_instant` 的精度窗口定义**（风险 15）。
8. **两处"设计稿说 P1、用户范围没列"的归属**（「口径裁决 7」）：网页壳只读面板、`spool memory doctor` —— 进 P1 就加两个任务，不进就写进"不做清单"。
9. **`memory_decision.embed_status` 需要 `off:disabled` 这个新取值**（"配置关闭"，不在 §14.4 规矩 6 的枚举里，见「口径裁决 2」）。

---

## Self-Review 提醒（每个任务收尾前）

1. 有没有把"这次"的**意图**写进代码（阈值、路径、条目名）？该进 `limits.KNOBS` 的进了吗？**有没有绕过 Task 1 单独往 `limits.py` 追加？**
2. 新路径的**失败路径**都测了吗（文件不存在、库打不开、条目为空、备份重名、模型没给可解析结果、时间解析失败、别名冲突、候选为空、`max_use=0`）？
3. 这条改动的"改回旧行为会红"是**哪条用例**？它现在真的红吗（临时回退验证一次）？
4. 有没有顺手做了 P2（巩固 / 衰减 / 机器级 / 网页壳 / 嵌入 / `doctor` / `audit --undo`）？
5. **旧东西还通吗**：`memory` 表 SQL 一字未改、`memory_fts` 名字还在、旧 `memory.md` 可手改、`recall` / `match_lessons` / `search_episodes` 行为不回归、`spool memory migrate` 的 P0 行为（`--dry-run` 默认、`--yes` 先备份）逐字未变？
6. **顺序有没有被颠倒**（§13.11）：本批有没有在分词口径落地之前动排序、或者偷偷引入嵌入？
7. 有没有把"不确定"变成"确定"（`state` 的 `conflict`/`needs_review` 被静默选一个、时间 `unknown` 被当成 `turn`、`no_reducer` 被默认套 `sum_delta`）？
8. 证据留了吗（命令、输出、nodeid、原始读数）？**有没有把"预期"写成"实测"、把"16 条语料"写成"真实库"、把"上限推算"写成"实测占用"？**
9. 有没有把本条任务的改动伸进**别人的单写者文件**（`store.py` / `limits.py` / `loop.py` / `cli/commands/memory.py`+`app.py`）？
