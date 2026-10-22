# 记忆架构 P0 实施计划（停噪声 · 去重 · 可检索 · 淘汰序 · 迁移）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让记忆子系统**先停止起反作用**：蒸馏不再把整段助手答复当"项目事实"写进每轮注入的热记忆；跨任务/跨蒸馏的逐字重复不再二次入库；教训与 `kind`/`trigger`/`source` 可检索；热记忆同分节内不再先进先出；旧 `memory.md` 有一条可预览、可拒绝、可回滚的迁移路径。

**Architecture:** 全部改动都在"写入侧收口 + 结构只追加"这两条线上：判据与规范化指纹收到一个新模块 `memory/gate.py`；`archive_task` 的 promote 循环变成"先判据、再判重、后落盘"；`hot.trim_to_budget` 的分节内淘汰从 `pop(0)` 换成按重要度启发式淘汰；`memory_fts` 从 1 列扩到 4 列（`text/kind/trigger/source`）并把同步责任收成一处 Python 写入口；`init_memory_schema` 从 `executescript` 一把梭（`store.py:105-108`）改成 `PRAGMA user_version` 步进迁移，只给 `episode` 加列加索引。**不新增任何表。**

**Tech Stack:** Python ≥ 3.12，标准库 + 现有依赖（`sqlite3` 自带 FTS5、`argparse`）；**零新依赖**，不引入嵌入；测试 pytest，命令行入口 `.\.venv\Scripts\spool.exe`。

**设计文档：** [`docs/superpowers/specs/2026-10-02-memory-architecture-design.md`](../specs/2026-10-02-memory-architecture-design.md)

**本计划只写 P0**，对应设计稿 §9.5 的 P0 行与 §3 的缺陷 1（噪声/兜底）、2（跨任务无去重）、3（分节内 FIFO）、4（教训不进 FTS）。本文件**不含任何代码改动**，也不声称任何一条已经实现。

---

## 范围：P0 做什么、不做什么

| 做（本计划 8 个任务，对应下面 9 条交付） | 不做（属 P1/P2，一条都不许顺手做） |
|---|---|
| ① 蒸馏空条目的兜底不再写 `kind="fact"`（`distill.py:259-263`） | `remember` 工具与工具体系（§5.2，P1 ①） |
| ② 判据 J1/J3/J4 + J5(trigger) 落地为**代码**，寒暄/自述/一次性收尾不进"项目事实"（§5.1/§5.3 ②） | `semantic` 表、`semantic_fts`、FTS 触发器、`memory_link` / `memory_write` / `memory_audit` / `memory_decision`（P1 ②） |
| ③ 跨任务/跨蒸馏的结构性去重（`archive.py:96` 前加判重，§5.3 ④ 第 1 级） | 近似去重（编辑距离 / `dedupe_similarity`，§5.3 ④ 第 2 级）与冲突检测（⑤） |
| ④ 教训补 FTS；`memory_fts` 扩到 `text/kind/trigger/source` 可检索 | 重排序：`bm25` / 五维权重 / 负反馈（§6.1）与 `memory_decision`（§6.3） |
| ⑤ 热记忆同分节内按重要度淘汰，落实容量口径（`hot_max_items=40` / `hot_item_chars=200`） | 缺陷 10（两把尺子合一，§6.2）——见「待拍板 5」 |
| ⑥ `PRAGMA user_version` 版本化 + `episode` 只加列与索引（§9.3 第 1、5 步） | `episode_archive_threshold` 与归档表（P2 ④）、巩固（P2 ①）、衰减（P2 ②） |
| ⑦ `spool memory migrate --dry-run` 只读报告；`--yes` 才改名保留并重写（§9.2 第 1-3 步） | `spool memory list/search/show/forget/edit/audit/why/doctor` 与网页壳面板（P1 ③、决议 3） |
| ⑧ 真实工作区只读 dry-run 验收（§9.5 P0 验收句） | 旧 `memory` 表复制进 `semantic` 与双写比对、`verify-migration`、回放（§9.3 第 2 步 / §9.4 / P1 ⑤） |
| ⑨ 回归基线与回滚演练 | 嵌入（`w_embed`、`embed_*` 三列，§14.6）、50 题评测 M1/M2/M3（§10） |

## 范围裁决：与设计稿的三处边界（先摆明，别默默改口径）

1. **`episode` 加列 + `user_version` 收进 P0。** §9.5 把"迁移命令与回放"标在 P1 ⑤，而 P0 行的验收句点名"在真实工作区上 dry-run 迁移报告与实际一致"；§9.3 第 1、5 步（`ALTER TABLE episode ADD COLUMN ...`、`PRAGMA user_version`）正是这条验收的前置。本计划按用户指令把它们收进 P0：**纯追加、旧代码照读、可逆**，且任何后续分期都要它。若判定应属 P1，删掉 Task 4 不影响 Task 1/2/3/6/7 的验收（只有 Task 5 依赖它的版本步进）。
2. **`spool memory migrate` 的 P0 形态。** P0 只做：只读 dry-run 报告 + `--yes` 时的"门禁过滤 → 原文件改名保留 → 写新 `memory.md`"。**不做** §9.2 第 4 步"`semantic` 重渲染"（P0 没有 `semantic` 表），**不做** §9.3 第 2 步"`memory` 每行复制进 `semantic`"。
3. **`memory_fts` 从 1 列（`store.py:41`）改 4 列 = 重建索引。** FTS 是派生索引（内容在 `memory` 表），重建可回滚；§9.3 第 3 步与决议 7 要求的是"表不删不改名"，`memory` 与 `memory_fts` 两个名字都保留。若坚持"索引也一个字节不动"，备选是"把 `kind`/`trigger`/`source` 拼进 `text` 列"（代价：不能按列过滤）——见「待拍板 3」。
4. **P0 只实现 J1（一次性指代部分）/J3/J4 + J5 的 `trigger` 非空。** J2（`evidence` 非空）与 `scope` 需要蒸馏 schema 加必填 `evidence`，那是 §5.2 的 P1 项；因此 **dry-run 报告不得声称按 J2 判过任何一条**。
5. **P0 的 `memory_fts` 不用 §4.3 原则二的 SQL 触发器。** 触发器只能拿到 `new.text` 原样值，而本仓检索依赖写入前把中文逐字拆开（`store.py:66,82-84`）——SQL 触发器里没有这个 Python 函数。P0 改成"一处 Python 写入口"。⚠️ 这条同样会落在 P1 的 `semantic_fts` 上，见「风险与未知 9」与「待拍板 4」。

---

## Global Constraints

- **只做 P0**：上表右列一条都不做。新增**表** 0 张；只允许 `episode` 加列加索引 + `memory_fts` 重建。
- **回归底线**：当前基线 **2099 收集 / 2096 passed / 2 failed / 1 skipped**。本计划写就时复跑过（现场如下）：
  - `--collect-only`：**2099 tests collected**（与给定收集数一致）。
  - 全套（`rg` 已在 PATH）：**2 failed / 2096 passed / 1 skipped**——与给定基线**逐项一致**。
  - 全套（`rg` 不在 PATH 的那次，作为反例留着）：**8 failed / 2090 passed / 1 skipped**，多出的 6 条正是 `scripts/pytest.ps1:19-21` 点名的 rg 依赖项：`tests/tools/test_read_roots.py::test_授权后可搜索`、`tests/tools/test_search.py::test_搜索命中并返回文件名`、`tests/tools/test_search.py::test_无命中时返回空结果而非失败`、`tests/tools/test_search_encoding.py::test_含非GBK字符的文件也能被搜索`、`tests/tools/test_search_encoding.py::test_搜索中文内容不会挂掉`、`tests/tools/test_view.py::test_搜索看到改动后的内容`。所以**跑之前先确认 `rg` 在 PATH 里**（`Get-Command rg`），否则会白跑一轮、并误把 6 条环境失败当回归。
  - 那 2 条既有失败（沙箱进程终止项，**不许去修**）的 nodeid 是：`tests/cli/test_service_command.py::test_能拉起再停掉自己的孩子`、`tests/cli/test_service_command.py::test_停的时候信号杀不掉也要能停`。除它们之外任何失败都算回归，必须当轮处理。
- **跑测试只用脚本**：`.\scripts\pytest.ps1`（worktree 加 `-Repo <路径>`，只跑一部分就把路径透传给它）。⚠️ 本机 PowerShell 执行策略会拦 `.ps1`（实测 `UnauthorizedAccess`）——被拦时按脚本体里的五件事手抄，一件都不能漏：`PYTHONPATH=<repo>\src;<repo>\scripts`、`PYTEST_ADDOPTS=-p pytest_sandbox_compat -p pytest_hang_guard`、`PYTEST_DEBUG_TEMPROOT=<仓库外新建空目录>`、`PYTHONUNBUFFERED=1`、`PATH` 里有 `rg`。
- **真实工作区 `D:\workSpace\agentWorkSpace` 只读**：只许跑 `--dry-run`（且 `memory.db` 只许以 `mode=ro` 打开）。不许写、不许改名、不许生成 `.bak`、不许生成 `-wal`/`-shm`。
- ⚠️ **绝对不许读该工作区的 `.env`**（真实密钥）。不许 `Get-Content`/`cat` 它，不许跑 `spool env`、`spool config`、任何需要密钥解析的命令指过去；dry-run 必须走"不起网关、不解析密钥"的离线路径，并用用例钉住（Task 6 验收 4）。
- **不动 8080 上的模型服务（pid 43096）**；**不许起长驻进程**（不跑 `spool web` / `spool serve` / `spool bench`）。
- 上限与阈值**不写死**：新增旋钮一律进 `limits.KNOBS`（`CAPABILITY`，可被 `.agent/limits.json` 覆盖），沿用既有机制（`limits.py:263-274`）。
- 中文注释与面向人的文案；标识符英文。
- **不 `git add` / 不 `git commit`**；本批只改本计划列出的文件（施工期最小文档回写：`docs/cli.md` 记一条 `spool memory migrate`、`docs/dogfooding.md` 记一条实测结果——若要求零文档改动，删掉即可，不影响代码验收）。
- 每个任务**先写会红的用例（红）→ 再实现（绿）**；不可证伪的验收不算验收。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/spoolkit/memory/gate.py` | 新增 | 判据 J1/J3/J4/J5(trigger)、`normalize_key`、`content_hash`、规则表与拒绝理由 |
| `src/spoolkit/memory/distill.py` | 改 | 删掉 259-263 的空条目兜底；`DistillResult` 形状不变 |
| `src/spoolkit/memory/archive.py` | 改 | promote 循环改成"判据 → 判重 → 落盘"；`ArchiveResult` 增 `duplicates` / `downgraded` |
| `src/spoolkit/memory/hot.py` | 改 | 分节内按 `importance_hint` 淘汰（替 `pop(0)`）；`hot_max_items` 生效 |
| `src/spoolkit/memory/store.py` | 改 | `SCHEMA_VERSION` + 步进迁移；`memory_fts` 4 列 + 回填；单一写入口；`open_db_readonly` |
| `src/spoolkit/memory/lessons.py` | 改 | `record_lesson` 走统一写入口（教训进 FTS） |
| `src/spoolkit/limits.py` | 改 | 新增 `remember_max_chars` / `hot_max_items` / `hot_item_chars` |
| `src/spoolkit/cli/commands/memory.py` | 新增 | `spool memory migrate [--dry-run|--yes]` |
| `src/spoolkit/cli/app.py` | 改 | 注册 `memory` 子命令（`--root` 口径同 `session`，`app.py:348-385`） |
| `tests/memory/test_gate.py` | 新增 | 判据正反例、理由字符串、规范化/hash |
| `tests/memory/test_schema_version.py` | 新增 | 版本步进、幂等、旧库升级、FTS 回填、前向兼容 |
| `tests/memory/test_migrate_command.py` | 新增 | dry-run 报告、`--yes` 备份与重写、幂等与边界 |
| `tests/memory/test_dryrun_readonly.py` | 新增 | 不读 `.env`、不动 `memory.md` / `memory.db` |
| `tests/memory/test_distill.py`、`test_hot.py`、`test_archive.py`、`test_store.py`、`test_lessons.py` | 改 | 改写钉住旧行为的用例 + 补新判据用例 |
| `docs/cli.md`、`docs/dogfooding.md` | 改（最小） | 新命令一句话 + 实测一条（施工期回写） |

## 任务顺序与依赖

```
Task 1 停噪声 ──┬─→ Task 2 去重 ─→ Task 3 淘汰序/容量
                └─→ Task 6 migrate ─→ Task 7 真实工作区 dry-run
Task 4 版本化 DDL ─→ Task 5 FTS 覆盖
Task 1..7 ─→ Task 8 回归与回滚演练
```

Task 2 复用 Task 1 的 `content_hash`；Task 5 依赖 Task 4 的版本步进；Task 7 依赖 Task 6（与 Task 1 的判据）。Task 3 与 Task 1/2 同改 `archive.py`，按顺序做以免冲突。

---

### Task 1: 停噪声（删兜底 + 判据进代码）

**目标：** 蒸馏返回空条目时不再把整段 `final` 写成 `kind="fact"`（`distill.py:259-263`）；进热记忆与进教训库的候选条目先过 J1/J3/J4（+ lesson 的 `trigger` 非空），寒暄、自述、一次性收尾、整段答复正文、同构表格都不进"项目事实"。

**改动面：**
- Create: `src/spoolkit/memory/gate.py`
- Modify: `src/spoolkit/memory/distill.py`（删 259-263）
- Modify: `src/spoolkit/memory/archive.py`（promote 循环 `85-97` 前置判据；`ArchiveResult` 增 `downgraded: list[tuple[str, str]]`）
- Modify: `src/spoolkit/limits.py`（新增 `remember_max_chars=400` 旋钮；设计稿 §4.3 已把名字与默认值定案）
- Test: Create `tests/memory/test_gate.py`；Modify `tests/memory/test_distill.py`（`211`、`222` 两条钉住旧兜底）、`tests/memory/test_archive.py`（补）

**要点：**
- 判据必须是**代码**，不是提示词（设计稿 §5.1 的理由：提示词约束对 7B 无效，三条重复寒暄就是证据）。规则表集中在 `gate.RULES`，每条带 `id`（`J1`/`J3`/`J4`/`J5`）与一句可打印的理由。
- P0 的 J1 只判**一次性指代**（`这次/本次/刚才/上面的/请确认/已完成` 这类；`memory.md:6` 的"你之前问过两次"也落在这里）；与 goal 的字重合度部分不做（dry-run 没有 goal，见 Task 6）。J4 = 长度 > `remember_max_chars` **或** ≥3 行同构表格 **或** 代码围栏 **或** 交付模板开头（`已完成`/`已经实现完毕`/`文件为`/`运行方式`/`操作说明`/`功能：`）。J3 = 问候/能力自述/致谢/复述用户（`你好`/`我是你的`/`感谢`/`你之前问过`）。J5 = `lesson` 必须有非空 `trigger`（`skill` 在 P0 不存在）。
- **判据不过 = 降级为 episode，不是丢弃**（§5.3 ②）：P0 的降级出口是 `memory` 表 `kind='episode'`（沿用下沉路径 `archive.py:102-109` 的 `add_memory`），并在 `ArchiveResult.downgraded` 里给出 `(文本, 理由)`。`audit` 表是 P1，P0 不建。
- 兜底删除后"任务结论"仍可查：`archive_task` 已经把 `final` 写进 `episode.summary`（`archive.py:62-68` + `summarize` 33-45，受 `episode_final_chars=400` 限制，`limits.py:186`）。**验收必须同时证明"热记忆里没有它"和"事件摘要里查得到它"**（设计稿 §9.5 P0 风险行点名了这条）。
- 规则表必须带**正例**：真正的项目事实（`测试命令是 pytest -q`）不许被拒——过宽的判据是另一种回归。

**验收标准（可证伪）：**
1. `distill(_model([_entries()]), state, final="得分规则是每吃一个 +10").entries == []`（旧代码返回 1 条 `fact`）。
2. `archive_task(..., promote=[("fact", "你好！我是你的本地编程助手。…")], final="你好！…")` 之后：`"fact" not in read_hot(hot_path)`；`result.downgraded` 含 `(..., "J3")`；`SELECT summary FROM episode` 的 summary 里仍含 `"结论：你好！…"`。
3. 正例不误杀：`promote=[("fact", "测试命令是 pytest -q")]` → `read_hot()["fact"] == ["测试命令是 pytest -q"]` 且 `downgraded == []`。
4. 无触发词的教训：`promote=[("lesson", "规则", "")]` → `SELECT count(*) FROM memory WHERE kind='lesson'` 为 0，`downgraded` 理由含 `J5`。

**证据要求：**
- 跑 `pytest tests/memory/test_gate.py tests/memory/test_distill.py tests/memory/test_archive.py -q`（经 `.\scripts\pytest.ps1` 口径）。
- 贴出三行实测：`distill(...).entries == []`、`ArchiveResult(downgraded=…)` 原文、`SELECT summary FROM episode` 的原文（证明"结论还在"）。
- `gate.RULES` 的正反例清单（至少各 6 条）随用例一起提交。

**依赖：** 无（Task 2 依赖它的 `content_hash`）。

**风险：** ① 规则过宽 → 真事实被拒（设计稿 §9.5 P0 风险行点名；缓解：episode.summary 兜住 + 正例用例）；② 规则过窄 → 噪声漏过（缓解：迁移 dry-run 就是一次真实语料体检，见 Task 7）；③ 降级出口写进 `memory.kind='episode'` 会让 `recall` 的候选池里出现被拒条目（噪声率下降但未清零）——见「风险与未知 3」。

**怎么证伪：** 恢复 `distill.py:259-263` 那五行 → 验收 1 红；把 `gate.judge` 的调用从 `archive.py` 的 promote 循环里删掉 → 验收 2 红；删掉 J4 的"交付模板开头"规则 → Task 7 的 dry-run 里 `memory.md:9/11` 从"降级"变"保留" → 判据红。

- [ ] 先写 `tests/memory/test_gate.py` 与改写后的 `test_distill.py`（红）
- [ ] 实现 `gate.py`、删兜底、archive 前置判据
- [ ] 正例/反例表补齐；三条改写用例在旧代码上确认红
- [ ] 跑局部测试转绿，记录证据

---

### Task 2: 跨任务/跨蒸馏结构性去重

**目标：** 同一条文本不再第二次入库——现在 `archive.py:96` 是无条件 `append`，`_merge` 只在 `distill.py:214` 的单次分治内生效。实测病灶是 `memory.md:4/5/8` 三条逐字相同（设计稿 §1 表）。

**改动面：**
- Modify: `src/spoolkit/memory/archive.py`（两个落点都先判重：热记忆 `append`（`96`）与降级下沉 `add_memory(kind="episode")`（`102-109`）；`ArchiveResult` 增 `duplicates: list[str]`）
- Modify: `src/spoolkit/memory/gate.py`（复用/微调 `normalize_key` / `content_hash`）
- Test: Modify `tests/memory/test_archive.py`（补 `test_跨任务同一事实不再二次入库`、`test_同一批里重复条目只留一条`、`test_库里已有同一事实时只记一次使用`）

**要点：**
- 判重是**精确级**（§5.3 ④ 第 1 级）：`content_hash = sha1(normalize_key(text))[:16]`，`normalize_key` = 去空白 + 去标点 + 全角转半角 + 小写（设计稿 §4.3 的原话）。比较对象三处：① 既有热记忆**同节**条目；② 本次已接受的条目（补 `_merge` 覆盖不到的部分）；③ `memory` 表既有行的 text。
- 命中时按 §5.3 ④："不新增 + touch 既有条目"——热记忆没有行可 touch，因此**库里有行就 `touch_memory`（`store.py:208-214`），没有就只在报告里记一次**；不新增任何行。
- **不假装做到语义去重**：同义改写（"用 uv 管理依赖" vs "本仓统一用 uv"）抓不到，这一条要写进 `gate.py` 注释与 `duplicates` 理由文案；相似度去重（`dedupe_similarity`）与冲突检测是 P1（§5.3 ④ 第 2 级、⑤）。

**验收标准（可证伪）：**
1. 同一工作区连续两次 `archive_task(..., promote=[("fact", "测试命令是 pytest -q")])` → `read_hot()["fact"] == ["测试命令是 pytest -q"]`（**一条**）；第二次 `result.promoted == []` 且 `result.duplicates == ["测试命令是 pytest -q"]`。
2. 一次 archive 里 promote 传两条重复 → 热记忆一条、`duplicates` 长度 1。
3. 规范化后相等即算重复：`"测试命令是 pytest -q"` 与 `"测试命令是 pytest -q。"` 视为同一条。
4. 库里已有该文本（先 `add_memory`）→ 再 archive 不进热记忆，且该行 `use_count` 从 0 变 1。
5. 降级下沉的落点也判重：同一条被拒条目连续两次归档 → `SELECT count(*) FROM memory WHERE kind='episode' AND text=?` 仍为 1。

**证据要求：**
- `pytest tests/memory/test_archive.py tests/memory/test_gate.py -q`；打印第二次归档的 `ArchiveResult` 原文（含 `promoted=[] duplicates=[…]`）。
- 在 tmp 工作区手工跑"两次归档同一事实"的可复现命令（写成用例即可，命令与用例名一并记录）。

**依赖：** Task 1（`content_hash` 与降级出口）。

**风险：** ① 把"同一事实的新版本"也当重复——P0 只做精确 hash，只有规范化后逐字相同才命中，所以这类误判**不会发生**，但代价是抓不到近似重复（见要点第 3 条）；② 判重发生在 `archive.py`，`distill._merge` 保持原样（它管的是单次分治内的合并，职责不变）。

**怎么证伪：** 把 `archive.py:96` 还原成无条件 `append` → 验收 1 红；把判重只留在热记忆落点、去掉降级落点 → 验收 5 红。

- [ ] 先写三条新用例（红）
- [ ] 实现两个落点的判重与 `duplicates` 报告
- [ ] 跑局部测试转绿

---

### Task 3: 同分节内不再 FIFO + 容量口径

**目标：** `hot.py:99-101` 的"分节内先淘汰最早的"与 `hot.py:29-30` 的注释（"事实最后才动"）互相矛盾；把它换成设计稿 §7.1 的新判据，并落实已定案的容量口径（`hot_budget` + `hot_max_items=40` + `hot_item_chars=200`）。

**改动面：**
- Modify: `src/spoolkit/memory/hot.py`（`trim_to_budget` 内改选牺牲者；新增 `importance_hint(text)` 与规则表；`hot_max_items` 生效；`DEMOTE_ORDER` 与其注释对齐）
- Modify: `src/spoolkit/limits.py`（新增 `hot_max_items=40`、`hot_item_chars=200`，均 `CAPABILITY`）
- Modify: `src/spoolkit/memory/archive.py`（写入侧：超过 `hot_item_chars` 的条目不进热记忆 → 下沉 `memory.kind='episode'`，`downgraded` 理由 `too_long`）
- Test: Modify `tests/memory/test_hot.py`（**必须改写** `72` `test_同一分节内先移除最早的条目`，它现在钉住的是旧行为）；Create/Modify 补 `test_同分节内低价值条目先被淘汰`、`test_条目数超上限时按重要度淘汰`、`test_超长条目不进热记忆`

**要点：**
- P0 的判据（写清是**启发式近似**）：优先级 = `(importance_hint(text), 位置)`，`importance_hint` 来自**一张规则表**——含"证据形状"（反引号里的路径/命令、`文件:行`、以命令名开头）加分；含一次性指代（`这次/本次/刚才/上面的/请确认/已完成`）扣分；越长越扣分。位置只作 tie-break（越早越先淘汰，保持既有顺序语义）。
- 设计稿 §7.1 的三元组是 `(重要性, confidence, created_at)` 升序；P0 的热记忆是**纯 Markdown**、没有逐条元数据（决议 6 + `hot.py:6-9` 要求格式不变），`confidence`/`created_at` 没有承载 → P0 只能近似。**代码注释与任务报告都要写清"这是 §7.1 的 P0 近似，P1 拿到 `semantic` 元数据后按三元组替换"**，不许说成"已实现 §7.1"。
- 数量上限在 `trim_to_budget` 里生效，所以**读路径**（`session.hot_text()` → `trim_to_budget`）与写路径（`archive.py:99-101`）同时受益；`hot_item_chars` 是**写入侧**拒绝线，存量超长条目靠 `importance_hint` 的长度扣分被优先淘汰（真实工作区 `memory.md:7` 那条 400 余字的天气表就是这样先被挤出去的），彻底清理由 Task 6 的迁移负责。
- 容量只有一个出处这条既有纪律不破：`hot_budget` 仍从 `hot_memory_ratio` 取值（`hot.py:40-43`、`limits.py:136-137`）。

**验收标准（可证伪）：**
1. `sections = {"fact": [含路径与命令的旧条目, "一条泛泛的当前状态记录（这次）"]}`，预算只够留一条 → 留下的是**旧的那条**（有证据形状），被淘汰的是**新的**那条。旧代码 `pop(0)` 淘汰旧条目 → 旧行为上必红。
2. `hot_max_items` 生效：写入 41 条 → 热记忆留 40 条，被淘汰者进了 `memory` 表（`kind='episode'`）。
3. `201` 字的条目按 `hot_item_chars=200` 不进热记忆，`downgraded` 理由含 `too_long`，且仍能在 `memory` 表里查到（降级不丢）。
4. 分节间顺序不变：`DEMOTE_ORDER[0] == "doing"`、`DEMOTE_ORDER[-1] == "fact"`；`hot_budget(8000) == int(8000 * 0.85 * 0.15)`（既有用例 `tests/memory/test_hot.py:48`、`67` 继续绿）。
5. 旋钮可覆盖：`limits.knob("hot_max_items").default == 40` 且 `.overridable` 为真。

**证据要求：**
- `pytest tests/memory/test_hot.py tests/memory/test_archive.py -q`（再补一次 `-k "hot"` 的单点复现）。
- 打印验收 1 的两次结果：新实现淘汰的是"新条目"、旧实现淘汰的是"旧条目"（把 `pop(0)` 临时改回来的那次输出也留着，作为"改回旧行为会红"的现场证据）。

**依赖：** 无强依赖；与 Task 1/2 同改 `archive.py`，按顺序做。

**风险：** ① 启发式误判只影响"谁先被淘汰"，不影响是否入库，且当前真实工作区 9 条、占用 1011/1044 token（设计稿 §1 实测），`hot_max_items` 短期不会触发；② 启发式不是设计稿的最终形态（见要点第 2 条）——`importance_hint` 的规则表要对得起"可解释"：每条规则一个理由字符串。

**怎么证伪：** 把 `trim_to_budget` 改回 `working[kind].pop(0)` → 验收 1 红（改写版 `test_同分节内低价值条目先被淘汰` 在旧代码上红）；删掉 `hot_max_items` 的检查 → 验收 2 红；删掉写入侧 `hot_item_chars` 判定 → 验收 3 红。

- [ ] 先改写 `test_同一分节内先移除最早的条目` 并新增三条用例（红）
- [ ] 实现 `importance_hint` + `hot_max_items` + `hot_item_chars`
- [ ] 跑局部测试转绿，留下"旧实现红"的现场输出

---

### Task 4: `user_version` 版本化 + `episode` 只加列与索引

**目标：** `init_memory_schema` 从 `executescript(MEMORY_SCHEMA)` 一把梭（`store.py:105-108`）改成 `PRAGMA user_version` 步进迁移（纯 `IF NOT EXISTS` 无法给既有表加列）；给 `episode` **只**加 §4.3 块 3 的 5 列与 2 个索引；`memory` 表结构与 `memory_fts` 在这一步里一字不动；每一步幂等（跑两次结果一致）。

**改动面：**
- Modify: `src/spoolkit/memory/store.py`（`SCHEMA_VERSION`、步骤表 `_MIGRATIONS`、`init_memory_schema` 逐步执行 + 记 `user_version`；保留 `MEMORY_SCHEMA` 作为"新库第一步"）
- Test: Create `tests/memory/test_schema_version.py`

**要点：**
- 新列与索引按设计稿 §4.3 块 3 原样：`scope DEFAULT 'session'`、`scope_id DEFAULT ''`、`verified DEFAULT 0`、`verify_detail DEFAULT ''`、`archived DEFAULT 0`，索引 `idx_episode_session(session_id, id DESC)`、`idx_episode_created(created_at DESC)`。
- **P0 不写这些列**（没有写入者：`verify_detail` 是决议 5 的 P1 语义，`archived` 是决议 8 的 P2 语义）——P0 只保证"结构就位 + 旧行取默认值 + 旧代码照读"。
- 前向兼容：库里 `user_version > SCHEMA_VERSION`（用户装回过旧版本）→ **不执行任何步骤、不报错**，打印一句说明。
- `memory` 表 DDL 与列清单**逐字不变**（`plans/2026-09-24-workspace-session-kernel.md:1156` 的老规矩 + 决议 7）：用例把 `sqlite_master` 里 `memory` 的 SQL 与固定字符串对比。

**验收标准（可证伪）：**
1. 旧库 fixture（用**改造前**的 `MEMORY_SCHEMA` 建出来、`user_version=0`）→ `init_memory_schema` 后 `PRAGMA user_version == SCHEMA_VERSION`，`PRAGMA table_info(episode)` 含 5 个新列，旧行取默认值（`scope='session'`、`verified=0`）。
2. 幂等：连跑两次 → `table_info` 与 `user_version` 不变、`episode` 行数不变、不抛异常。
3. 新库（空文件）→ 一步到位得到同一结构。
4. `memory` 表未被改：`[r["name"] for r in table_info(memory)]` 等于固定列表，且其建表 SQL 与改造前逐字一致。
5. 前向兼容：把 `user_version` 手工设成 `SCHEMA_VERSION + 1` → 再跑不报错、版本号不被写回低值。

**证据要求：**
- `pytest tests/memory/test_schema_version.py -q`。
- 在**真实工作区的只读副本**上验证：`Copy-Item D:\workSpace\agentWorkSpace\.agent\memory.db .tmp\migrate-probe.db`（复制到本仓库的 `.tmp\`，原库不动）→ 对副本跑一次 `init_memory_schema` → 打印 `PRAGMA user_version` 与 `table_info(episode)`。⚠️ **不许在原库上跑**。

**依赖：** 无（Task 5 依赖它）。

**风险：** ① `ALTER TABLE ADD COLUMN` 是元数据操作（不重写表），但**跑过就留下列**；回滚 = 代码回退（旧代码 `SELECT *` 不看新列，不会炸），**不做 `DROP COLUMN`**（那才是不可逆动作）；② 多进程并发初始化（本仓前提是"同一个工作区同时只跑一轮"，`rationale.md:126`）不在 P0 处理范围。

**怎么证伪：** 把 `init_memory_schema` 改回 `executescript(MEMORY_SCHEMA)` → 旧库 fixture 上 `table_info(episode)` 不含 `verified` → 验收 1 红。

- [ ] 先写旧库升级 / 幂等 / 前向兼容三条用例（红）
- [ ] 实现步骤表与 `user_version`
- [ ] 在 `memory.db` 副本上复验，留下输出

---

### Task 5: FTS 覆盖（教训 + `kind`/`trigger`/`source`）

**目标：** 教训写库即写 FTS（治缺陷 4：`lessons.py:60-66` 只写主表）；`memory_fts` 从 `fts5(text)`（`store.py:41`）扩到 `text/kind/trigger/source` 四列，`kind`、`trigger`、`source` 都可检索；写入同步责任收成**一处 Python 写入口**。

**改动面：**
- Modify: `src/spoolkit/memory/store.py`（版本步进里重建 `memory_fts` 为 4 列 + 从 `memory` 全表回填；`add_memory` 一次写入四个字段；新增统一写入口 `insert_memory`，`add_memory` 与 `record_lesson` 都走它）
- Modify: `src/spoolkit/memory/lessons.py`（`record_lesson` 不再裸 `INSERT`）
- Test: Modify `tests/memory/test_store.py`（补 `kind`/`trigger`/`source` 检索）、`tests/memory/test_lessons.py`（补"教训可被检索"）、`tests/memory/test_schema_version.py`（补回填与幂等）

**要点：**
- 回填必须在 **Python 侧**做（`SELECT id, text, kind, trigger, source FROM memory` → `INSERT INTO memory_fts(...)`），因为中文要逐字拆（`store.py:66,82-84`），SQL 触发器里没有这个函数——这是「范围裁决 5」那条。
- `memory_fts` **保持非 external content**：既有用例 `tests/memory/test_store.py:99`（`DELETE FROM memory_fts WHERE rowid=?`）依赖这个形态，不要顺手改成 external content。
- 检索路径不变（`_match_query` 仍是单字 OR；相关度/`bm25` 排序是 P1 的 §6.1）。P0 只承诺"**查得到**"，不承诺"排得对"。

**验收标准（可证伪）：**
1. `record_lesson(conn, "解析自定义格式先上语法约束", "解析,正则,格式", "ep#12")` → `search_memories(conn, "正则")` 命中 1 条（旧代码 0 条）。
2. `add_memory(conn, kind="fact", text="索引层用 ast 提取符号")` → `search_memories(conn, "fact")` 命中（旧代码 0 条：`kind` 不在索引里）。
3. `search_memories(conn, "ep#12")` 命中该教训（按来源回溯，旧代码 0 条）。
4. 旧库升级后**既有行**也可按 `kind`/`trigger`/`source` 检索（回填生效）：fixture 用旧代码路径写入 1 条 lesson（只进主表）+ 1 条 fact → 升级 → 两条都能被新字段检索到。
5. 既有检索行为不回归：`tests/memory/test_store.py` 全绿（英文/中文命中、不误命中、部分词召回、删除同步索引）。

**证据要求：**
- `pytest tests/memory/test_store.py tests/memory/test_lessons.py tests/memory/test_schema_version.py -q`。
- 用**真实 `memory.db` 副本**跑回填并打印：`SELECT count(*) FROM memory`、`SELECT count(*) FROM memory_fts`、三条检索命中数（真实库 `memory` 表当前 0 行——设计稿 §1 实测——所以回填是空操作，这本身就是一条要如实报告的证据）。

**依赖：** Task 4（版本步进）。

**风险：** ① 重建 `memory_fts` 是**索引**动作，内容可从 `memory` 重算，不是底账；步骤先建索引后记 `user_version`，中途失败下次重跑即可；② `kind` 进索引后，查英文词 `fact`/`lesson` 会命中整类条目（这是设计稿 §2.2 点名要的效果，但也意味着"查什么都能捞到一堆"的观感变强）——相关度排序是 P1；③ 大库回填耗时未测（见「风险与未知 6」）。

**怎么证伪：** 把 `record_lesson` 改回裸 `INSERT`（不写 FTS）→ 验收 1 红；把 FTS DDL 改回 `fts5(text)` → 验收 2/3 红；把回填步骤删掉 → 验收 4 红。

- [ ] 先写 5 条用例（红）
- [ ] 实现统一写入口 + 4 列 FTS + 回填步骤
- [ ] 跑局部测试转绿，在真实库副本上复验

---

### Task 6: 迁移命令（dry-run → 门禁 → 改名保留）

**目标：** 给旧 `memory.md` 一条可解释、可拒绝、可回滚的路径：默认 `--dry-run` **只读报告**；`--yes` 才动文件（**先改名保留原文，再写过滤后的新文件**）。

**改动面：**
- Create: `src/spoolkit/cli/commands/memory.py`
- Modify: `src/spoolkit/cli/app.py`（注册 `memory` 子命令，`--root` 口径照 `session`，`app.py:348-385`）
- Modify: `src/spoolkit/memory/store.py`（新增 `open_db_readonly(path)`：`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`——`store/db.py:52-58` 的 `open_db` 总是创建/写，dry-run 不能用它）
- 复用：`hot.parse_hot` / `render_hot`（`hot.py:61-87`）、Task 1 的 `gate.judge`
- Test: Create `tests/memory/test_migrate_command.py`、`tests/memory/test_dryrun_readonly.py`

**报告格式（stdout，四栏 + 逐条理由；干跑不写任何文件）：**

```
memory.md 迁移预演（--dry-run：不写文件、不改库、不解析密钥）
  解析：<N> 条（memory.md:<行号>）
  收进热记忆（<a> 条）
  判重 / 判据拒（<b> 条）
    - [duplicate] 你好！我是你的本地编程助手。…（与 memory.md:4 规范化后相同）
    - [J3]        你好！我是你的本地编程助手。…（寒暄/自述）
  降级为 episode（<c> 条）
    - [J1] 你之前问过两次同样的问题…（一次性指代/复述用户）
    - [J4] 12 城市天气表…（≥3 行同构表格）
    - [J4] 贪吃蛇小游戏已经实现完毕，文件为 snake.py…（交付答复模板）
      已在 episode#<id> 的 summary 里（可核对：LIKE 命中）
  未判：J2（evidence）与 scope 属 P1，本报告不据此判任何一条
  结论：新 memory.md 将只剩「收进热记忆」的条目；原文件改名为 memory.md.bak-<日期>
```

**要点：**
- `--dry-run` 是**默认**；执行要显式 `--yes`；两者互斥。
- `--yes` 的顺序不可颠倒：先 `memory.md` → `memory.md.bak-YYYY-MM-DD`（同名已存在则追加 `-2`/`-3`，**绝不覆盖任何既有文件**），再写新文件（`write_hot`，分节标题与格式不变）。
- 迁移**只动 `memory.md` 一个文件**：不建表、不写 `memory` 行、不动 `user_version`。设计稿 §9.2 第 2 步说降级是"重定向而不是新写"（那些正文已经在 `episode.summary` 里）——报告里对每条降级条目做一次**只读** `SELECT id FROM episode WHERE summary LIKE ?` 核对，命中就写 `episode#<id>`，**没命中就如实写"未命中"**，不许编。核对用**规范化后前 20 个字符**做 `%…%` 匹配（`episode.summary` 的结论段受 `episode_final_chars=400` 截断，拿整条去比会假阴性）。
- **同一节内的先后**：先做规范化判重（逐条比对，保留**第一条**作锚点），再对锚点跑判据。所以三条逐字相同的寒暄会呈现为"1 条 `J3` 拒 + 2 条 `duplicate`"，`duplicate` 的理由里要注明"锚点自己也没入库（J3）"，否则读报告的人会以为留下了一条。
- 文件不存在 → 打印"无可迁移"、退出码 0、不建文件。
- 命令走离线路径：不构造网关、不调 `settings` 的密钥解析。**实现口径**：`memory` 子命令**不要定义 `provider` 属性**——`app.py:1148-1169` 的"缺供应商向导 + `settings.describe_missing()`（`:1155`）+ `resolve_provider_args`"整段都在 `hasattr(args, "provider")` 之后，没有这个属性就一个字节的 `.env` 都不会被读；`--root` 仍照既有口径在 `app.py:1122-1139` 解析一次。

**验收标准（可证伪）：**
1. tmp 工作区（寒暄×3 逐字相同 + 天气表 + 交付答复 + 真事实）跑 `--dry-run`：四栏计数与理由正确；`memory.md` 内容与 mtime **不变**；目录清单**无新增文件**（无 `.bak`）。
2. `--yes`：出现 `memory.md.bak-<日期>`，其内容与迁移前原文**逐字节相同**；新 `memory.md` 只含真事实（`read_hot()` == `{"fact": ["测试命令是 pytest -q"]}`）。
3. 边界：再跑一次 `--yes` → 第二个备份带序号、第一个不被覆盖；空工作区 → "无可迁移"且不建文件。
4. **不读 `.env`**：tmp 工作区放一个含哨兵的 `.env`，monkeypatch 密钥解析路径 → 跑 `--dry-run` 与 `--yes` 都不触发；用例名 `test_migrate_不解析工作区env`。
5. **只读打开库**：用例 `test_dry_run不改动memory_db`——记录 `memory.db` 的字节与 mtime → 跑 `--dry-run` → 断言两者不变且目录里没有新增 `-wal`/`-shm`。

**证据要求：**
- `pytest tests/memory/test_migrate_command.py tests/memory/test_dryrun_readonly.py -q`。
- 贴出 tmp 工作区 `--dry-run` 的完整报告原文、`--yes` 后的 `Get-FileHash`（备份与原文一致）与 `read_hot` 结果。

**依赖：** Task 1（`gate.judge`）。

**风险：** ① `memory.md` 是**人可能手改过**的文件（§9.2）——所以 `--yes` 是唯一会动它的路径，且"先备份后写"保证写失败也不丢原文；② 真实语料的分类可能与设计稿预期不一致（见 Task 7），**不许为了对上预期而放宽判据到误杀真事实**，只许补规则 + 补用例。

**怎么证伪：** 把 `--yes` 的顺序倒过来（先写新文件再备份）→ 验收 2 的"备份逐字节等于原文"红；让 `--dry-run` 顺手做修复（删掉判据不过的条目）→ 验收 1 的文件不变断言红；让 `--dry-run` 用 `open_db`（可写）打开 → 验收 5 红。

- [ ] 先写 `--dry-run` 报告与只读两条用例（红）
- [ ] 实现命令与 `open_db_readonly`，注册子命令
- [ ] `--yes` 的备份/重写/幂等用例转绿

---

### Task 7: 真实工作区 dry-run（只读验收）

**目标：** 对 `D:\workSpace\agentWorkSpace` 跑一次 `--dry-run`，报告与预期一致；并证明**没有改动那个工作区**、**没有读它的 `.env`**。

**改动面：** 无（只跑命令 + 留证据）。若分类与预期不一致 → 回到 Task 1/6 补规则 + 补用例，**不改设计稿、不改真实文件**。

**命令（在仓库根、用 venv 里的入口）：**

```powershell
& .\.venv\Scripts\spool.exe memory migrate --root D:\workSpace\agentWorkSpace   # 默认就是 --dry-run
```

（`spool.exe` 已确认存在于 `.venv\Scripts\`；若哪天不在，退到 `.\.venv\Scripts\python.exe -m spoolkit.cli.app memory migrate --root ...`——`app.py:1173` 有 `__main__` 入口。）

**判据（预期报告，逐条对齐）：**
- 解析 **9 条**（`memory.md:4,5,6,7,8,9,10,11,12`；本计划只读复看过该文件，构成与设计稿 §1 的表一致）。
- **3 条逐字相同的寒暄全部不入库**：`:4` → `[J3]`（寒暄/自述）；`:5`、`:8` → `[duplicate]`（与 `:4` 规范化后相同）。合计 3 条落在"判重 / 判据拒"栏。
- **4 段整段答复 + 1 张天气表降级为 episode**：`:9`、`:10`、`:11`、`:12` → `[J4]`（交付答复模板 / 代码围栏 / 一次性收尾）；`:7` → `[J4]`（≥3 行同构表格）。合计 5 条。
- 另有 `:6`（"你之前问过两次同样的问题…"）→ `[J1]`（一次性指代/复述用户）降级。合计降级 **6 条**。
- **收进热记忆：0 条**。新 `memory.md`（只在 `--yes` 时才写）将只剩标题。

**只读证明（证据要求，每条都要有输出）：**
1. 跑前 / 跑后：`Get-FileHash .agent\memory.md, .agent\memory.db -Algorithm SHA256` 与 `LastWriteTime` 一致；`Get-ChildItem .agent` 无新增文件（尤其**没有** `.bak`、`-wal`、`-shm`）。
2. 执行记录里没有任何读 `.env` 的动作：不跑 `spool env` / `spool config`，不 `Get-Content` 那个文件；命令输出里不出现任何密钥值。
3. `--dry-run` 退出码 0；报告四栏计数与上面判据一致（不一致按下面"风险"处理，不许改判据凑数）。

**依赖：** Task 6（命令）+ Task 1（判据）。

**风险与未知（这条任务的真实状态）：**
- **本计划没有跑过这条 dry-run**：上面那份"预期"来自设计稿 §9.2 的推断 + 本计划对 `memory.md` 的一次只读复看，**不是实测输出**。执行者必须把真实输出原样留证，不许把它写成"与预期一致"了事。
- 若真实报告与预期不一致：① **分类不同**（例如某条被 `J1` 而不是 `J3` 命中）→ 记差异、补规则或补用例，并在报告里如实写"与设计稿 §9.2 点名的规则不同"；② **计数不同**（文件已被后续任务改过）→ 以**当次只读复看的实际条数**为准，写清与设计稿的差在哪；③ 无论哪种，**都不许改设计稿、不许改真实文件、不许把推断写成实测**。

**怎么证伪：** 把 `--dry-run` 实现成"顺手修复"（例如把判据不过的条目从文件里删掉）→ 只读证明 1 红；把 J3 规则表删掉 → 3 条寒暄从"拒"变"保留" → 判据红。

- [ ] 跑前留 `Get-FileHash` + 目录清单
- [ ] 跑 dry-run，原样保存报告
- [ ] 跑后复验哈希/清单，对齐判据，差异如实记录
- [ ] 差异若来自判据 → 回 Task 1/6 补规则与用例

---

### Task 8: 回归基线与回滚演练

**目标：** 证明 P0 之后"除既有 2 条沙箱失败外无回归"，并证明每一步都能回滚。

**改动面：** 无（跑测试 + 记录）；发现回归就在对应任务里修，**不许去修那 2 条既有失败**。

**验收标准（可证伪）：**
1. 基线：`.\scripts\pytest.ps1` → `2096 passed / 2 failed / 1 skipped`（收集数 2099 已复跑确认）；2 failed 的 nodeid 必须是 `tests/cli/test_service_command.py::test_能拉起再停掉自己的孩子` 与 `::test_停的时候信号杀不掉也要能停`（**同一**两条，不许换人）。
2. P0 完成后：`failed` **仍恰好这 2 条且 nodeid 相同**；`passed` = 2096 + 新增用例数；`skipped ≤ 1`（若新增 skip，必须写出原因，不许默认放过）。若 P0 后多出 `tests/tools/test_search*` / `test_view` / `test_read_roots` 这类失败，先查 `rg` 在不在 PATH（那是环境，不是回归）。
3. 回滚演练（在 **tmp 工作区**做，不碰真实工作区）：
   - 文件层：`memory.md.bak-<日期>` 改回 `memory.md` → `read_hot` 回到迁移前内容；
   - 库层：`memory_fts` 用旧定义 `fts5(text)` 重建 + 按 `text` 回填；`episode` 新列留着不用（旧代码 `SELECT *` 照读）；
   - 代码层：回退本批改动 → 全套测试回到基线；
   - 断言 `sqlite_master` 里 `memory` 与 `memory_fts` 两个名字都还在，且 `memory` 表行数演练前后一致（只读保留，决议 7）。
4. `git status` 只显示本批改动的文件，**没有任何 commit**。

**证据要求：** 两轮 pytest 的尾部统计（带 `-rf`）+ 演练命令与输出 + `git status` 原文。

**依赖：** Task 1-7。

**风险：** 新增用例可能踩到沙箱（子进程 / 文件权限）——若出现这类失败，按既有 2 条同样的口径**记录**在任务报告里，不许静默忽略，也不许顺手"修好"既有那 2 条。

**怎么证伪：** 把某个任务的实现回退一格再跑全套 → 若"无回归"仍成立，说明那条验收没有钉住任何东西（用例不算证据），回去补。

- [ ] 跑基线并核对 nodeid
- [ ] 跑 P0 后全套并核对 failed/skipped
- [ ] 执行三步回滚演练并留证
- [ ] 记录差异与未解释的 skip

---

## 回滚方案（逐步：旧文件改名保留、旧表只读、代码回退即可）

| 步 | 动作 | 回滚 | 残留 | 可逆性 |
|---|---|---|---|---|
| T1 判据与删兜底 | 代码 | 回退代码 | 无（被拒条目本来就在 `episode.summary` / `memory.kind='episode'` 里，信息不丢） | 完全可逆 |
| T2 去重 | 代码 | 回退代码 | 无（只是少写条目） | 完全可逆 |
| T3 淘汰序/旋钮 | 代码 + `limits.KNOBS` | 回退代码；或删掉 `.agent/limits.json` 里的覆盖项 | 无 | 完全可逆 |
| T4 `user_version` + `episode` 加列 | DDL | 代码回退（旧代码不看新列）；**不做 `DROP COLUMN`** | 5 个空列 + 2 个索引留在库里 | 只增不减，无害 |
| T5 FTS 4 列 + 回填 | 索引 | 用旧定义 `fts5(text)` 重建 + 按 `text` 回填（底账是 `memory` 表） | 无（索引可重算） | 可重建 |
| T6 `memory.md` 迁移 | 文件 | `memory.md.bak-<日期>` 改回 `memory.md` | 备份文件（**有意保留**） | 完全可逆 |
| T7 真实工作区 dry-run | 无 | —— | 无（只读 + 报告） | 不适用 |

一句话：**P0 没有不可逆动作**——旧文件改名保留、旧表（`memory` / `memory_fts`）只读保留、`episode` 只加列，回滚 = 代码回退（必要时加一次索引重建）。

---

## 风险与未知（未取证 / 待测量，**不许当结论用**）

1. **真实工作区的 dry-run 结果未取证**：设计稿 §9.2 的"3 条寒暄被拒 / 4 段答复与天气表降级"是推断，不是实测输出；本计划对 `memory.md` 只做了一次只读复看（9 条，构成与 §1 表一致）。真实报告见 Task 7 的证据。
2. **P0 判据的假阳率未测**：M3 噪声率要"机器判定 + 人工抽 20 条复核"（§10.3），P0 不做评测，所以"没有把真事实拒掉多少"目前只有用例级别的保证。
3. **降级出口的副作用未测**：被拒条目落进 `memory.kind='episode'`（沿用既有下沉路径）后，`recall` 的候选池会含这些条目——噪声率下降但没有清零。要不要改成"只报告不落库"，见「待拍板 6」。
4. **`hot_max_items=40` / `hot_item_chars=200` 的影响未测**：两个值来自设计稿 §4.3 的定案，但对"真事实会不会因此进不了热记忆"没有实测（真实工作区当前 9 条、占用 1011/1044 token 是设计稿 §1 的实测）。
5. **`importance_hint` 只是 §7.1 三元组的近似**：P0 的热记忆没有逐条 `confidence`/`created_at` 承载（决议 6 要求格式不变）。
6. **FTS 回填在大库上的耗时未测**：真实工作区 `memory` 表 0 行（设计稿 §1 实测）→ 空操作；别的库若有几千行，回填是 Python 循环。
7. **两把尺子没合一（缺陷 10 不在 P0）**：`MemorySession` 仍用 `OfflineTokenCounter`（`runtime.py:626`），装配用真实分词器（`runtime.py:482`），估算偏高约 25%（`docs/benchmarking.md:499`）——所以"热记忆超没超预算"与装配的判断仍可能不同时成立。
8. **评测（M1/M2/M3）本计划不做**：`bench` 不在 P0 范围内；8080 上的服务（pid 43096）本批不碰。
9. **§4.3 原则二的 SQL 触发器在中文检索上落不了地**（触发器拿不到逐字拆的函数）：这条不只影响 P0 的 `memory_fts`，**同样会落在 P1 的 `semantic_fts`** 上；本计划只在 `memory_fts` 上绕开，P1 之前要定。
10. **J2（`evidence`）在 P0 不可判**：dry-run 报告里的每一条都**没有**经过"有出处"这道检查，报告必须写明这一点。

## 待用户拍板（本计划按用户指令先这么定）

1. `episode` 加列 + `user_version` 是否算 P0（§9.5 把迁移标在 P1 ⑤）。
2. `spool memory migrate` 的执行形态（§9.2 第 3 步）是否进 P0，以及 P0 版"新 `memory.md` 只保留通过判据的条目"（不是 §9.2 第 4 步的 `semantic` 重渲染）。
3. `memory_fts` **重建**（本计划方案，可按列过滤）vs "把 `kind`/`trigger`/`source` 拼进 `text` 列"（不动索引结构，但不能按列过滤）。
4. §4.3 原则二（external content + 触发器）与中文逐字拆的冲突怎么解（见「风险 9」）。
5. 缺陷 10（两把尺子合一）要不要提前到 P0——它会改变每轮注入的实际 token 量，属于"容量口径"的另一半。
6. 被拒条目的 P0 出口：落 `memory.kind='episode'`（可检索、候选池变脏）vs 只报告不落库（安静，但违背"降级而非丢弃"的字面）。本计划选了前者。

## Self-Review 提醒（每个任务收尾前）

1. 有没有把"这次"的意图写进代码（阈值、路径、条目名）？该进 `limits.KNOBS` 的进了吗？
2. 新路径的**失败路径**（文件不存在、库打不开、条目为空、备份重名）都测了吗？
3. 这条改动的"改回旧行为会红"是**哪条用例**？它现在真的红吗（临时回退验证一次）？
4. 有没有顺手做了 P1/P2（`semantic` / `remember` / 重排序 / 巩固 / 嵌入）？
5. 旧文件、旧表、旧命令还通吗（`memory` 表 SQL 一字未改、`memory_fts` 名字还在、`recall` / `match_lessons` 行为不回归）？
6. 证据留了吗（命令、输出、nodeid）？有没有把"预期"写成"实测"？
