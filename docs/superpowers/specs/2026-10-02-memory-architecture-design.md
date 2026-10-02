# 记忆子系统架构 —— 设计文档

日期：2026-10-02
状态：待评审
前置：[`2026-09-11-local-coding-agent-design.md`](2026-09-11-local-coding-agent-design.md) §4.4（记忆系统的原始设计）、
[`2026-09-24-workspace-session-kernel-design.md`](2026-09-24-workspace-session-kernel-design.md)（会话一级的归属）

> 本文件只做设计，**不含任何代码改动**。文中所有"现状"论断都指到 `文件:行`；
> 标 **[实测]** 的数字来自本次对该仓库与真实工作区（`D:\workSpace\agentWorkSpace`）的只读取证，
> 复现命令写在附录 A；**本文档没有取证到的部分**（含"未跑过 50 题回归集"）在附录 B 如实列出。
>
> **2026-10-02 增补**：§11 的 8 条开放决策已由产品负责人拍板，决议落在 **§13**；
> 嵌入依赖的**选型、实测数据与上下文预算纪律**落在 **§14**（本轮主体）。
> 其余章节只在**与决议冲突处**做了最小改动，**章节编号保持稳定**（改动清单见 §13.9）。

---

## 1. 为什么做这一件事

产品负责人的判断是原话：

> P3 记忆问题也值得关注，它是 Agent 长任务执行下的成功率、准确率保障，也是多轮任务中
> 节约 token、提高效率的重要手段。**当前实现方式一致认为过于简陋，无法支撑产品作为一个
> 成熟的商业项目架构。**

这条判断能不能落到证据上？能，而且比"简陋"更具体：**现在的记忆系统在真实工作区里
已经处于"写进去的是噪声、想查的查不到"的状态。**

**[实测]** 真实工作区 `D:\workSpace\agentWorkSpace\.agent\memory.md`（3192 字节 / 1531 字符，
最后写入 2026-10-02 10:38）的"项目事实"一节里 9 条全部是**整段助手回复**：

| 行 | 内容（截断） | 长度 |
|---|---|---|
| `memory.md:4` | `你好！我是你的本地编程助手。…` | 42 |
| `memory.md:5` | `你好！我是你的本地编程助手。…`（**与上一行逐字相同**） | 42 |
| `memory.md:8` | `你好！我是你的本地编程助手。…`（**第三次**） | 42 |
| `memory.md:9` | `贪吃蛇小游戏已经实现完毕，文件为 snake.py…` | 244 |
| `memory.md:10` | `贪吃蛇是交互式终端游戏，我无法直接替你启动它…` | 168 |
| `memory.md:11` | `网页版贪吃蛇已经写好了，文件为 snake.html…` | 267 |
| `memory.md:12` | `任务已完成。请确认最终文件状态并宣告完成。` | 67 |
| `memory.md:6` | 一段"你之前问过两次同样的问题"的**会话回顾** | 202 |
| `memory.md:7` | 一张 12 城市的**天气表格** | 401 |

**[实测]** 同一个工作区的 `memory.db`：`memory` 表 **0 行**、`memory_fts` **0 行**、
`episode` **26 行**、`transcript` 53 行、`session` 9 行。

把这两条摆在一起，缺口就清楚了：

1. **写进去的是噪声**：寒暄、自述、"请确认"这类一次性输出被当成"项目事实"，且**三条逐字重复**；
2. **该查的查不到**：`recall` 查的是 `memory` 表与它的 FTS 索引——而这两张表是空的，
   所以模型调 `recall` 时什么都拿不到（`memory/tools.py:36`、`memory/session.py:53`）；
3. **热记忆已经顶到容量墙**：**[实测]** 1011 token / 上限 1044 token（= `8192 × 0.85 × 0.15`），
   **占用 96.8%**。也就是**再写一条事实，就会有一条既有事实被挤下去**
   （`memory/hot.py:99-105`）——而被挤下去的那些恰好是最早写入的那些。

一句话：**这一层现在不是"简陋的能力"，是"正在起反作用的负债"。**
每轮都要付它约 1011 token（`agent/loop.py:544`、`451-452`），换回来的是 9 条噪声。

---

## 2. 现状（可核对）

### 2.1 分层与存储：四层其实只有两个半

| 层 | 存哪 | 谁写 | 谁能读 |
|---|---|---|---|
| 热记忆（每轮注入） | `memory.md`，Markdown 分节 | `archive_task` 的 `promote`（`memory/archive.py:96`） | 每轮全量注入（`memory/session.py:39-49`） |
| 冷记忆（按需检索） | `memory` 表 + `memory_fts`（`memory/store.py:18-41`） | `add_memory`（`:111`）、`record_lesson`（`memory/lessons.py:52`） | `recall` 工具（`memory/tools.py:44`） |
| 情节 | `episode` 表（`memory/store.py:32-39`） | `add_episode`（`:217`），每次任务收尾必写 | `search_episodes` 的 LIKE（`:234`） |
| 教训 | 同上 `memory` 表，`kind='lesson'` | `record_lesson`（`lessons.py:52`）、`plan.py:643` | 主动推送 `match_lessons`（`lessons.py:82`） |

`hot.py:18-25` 定义的六个分节里，`doing`（进行中）**没有任何写入者**——
`distill.KINDS = ("fact","preference","decision","lesson")`（`memory/distill.py:31`）不含它，
`archive_task` 每次收尾还主动 `pop("doing")`（`archive.py:82`）。也就是说这一节是死的。

### 2.2 FTS5 覆盖了什么

`memory/store.py:41`：

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(text);
```

**[实测]** 这张表**只索引 `text` 一列**，全文检索的后果：

- `kind` 不在索引里 → 查 `"fact"` 命中 **0 条**；
- `trigger` 不在索引里 → 一条教训的触发词写 `"解析,正则,格式"`，查 `"正则"` 命中 **0 条**；
- `source`（`ep#12`）不在索引里 → 无法按来源回溯；
- 没有 `external content` 也没有任何触发器（**[实测]** `trigger` 数 = 0），
  即 **FTS 与主表靠调用方手工同步**：`add_memory` 手工插一条 FTS（`store.py:127-130`），
  `record_lesson` **只写主表、完全不写 FTS**（`lessons.py:60-66`）——
  于是每一条教训对 `recall` 都是不可见的。

中文处理用逐字拆（`store.py:66,82-84`），**[实测]** 查 `"归档"` 与查 `"归"` 返回同一批结果，
说明匹配粒度是**单字 OR**（`_match_query`，`store.py:87-94`）。这带来了根本性的相关度问题，见 §4.3。

### 2.3 排序：三个维度里只有一个真的在起作用

`memory/store.py:191-205`：

```python
FREQUENCY_WEIGHT = 0.6
RECENCY_WEIGHT = 0.4

def score(row):
    last = row["last_used_at"] or row["created_at"]
    age_days = max(0.0, (now - last) / 86400)
    recency = 1.0 / (1.0 + age_days)
    return row["use_count"] * FREQUENCY_WEIGHT + recency * RECENCY_WEIGHT
```

**[实测]**（本次复现）：

- `recency ∈ (0,1]`，乘 0.4 后**上限 0.4**；而 `use_count` 一项每用一次 +0.6。
  → **被用过一次的记忆，永远排在从未被用过的新记忆前面**；
- `_rank` 源码里**不出现** `confidence`、`bm25`、`kind`（**[实测]** 逐词检查），
  也就是说：`confidence` 列选了但没用于排序，教训的置信度**只影响推送、不影响检索排序**；
- `session.recall` 对**返回的每一条**调 `touch_memory`（`memory/session.py:54-57`），
  而 `touch` 就是 `use_count += 1`（`store.py:208-214`）。
  **[实测]** 连续 `recall("解析")` 四次，命中的两条 `use_count` 从 0/1 变成 **5/4**。
  → **"用过更多排更前" + "排前就记一次使用" = 自我强化的马太效应**：
  第一次偶然排前的那条会永久霸占榜位。这个循环里没有任何负反馈。
- FTS 的 `MATCH` 只给"命中/未命中"，没有用上 SQLite 现成的 `bm25()` 排序函数。

### 2.4 写入路径：只有一条，而且没有门禁

```
任务收尾 agent/loop.py:_archive (1274)
  → distiller (可选) = distill(gateway, state, final)  (runtime.py:652-658)
  → memory.archive(state, outcome, promote, final)     (memory/session.py:99)
     → add_episode(...)                                 (archive.py:62)
     → for excluded in state.excluded: add_memory(kind="failure")   (archive.py:71-78)
     → for entry in promote: sections[kind].append(text.strip())    (archive.py:96)
     → trim_to_budget(...) → write_hot(...)                        (archive.py:99-111)
```

- **没有 `remember` 工具。** `memory/tools.py` 的 `__all__` 只有 `recall_spec` 与 `search_history_spec`
  （`:224`），`assemble_loop` 也只注册这两个（`cli/runtime.py:457-478`）。
  也就是**模型没有任何显式写记忆的入口**，写什么完全由一次自动蒸馏决定。
- **没有任何写入门禁**：`archive.py:96` 直接 `append`，不去重、不查长度、不判类型是否合理；
  `distill._parse_entries`（`distill.py:119-150`）只校验 `kind ∈ KINDS` 与 `text.strip()` 非空。
- **自述冒充成功**：`_archive` 的 `outcome` 由主循环给，而 `finished=True` 的判据是
  `turn.done`（模型自己宣告完成，`loop.py:1036-1038`）或督导判断完成（`loop.py:695-704`），
  **与有没有通过验证无关**。这个 outcome 又被 `settle_lessons(..., succeeded=result.finished)`
  （`cli/commands/run.py:234`）拿去调教训置信度——**"这个教训有用"的判据是"模型说自己做完了"**。
- **敏感内容不过闸**：`redact_secrets` 全仓只有 4 处调用，全在**读取**路径
  （`transcript.py:152,189,443`），写入路径（`distill` → `archive` → `add_memory`/`record_lesson`/`write_hot`）
  **一处都没有**。`memory.md` 会每轮注入、`memory.db` 会落盘，一旦密钥被说出口就长期留着。

### 2.5 噪声是怎么进来的：一条兜底

这是 `memory.md:4,5,8` 那三条寒暄的直接来源。`memory/distill.py:259-263`：

```python
clipped_final = final.strip()
if not entries and clipped_final:
    knob_entry = limits.knob("episode_final_chars")
    max_chars = int(knob_entry.default) if knob_entry else 400
    entries = [("fact", _clip_final(clipped_final, max_chars), "")]
```

兜底本身是 T6 有意做的（`docs/roadmap.md:135-144`：让"任务结论"落进可检索记忆），
**但它把"整段交付答复"直接标成 `kind="fact"`，且只按 400 字符截断**。
配套证据：

- `episode #21`（`web-1790799477940`，10-01 04:18）summary 就是
  「你好：已完成 无记录 / 结论：你好！我是你的本地编程助手。…」；
- 同样的三条兜底 fact 分别来自 `episode #16`、`#17`、`#21` 三次不同会话的任务——
  **说明这不是一次意外，而是每次"寒暄型任务"都会复现的形状**；
- 蒸馏返回空条目时的兜底不区分 `final` 是"交付答复"还是"客套话"。

### 2.6 去重：只在一个很窄的范围内存在

`distill._merge`（`distill.py:153-168`）按 `text.strip()` 精确相等去重，
但它的调用点只有一处：**同一次 `distill` 调用内部因输出截断而分治时合并左右两半**
（`distill.py:214`）。跨任务、跨会话、跨 episode **没有任何去重**——
`archive.py:96` 的 `append` 不检查既有条目，所以 `memory.md:4/5/8` 那三条能同时存在。
（`archive.py:80` 是先 `read_hot` 再 `append` 的，既有内容就在手里，检查它不需要任何新依赖——
这是"没做"，不是"做不到"。）

### 2.7 容量与"代谢"：现状是 FIFO，与注释说的相反

`memory/hot.py:29-38` 的注释写着"事实最稳定、复用价值最高，所以最后才动它"，
`DEMOTE_ORDER` 也确实把 `fact` 放在最后（`hot.py:31-38`）。
但 `trim_to_budget` 在**同一个分节内部**是从**最前面**开始 pop 的（`hot.py:99-101`）：

```python
for kind in DEMOTE_ORDER:
    while working.get(kind) and counter.count(render_hot(working)) > budget:
        demoted.append(working[kind].pop(0))
```

而 `archive.py:96` 是**往尾部 append**。两者合起来 = **先进先出**：
**最新写进去的一定活，最早写进去的一定先被挤出去**。
"事实最后才动"这条只保护了"分节之间的顺序"，在事实节**内部**它恰恰是最先被删的。
后果很具体：今天这条噪声（寒暄）会把一个月前的构建命令挤掉。

容量本身：`hot_budget(8192) = int(8192 × 0.85 × 0.15) = 1044`（`hot.py:46-58`，
`context/budget.py:17,48`）。**[实测]** 当前 1011，占用 96.8%。

另外两处不一致：

- `MemorySession` 的计数器是 `OfflineTokenCounter`（`runtime.py:626`），
  而 `AgentLoop._budget` 用的是**真实分词器**（`runtime.py:482` 的 `counter_for(gateway)`，
  传给 `Assembler`）。`docs/benchmarking.md:499` 实测**估算比服务端高 25%**——
  即记忆的容量判定与装配的容量判定**建立在两个不同的尺子上**。
- 被下沉的内容写进 `memory` 表的 `kind='episode'`（`archive.py:103-109`），
  但它**不写 FTS**（走的是 `add_memory`，会写 FTS——这一条其实写进去了），
  而它的 `source` 是 `ep#{id}`、`confidence=0.4`，**与真正的情节表 `episode` 同名不同物**，
  分节名与表名撞车，检索里 `[episode]` 与 `[事件]` 是两回事（`memory/session.py:58-69`）。

### 2.8 读取进上下文：一条注入 + 两个按需工具

| 通道 | 入口 | 预算 |
|---|---|---|
| 热记忆全量注入 | `loop.py:544` → `Section(name="hot_memory", priority=20)`（`loop.py:451-452`） | `hot_memory_ratio=0.15`（`limits.py:136-139`） |
| 教训主动推送 | `loop.py:574-582`，`_render_lessons`（`:176-190`） | `lessons_ratio=0.10`（`limits.py:146`），`MAX_PUSHED=3`（`lessons.py:41`） |
| `recall` 工具（按需） | `memory/tools.py:44`，`limit` 默认 5 | 落在 `retrieval_ratio=0.15` 区段（`loop.py:467-470`） |
| `search_history` 工具（按需） | `memory/tools.py:151` | 同上；`history_search_limit=5`、`history_snippet_chars=240` |

装配层的裁剪是**区段级**的：`Assembler._fit` 只保证"这个区段的文本 ≤ 这个区段的配额"
（`context/assembler.py:90`），`hot_memory` 的区段文本又是 `trim_to_budget` 自己按
`hot_budget` 裁过的（`memory/session.py:44-46`）——**两处各自算一遍同一个预算**，
靠 `hot.py:40-43` 的注释要求"只有一个出处"来维持一致，没有断言或测试把它钉住。

**没有任何"为什么取了这条"的输出**：`recall` 只回 `[kind] text（来源 …）`（`session.py:58-61`），
不含分值、不含命中词、不含候选总数。调这一块时无法从输出看出是相关性还是频率把它顶了上来。

### 2.9 用户面：几乎不存在

- **CLI**：没有 `memory` 子命令（`cli/app.py` 的子命令清单：`run/plan/policy/revert/bench/session/limits/config/chat/mcp-servers/...`，
  其中只有 `spool session search` 走 `memory.db`，且只查 `transcript` 表，`cli/commands/session.py:112-208`）。
  想删一条错误事实，只能用文本编辑器改 `memory.md`——但它下次归档会被整份重写（`archive.py:111`），
  人工改动**没有任何痕迹**，也不会同步到 `memory` 表。
- **网页壳**：`WRITE_PATHS` 只有 `/run /run/stop /confirm /session/delete /session/restore /session/new`
  （`web/server.py:111-118`），没有任何记忆端点；`GET` 只有 `/` `/state` `/events`（`:776-781`）。
  网页只能显示 `transcript`（`web/runner.py:515-524`、`web/page.py:1686`）。
- **审计**：不存在。`source` 字段只有 `ep#<id>` / `未标注`（`archive.py:76,94,107`），
  没有"谁在什么时候、以什么理由写的"，也没有"这轮往上下文里注入了哪几条"的记录。

---

## 3. 现状缺陷清单

严重度：**S1** = 正在产生错误数据或安全问题；**S2** = 能力缺口；**S3** = 一致性/可维护性。

| # | 级别 | 缺陷 | 证据 | 真实症状 |
|---|---|---|---|---|
| 1 | S1 | 蒸馏空条目的兜底把整段助手答复写成 `kind="fact"`，无内容判据、无长度门禁之外的过滤 | `memory/distill.py:259-263`；`memory/archive.py:96` | **[实测]** `memory.md:4,5,8` 三条寒暄、`memory.md:9-12` 四段答复（含"任务已完成。请确认…"） |
| 2 | S1 | 跨任务无去重：`append` 前不比对既有条目 | `memory/archive.py:96`（`_merge` 只在 `distill.py:214` 单次调用内生效） | **[实测]** `memory.md` 里**三条逐字相同**的寒暄 |
| 3 | S1 | 热记忆同一分节内 FIFO 淘汰，与注释"事实最后才动"相反；且已占 96.8% | `memory/hot.py:99-101`、`archive.py:96` | **[实测]** 1011/1044 token；下一条新事实将挤掉最早的真实事实 |
| 4 | S1 | 教训写库不写 FTS，`recall` 永远查不到教训 | `memory/lessons.py:60-66`（对比 `store.py:127-130`） | FTS 只索引 `text`（`store.py:41`），`trigger`/`kind`/`source` 全不可检索 |
| 5 | S2 | 排序里"使用频次"压倒"新近度"（0.6·use_count vs ≤0.4·recency），且 `recall` 每次给命中项 +1 造成自我强化 | `memory/store.py:191-205`、`session.py:54-57` | **[实测]** 用过 1 次的旧条目永远压过 0 次的新条目；4 次 recall 把 use_count 冲到 4–5 |
| 6 | S2 | 没有相关度维度（不用 `bm25()`）、不用 `confidence`、不用 `kind`；中文查询退化成单字 OR | `store.py:181-188`、`195-205`、`87-94` | **[实测]** 查 `"归档"` 与 `"归"` 结果相同；排序源码中不出现 `confidence`/`bm25`/`kind` |
| 7 | S2 | 模型没有显式 `remember` 入口，写什么全由一次自动蒸馏决定 | `memory/tools.py:224`、`cli/runtime.py:457-478` | 用户说"记住：本项目一律用 uv"时，没有任何可靠路径把它写进去 |
| 8 | S1 | `outcome` 与教训置信度都基于"模型自称完成"，不是验证结果 | `loop.py:1036-1038`、`695-704`；`run.py:234` | 一次虚假完成会把错误教训的置信度推高（`lessons.py:44-49`） |
| 9 | S1 | 写入路径完全不过 `redact_secrets` | `redact_secrets` 仅 `transcript.py:152,189,443` 三处（读路径） | 密钥一旦被蒸馏进 `memory.md`，就每轮进上下文且长期落盘 |
| 10 | S3 | 容量判定用 `OfflineTokenCounter`，装配用真实分词器，两把尺子（估算偏高 25%） | `runtime.py:626` vs `runtime.py:482`；`docs/benchmarking.md:499` | 记忆的"是否超预算"与装配的"是否超预算"可能不同时成立 |
| 11 | S3 | 无用户面：无 CLI 记忆命令、无网页端点、无审计；手改 `memory.md` 会被下次归档整份重写且不回写库 | `cli/app.py` 子命令清单、`web/server.py:111-118`、`archive.py:111` | 用户无法查看/修改/删除一条错误记忆，也没有"谁写的"可查 |
| 12 | S3 | `hot.py:18-25` 的 `doing` 分节没有任何写入者；下沉条目用 `kind='episode'` 与真正的情节表同名不同物 | `distill.py:31`、`archive.py:82`、`archive.py:103-109`、`session.py:58-69` | 分节与表名撞车，检索输出里 `[episode]` 与 `[事件]` 是两种东西 |
| 13 | S3 | `episode` 表无任何索引、无容量上限、每次任务收尾必写 | `store.py:32-39`；**[实测]** episode 索引 = 空 | 真实工作区累计 26 条且只增不减；`search_episodes` 是 OR-LIKE 全表扫（`store.py:234-260`） |
| 14 | S3 | 写入触发点只有收尾一处，任务中途的有价值结论无法落库 | `archive.py:48`（只被 `loop.py:1274` 的收尾调用） | 长任务跑到一半被中断，中途的决策/教训全丢 |

---

## 4. 目标架构

### 4.1 一句话

**把现在"一个 Markdown 文件 + 一张混装表"改成分层、有来源、有门禁、有生命周期、可审计，
且 P1 基线完全建立在现有 SQLite/FTS5 之上的记忆子系统**（嵌入是**可选信号**，
见 §14；"零新依赖"在决议 1 后只描述 P1 基线与回退路径，不再是全设计的硬约束）——
热记忆退回"小而精的索引"，语义记忆成为真正可检索、可引用、可撤销的那一层。

### 4.2 分层：四层各是什么、存在哪、怎么命名

```
┌─ 工作记忆 working ───────────────────────────────────────────────┐
│  载体：TaskState + .agent/sessions/<sid>/tasks|progress（不变）    │
│  生命周期：一次任务。任务结束清空（现有 checkpoint 语义）           │
│  命名：不进 memory.md / memory.db                                │
├─ 情节 episode ──────────────────────────────────────────────────┤
│  载体：episode 表（一任务一行，含结果与验证状态）                  │
│  语义：episode:<id>                                              │
│  生命周期：只增；超量归档到 episode_archive 表                    │
├─ 语义 semantic ─────────────────────────────────────────────────┤
│  fact / preference / decision / lesson 四类，全在 semantic 表     │
│  语义：fact:<id> / pref:<id> / dec:<id> / lesson:<id>            │
│  生命周期：容量上限 + 衰减 + 巩固 + 软删除归档                     │
│  注：嵌入向量只存库（§14.6），热记忆不因它改变形态（决议 6）        │
├─ 技能 skill ────────────────────────────────────────────────────┤
│  载体：semantic 表 kind='skill'，字段 steps(JSON) + trigger       │
│  语义：skill:<id>                                                │
│  生命周期：与 lesson 同（按实际成功率调 confidence）               │
└──────────────────────────────────────────────────────────────────┘
        ↑ episode 通过 derived_from 引用 semantic，两者互不包含正文
```

**为什么这么定**：

- 四层的分界判据是**"它的真假会不会因为下一次任务而改变"**：
  `working` 每次任务都变；`episode` 是发生过的、永远为真；`semantic` 在被推翻前为真；
  `skill` 是"做法"而非"事实"，它的价值要用成功率来验。
  现在的问题正是这个判据**在代码里不存在**——`distill` 只让模型在四个字符串里选一个，
  没有任何结构性约束（`distill.py:31,119-150`）。
- **不这么定会怎样**：继续用"一个 `kind` 字符串"区分一切，
  那么"可以引用的东西"和"只是发生过的记录"就永远混在一张表里——
  上面第 4、12 条缺陷（教训查不到、`episode` 撞名）都是同一个根因的两次发作。
**技能层为什么这一期只做"形状"不做"执行"**：把它做成可执行的脚本/工具，
就要引入沙箱、权限、版本三个新问题，那是另一个量级的设计。
本期只让它可以被记录、检索、引用，让"我们到底有没有可复用做法"这件事**可被观测**。

### 4.3 数据模型（P1 基线全部用现有 SQLite；嵌入见 §14）

**原则一：不改 `memory` 表。** 它装着真数据，而 `docs/superpowers/plans/2026-09-24-workspace-session-kernel.md:1156`
已经立过这条规矩（"`memory.db` 表结构一个字不改"）。做法是**新增表 `semantic`，旧表保留为兼容视图来源**，
迁移期双写（§6）。

**原则二：FTS 用 external content + 触发器**，把同步责任从 4 个调用点收到数据库自己手里——
正好治第 4 条缺陷（`record_lesson` 漏写 FTS）。

> **关于"零新依赖"（决议 1 后已放宽）**：本节所有 DDL、索引、去重、审计、可解释性
> **都不需要嵌入**，所以这一段仍然是 P1 的**基线**，也是**任何嵌入后端不可用时的回退形态**。
> 但"零新依赖"不再是一条全设计的硬约束：嵌入依赖已被允许（§13.1），
> 具体的存储列（`embed_model` / `embed_dim` / `embed_vec`）与预算纪律见 §14.6 与 §14.4。

```sql
-- 1) 语义记忆（新表；承载 fact/preference/decision/lesson/skill）
CREATE TABLE IF NOT EXISTS semantic (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,          -- fact|preference|decision|lesson|skill
    scope         TEXT NOT NULL DEFAULT 'workspace',  -- session|workspace|machine
    scope_id      TEXT NOT NULL DEFAULT '',           -- session 级时为会话 id，否则 ''
    text          TEXT NOT NULL,
    trigger       TEXT NOT NULL DEFAULT '',  -- 逗号分隔关键词（lesson/skill 必填）
    steps         TEXT NOT NULL DEFAULT '',  -- JSON 数组，仅 skill
    source        TEXT NOT NULL DEFAULT '',  -- 出处：ep#12 / user / model / import
    evidence      TEXT NOT NULL DEFAULT '',  -- 可核对的依据：file:line / 命令 / 工具输出摘要
    confidence    REAL NOT NULL DEFAULT 0.5,
    importance    REAL NOT NULL DEFAULT 0.5, -- 写入时判定，参与排序
    content_hash  TEXT NOT NULL,           -- 去重指纹（规范化文本的 sha1 前 16 位）
    status        TEXT NOT NULL DEFAULT 'active',  -- active|archived|superseded
    superseded_by INTEGER REFERENCES semantic(id),
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    applied_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_semantic_kind    ON semantic(kind, status);
CREATE INDEX IF NOT EXISTS idx_semantic_scope   ON semantic(scope, scope_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_dedupe
    ON semantic(content_hash) WHERE status = 'active';  -- ② 结构性去重

-- 2) 全文索引：external content + 触发器，索引 text + trigger + evidence
CREATE VIRTUAL TABLE IF NOT EXISTS semantic_fts USING fts5(
    text, trigger, evidence,
    content='semantic', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS semantic_ai AFTER INSERT ON semantic BEGIN
    INSERT INTO semantic_fts(rowid, text, trigger, evidence)
    VALUES (new.id, new.text, new.trigger, new.evidence);
END;
CREATE TRIGGER IF NOT EXISTS semantic_ad AFTER DELETE ON semantic BEGIN
    INSERT INTO semantic_fts(semantic_fts, rowid, text, trigger, evidence)
    VALUES ('delete', old.id, old.text, old.trigger, old.evidence);
END;
CREATE TRIGGER IF NOT EXISTS semantic_au AFTER UPDATE ON semantic BEGIN
    INSERT INTO semantic_fts(semantic_fts, rowid, text, trigger, evidence)
    VALUES ('delete', old.id, old.text, old.trigger, old.evidence);
    INSERT INTO semantic_fts(rowid, text, trigger, evidence)
    VALUES (new.id, new.text, new.trigger, new.evidence);
END;

-- 3) 情节扩展（ALTER 加列，不改既有列；旧行默认值即"未验证"）
ALTER TABLE episode ADD COLUMN scope         TEXT NOT NULL DEFAULT 'session';
ALTER TABLE episode ADD COLUMN scope_id      TEXT NOT NULL DEFAULT '';
ALTER TABLE episode ADD COLUMN verified      INTEGER NOT NULL DEFAULT 0;  -- 0 未验证 1 通过 -1 失败
ALTER TABLE episode ADD COLUMN verify_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE episode ADD COLUMN archived      INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_episode_session ON episode(session_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_episode_created ON episode(created_at DESC);

-- 4) 情节 ↔ 语义 的引用（多对多，两条边各带方向）
CREATE TABLE IF NOT EXISTS memory_link (
    id         INTEGER PRIMARY KEY,
    src_kind   TEXT NOT NULL,   -- episode|semantic
    src_id     INTEGER NOT NULL,
    dst_kind   TEXT NOT NULL,
    dst_id     INTEGER NOT NULL,
    relation   TEXT NOT NULL,   -- derived_from|supersedes|duplicate_of|uses_skill
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_link_src ON memory_link(src_kind, src_id);
CREATE INDEX IF NOT EXISTS idx_link_dst ON memory_link(dst_kind, dst_id);

-- 5) 写入幂等账本
CREATE TABLE IF NOT EXISTS memory_write (
    id         INTEGER PRIMARY KEY,
    round_key  TEXT NOT NULL,   -- sha1(session_id|task_id|goal|final|step)
    source     TEXT NOT NULL,   -- distill|tool:remember|user|consolidate|migrate
    written    INTEGER NOT NULL DEFAULT 0,
    skipped    TEXT NOT NULL DEFAULT '',  -- duplicate|denied|redacted|conflict
    detail     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_write_round ON memory_write(round_key, source);

-- 6) 审计：谁、什么时候、改了什么
CREATE TABLE IF NOT EXISTS memory_audit (
    id         INTEGER PRIMARY KEY,
    at         REAL NOT NULL,
    actor      TEXT NOT NULL,   -- cli|web|agent|distill|consolidate|system
    action     TEXT NOT NULL,   -- add|edit|delete|archive|restore|supersede|demote
    target     TEXT NOT NULL,   -- semantic:12 / episode:8 / hot:fact:3
    before     TEXT NOT NULL DEFAULT '',
    after      TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT ''
);

-- 7) 读取可解释性：这一轮为什么取了这几条
CREATE TABLE IF NOT EXISTS memory_decision (
    id          INTEGER PRIMARY KEY,
    at          REAL NOT NULL,
    session_id  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    channel     TEXT NOT NULL,   -- hot|lesson_push|tool:recall|tool:search_history
    candidates  INTEGER NOT NULL,
    chosen      TEXT NOT NULL,   -- JSON：[{ref,score,parts,reason}]
    dropped     TEXT NOT NULL DEFAULT ''  -- JSON：[{ref,score,reason}]
);
```

**容量上限（写进 `limits.py` 的 `KNOBS`，全部 `CAPABILITY` 可覆盖）**：
`semantic_max_per_kind`（fact 200 / preference 50 / decision 100 / lesson 150 / skill 100，
按 kind 各一条）、`episode_archive_threshold=1000`、`hot_max_items=40`、
`hot_item_chars=200`、`remember_min_chars=8`、`remember_max_chars=400`。

**为什么这么定**：

- `content_hash` + 唯一部分索引把去重从"调用方记得检查"变成"数据库拒绝重复"——
  这才是第 2 条缺陷的根治，而且**不需要嵌入模型**：`content_hash` 用
  `sha1(去空白 + 去标点 + 全角转半角)`，`memory.md:4/5/8` 那三条逐字相同的会被同一个 hash 挡住。
- `evidence` 是有意加的**最难作假的一列**：门禁要求 `fact`/`decision` 必须能给出来处
  （§5.1），给不出来就降级为 `episode`。这一列也是"可解释性"的一半。
- `memory_decision` 是第 6 条缺陷（排序不可解释）的直接修正：
  排序变化的每一次都要留下当时的分值分解，否则"为什么这条被取了"永远只能靠猜。
- **不这么定会怎样**：把去重、审计、可解释性都做成"调用方约定"，
  那么每加一个写入路径（比如下一期的 `remember` 工具）就要重新记得一遍——
  现在 `record_lesson` 漏写 FTS 就是这种约定的必然下场。

### 4.4 目标架构图

```mermaid
flowchart TB
    subgraph 写["写入路径（三条，全部经过门禁）"]
        D["自动蒸馏 distill<br/>任务收尾"] --> G
        T["remember 工具<br/>任务中途（新）"] --> G
        U["用户显式操作<br/>CLI/网页（新）"] --> G
        C["巩固 consolidate<br/>批量作业（新）"] --> G
    end
    G{"门禁 Gate<br/>判据 · 去重 · 冲突 · 脱敏 · 幂等"}
    G -->|拒| R["memory_audit<br/>(denied + 理由)"]
    G -->|收| S[("semantic<br/>fact/preference/decision/lesson/skill")]
    G -->|收| E[("episode<br/>+verified")]
    S --- L[("memory_link<br/>derived_from / supersedes")]
    E --- L
    S --- F[("semantic_fts<br/>external content + trigger")]
    S -->|前 N 条| H["memory.md<br/>热索引（≤1044 token）"]
    subgraph 读["读取路径"]
        H -->|每轮注入 priority=20| CTX["上下文装配"]
        S -->|按需 recall 工具| CTX
        S -->|场景触发推送 ≤3 条| CTX
        H -.->|"decision 记录"| MD[("memory_decision")]
        S -.->|"decision 记录"| MD
    end
    subgraph 面["人可见面"]
        CLI["spool memory list/search/show/forget/..."] --> S
        WEB["网页壳记忆面板（P1 只读 → P2 可写）"] --> S
        CLI --> A[("memory_audit")]
        WEB --> A
    end
```

---

## 5. 写入路径

### 5.1 什么值得记（判据）

一条内容能进 `semantic`，必须**同时**满足五条。任一条不满足 → 进 `episode`（记下来，但不每轮注入）：

| # | 判据 | 怎么机械检查 | 反例（必须被挡住） |
|---|---|---|---|
| J1 | **跨任务可复用**：下次任务在别的目标下还会用到 | 与 goal 的字符重合度 < 阈值（默认 0.6）；或不含"这次/本次/刚才/上面的"这类一次性指代 | `memory.md:12`「任务已完成。请确认最终文件状态并宣告完成。」 |
| J2 | **可核对**：`evidence` 非空，且能指到工作区内的文件/命令/工具输出 | `evidence` 经路径校验落在工作区内，或匹配 `工具名\\(` / `命令前缀` 的形状 | 无出处的断言、模型的自我描述 |
| J3 | **非寒暄/非自述**：不是问候、能力自述、致谢、过程旁白、复述用户的话 | 规则表 + 与 goal 的**近似重复检测**（规范化后相等或一方是另一方的前缀） | `memory.md:4/5/8`「你好！我是你的本地编程助手…」 |
| J4 | **非一次性产物**：不是某次交付的正文（代码、表格、清单、长解释） | 长度 ≤ `remember_max_chars`(400) **且** 不含 3 行以上同构表格/代码块 **且** 不是以"运行方式/操作说明/功能："这类交付模板开头 | `memory.md:7` 12 城市天气表、`memory.md:9-11` 三段答复 |
| J5 | **有明确范围与触发**：`scope` 必须给（默认 `workspace`）；`lesson`/`skill` 必须给 `trigger` | `trigger` 非空且至少一个词能作为子串命中未来任务的 goal | 没有触发词的教训（现在会被写进去但永远推不出来） |

`skill` 额外要求：`steps` 是**可执行动作的序列**（写文件 / 跑命令），不是"要注意 X"这类叮嘱——
叮嘱属于 `lesson`。

**为什么判据要机械可查**：现在这一层唯一的判据是一句提示词里的
"只提取具备跨任务复用价值的条目，宁可少也不要凑数"（`templates.py:555`），
以及一个 JSON schema（`distill.py:37-54`）。**提示词约束对 7B 无效**——
这次实测的三条重复寒暄就是证据。
判据必须是代码，且**拒收的理由要能说出来**（写进 `memory_audit`），否则调这一块还是猜。

### 5.2 谁决定：自动蒸馏 vs 模型显式 `remember`

**两条都留，分工不同**：

| 路径 | 何时 | 判据 | 默认 |
|---|---|---|---|
| 自动蒸馏 | 任务收尾（现有触发点，`loop.py:1274`） | 同一套门禁（§5.1）；蒸馏的 JSON schema 增加**必填** `evidence` | 开 |
| `remember` 工具 | 任务**中途**，模型显式调用 | 同一套门禁 + 工具参数里必须给 `evidence` 与 `scope` | 开（P1） |
| 用户显式 | `spool memory add`（P1） | 跳过 J1/J3/J4（人说了算），但**必须**给 `evidence` 或显式 `--force` | 开 |

`remember` 工具的形状（P1）：

```json
{"name": "remember",
 "parameters": {"type": "object",
   "required": ["kind", "text", "evidence"],
   "properties": {
     "kind":  {"enum": ["fact", "preference", "decision", "lesson", "skill"]},
     "text":  {"type": "string"},
     "evidence": {"type": "string", "description": "出处：file:line / 命令 / 工具输出摘要"},
     "trigger": {"type": "string"},
     "scope": {"enum": ["session", "workspace", "machine"]}}}}
```

拒绝时**返回可读理由**（`ok=False` + 理由），与 `search_history` 的
`limit 最多 N 条（…）`（`tools.py:81-86`）同一口径——让小模型知道"为什么没记住"，
比静默丢弃或静默记住都好。

**为什么不只留自动蒸馏**：现在的中途结论没有任何落库路径（缺陷 14）。
长任务里真正值钱的"我选了这个方案，因为它避免了 X"发生在中途，而收尾时
蒸馏只看到 `state.done` 与一段 `final`（`distill.segments_of`，`distill.py:73-89`）——
它看不到中途的取舍。

**为什么不让 `remember` 取代自动蒸馏**：模型不一定记得调（本仓已有实测：`dispatch`
在一整批 50 题里调用 0 次，`docs/benchmarking.md:116-118`）。自动路径必须留着兜底。

### 5.3 门禁：五道

```
写入请求 ──▶ ① 脱敏 redact_secrets ──▶ ② 判据 J1..J5 ──▶ ③ 幂等 round_key
                                              │                    │
                                              ▼                    ▼
                                        拒：记 audit         新：查 content_hash
                                                                    │
                                              ④ 去重命中 ──▶ touch 既有条目 + audit(duplicate)
                                                                    │
                                              ⑤ 冲突检测 ──▶ 标 superseded + link(supersedes)
                                                                    │
                                                                  写入
```

**① 脱敏（必须做，且必须在写入侧）**：`redact_secrets` 现在只在读路径用
（`transcript.py:152,189,443`）。写入侧一律先过它，**并且**在
`archive_task` 与 `remember` 之间**只有一处**调用（不是每个调用点各写一遍）。
命中脱敏时 `memory_write.skipped='redacted'`，理由里写"命中了哪一类形状"（不回显值）。
理由：`memory.md` 每轮进上下文，`memory.db` 长期落盘——密钥一旦进去就是**长期慢性泄漏**，
而现有防线（Tavily/Gemini 密钥解析三处各自为政，`specs/2026-09-27-...:41`）都在入站侧。

**② 判据（§5.1）**：不通过则**降级为 `episode` 而非丢弃**——
"发生过"这件事本身有价值，只是不该每轮注入。降级要写 audit。

**③ 幂等**：`round_key = sha1(session_id|task_id|goal|final|step)`。
同一轮重跑（`--resume`、进程被杀后重收尾、蒸馏重试）不允许写两遍：
用 `memory_write` 的唯一索引挡。**唯一索引而不是先查后写**——
先查后写有竞态，而且"漏查"这类 bug 不会报错。

**④ 去重（语义重复怎么判）**：三级，从便宜到贵，前一级命中就不做下一级：

1. **精确**：`content_hash` 相同 → 直接 `touch` 既有条目（`use_count += 1`），不新增；
2. **近似**：规范化后（去空白标点、小写、全角转半角）编辑距离 / Jaccard 相似度
   ≥ `dedupe_similarity`（默认 0.9）→ 视为同一条：
   保留**较早的** `created_at` 与**较高的** `confidence`，`text` 取更完整的那一份，
   写 `memory_link(relation='duplicate_of')`，记 audit；
3. **冲突候选**：判定为"同主题但不可合并"（见 ⑤）。

第 2 级为什么用编辑距离而不是嵌入：**零新依赖**是硬约束（§9 开放决策 1）。
编辑距离在"短条目 + 中文"这个规模上够用，代价是**只能抓字面相近**——
同义改写（"用 uv 管理依赖" vs "本仓统一用 uv"）抓不到。
这条**要在文档与 `--explain` 输出里如实说明**，不能假装它等同语义去重：
第一级（hash）能挡住实测的那三条重复，第二级是增量。

**⑤ 冲突（新旧矛盾怎么处理）**：三条都保留，但内容上分主次——

- 新条目 `status='active'`，旧条目 `status='superseded'`、`superseded_by=新 id`，写
  `memory_link(relation='supersedes')`；
- **旧条目不删**：`spool memory show <旧>` 仍能看到"我们曾经这么认为"，
  这对调试很重要（本仓风格：`lessons.prune_lessons` 也是降级而非删除，
  `lessons.py:146-163`）；
- 冲突**判定规则**（必须可解释，不能只靠模型说"这矛盾"）：
  同一 `kind` + 同一"主题键"（`trigger` 与 `text` 的前 N 个字符规范化的交集非空）
  + 文本规范化后**不同** + 两者都非 `archived` → 判定为潜在冲突，
  由一次**独立上下文的判定调用**给出"矛盾/补充/无关"，并把判定原文写进
  `memory_link.relation` 的 detail 与 audit。判定失败（模型没给出可解析结果）时
  **默认保留两条 active 并标 `needs_review`**——静默丢一条比留着两条更危险。

**⑥ 敏感**：除 ① 的正则外，再加两条本仓已有的口径：
不索引 `.env` 的任何值（`.env` 只允许被读成键名清单，与 `spool env` 同一口径）；
`memory.db` 的 `evidence` 里出现工作区外的绝对路径时，只保留文件名与相对位置。

### 5.4 幂等与可回放

- 每条 `semantic` 有稳定的引用名 `kind:id`（`fact:12`、`lesson:7`），
  `memory_link` 用它做边——**引用不受排序、分页、重排影响**；
- 归档就是把 `status` 改成 `archived`，恢复是改回 `active`，**不做物理删除**
  （`spool memory forget` 也是软删除 + audit；真要物理删除需要 `--purge` 且写明后果）；
- 归档文件（§7.3）是**可回放的文本**（JSONL），回到库里就是重新跑一遍同一个写入门禁，
  因此"导出再导入"不会绕过判据。

---

## 6. 读取路径

### 6.1 排序：五个维度，权重不写死

```python
score = w_rel * rel + w_rec * rec + w_imp * imp + w_use * use - w_pen * penalty
```

| 维度 | 怎么算 | 为什么 |
|---|---|---|
| `rel` 相关度 | `1 - exp(-bm25)` 归一化（SQLite `bm25()`），**替换现在的"命中/未命中"** | 现在 FTS 只告知命中与否，单字 OR 之下"命中"几乎无信息量（**[实测]** 查 `"归"` 与查 `"归档"` 同结果） |
| `rec` 新近度 | `exp(-age_days / half_life_days)`，`half_life_days` 默认 30 | 现在是 `1/(1+age)`，衰减太慢且被 0.4 封顶 |
| `imp` 重要度 | 写入时判定 + 用户可改：`fact/decision` 基线 0.6、`preference` 0.7、`lesson/skill` 0.5；被 `supersedes` 过或通过验证的 +0.1 | **这一维现在完全不存在**；没有它就分不出"构建命令"和"某次天气" |
| `use` 使用频次 | `log1p(use_count) / log1p(max_use)` 归一化 | 现在 `use_count` 直接线性乘 0.6，一次使用就吃掉新近度的全部权重（第 5 条缺陷） |
| `penalty` 负反馈 | `- w_pen * (applied_count / (use_count+1))`，即"推给了但没被采纳"要扣分 | 现在**没有任何负反馈**，`recall` 的 touch 只加不减，形成马太效应（**[实测]** 4 次 recall → use_count 4–5） |

**权重放哪（不写死）**：全部登记进 `limits.KNOBS`，`CAPABILITY` 类型，可被
`.agent/limits.json` 按工作区覆盖——沿用既有机制（`limits.py:267-274`）：

```
rank_w_relevance  0.40   检索排序：相关度权重
rank_w_recency    0.20   检索排序：新近度权重
rank_w_importance 0.25   检索排序：重要度权重
rank_w_usage      0.10   检索排序：使用频次权重
rank_w_penalty    0.05   检索排序：负反馈权重
rank_recency_half_life_days  30   新近度半衰期（天）
dedupe_similarity  0.90   近似去重阈值（0.9 = 很接近才算重复）
```

**可解释性（必须一起做）**：`search_memories` 返回
`(Memory, parts: dict, score: float)`，`recall` 的输出改成：

```
- [fact:12] 构建命令是 pytest -q
  （分 0.71 = 相关 0.38 + 新近 0.12 + 重要 0.18 + 频次 0.03 − 反馈 0.00；候选 7 条，命中词「构建」「命令」）
```

`spool memory search --explain` 与 `memory_decision` 表写同一份分解。
理由：现在排序不合预期时**只能靠读代码猜**（这正是 §2.3 那三条缺陷没有被更早发现的原因）。

### 6.2 token 预算：必须给出最坏占用

| 通道 | 上限 | 依据 | 最坏占用（window=8192） |
|---|---|---|---|
| 热记忆注入 | `hot_memory_ratio=0.15` × 有效预算 | 现有 `limits.py:136-139`、`budget.py:48` | **1044 token / 每轮**（**[实测]** 当前 1011） |
| 教训推送 | `lessons_ratio=0.10`，且 `MAX_PUSHED=3` | 现有 `limits.py:146`、`lessons.py:41` | 696（区段配额）|
| `recall` 工具一次 | `recall_limit=5`、`recall_entry_chars=240` | 新增（对齐 `history_search_limit=5` / `history_snippet_chars=240`） | 5 × 240 字 ≈ **1200 token** |
| 单轮记忆总额 | `memory_budget_ratio=0.30` × 有效预算 | 新增 | **2089 token** |
| 每轮固定税（机器记忆） | 0（**默认**） | §9 决策 2 | 0 |
| **嵌入对上下文的影响** | **0**（**不新增任何通道**） | §14.4 规矩 4：嵌入只回答"这 K 条是哪几条"，不回答"给几条" | **0** |
| **重排候选池（不进上下文）** | `embed_candidates_n=50` / 路，`embed_rerank_k ≤ recall_limit` | §14.4 规矩 3；候选只在内存里过一遍，**不渲染进 prompt** | **0 token**；内存上界见 §14.4 规矩 4 |

**为什么给"单轮记忆总额"**：现在四个通道各有一个比例，但**没有总额**——
热记忆 15% + 教训 10% + 检索 15% 已经 40%，再叠上 `search_history`，
理论上一次请求可以有一半是记忆。加了总额封顶之后，"记忆最坏占多少"是一个
可以写进 SLO 的数字。超总额时的让位顺序：`recall` > 教训推送 > 热记忆
（热记忆是每轮都要的底账，最后让）。

**两处口径必须合一**：`MemorySession` 的计数器换成与 `AgentLoop` 同一个
`counter_for(gateway)`（`runtime.py:482`）——这是第 10 条缺陷。
**同时**保留 `OfflineTokenCounter` 作为无网关时的兜底（`build_loop` 那条离线路径，
`runtime.py:545-554`）。理由：`docs/benchmarking.md:499` 实测估算偏高 25%，
在两把尺子下"记忆超没超预算"和"装配超没超预算"会得出不同结论。

### 6.3 "为什么取了这条"（可解释性 / 可观测）

三处出口，同一份数据：

1. `memory_decision` 表：每轮每通道一条，含 `candidates` / `chosen`(带分值分解) / `dropped`(带淘汰理由)；
2. `--events` 模式下新增事件 `memory`（**只增字段，不改既有事件语义**，与心跳 `wait`
   同一做法，`specs/2026-09-21-model-wait-heartbeat-design.md:58-75`）：
   `{"type":"memory","channel":"hot","count":9,"tokens":1011,"budget":1044,"dropped":2}`；
3. `spool memory why --session <id>` / 网页壳记忆面板的"这一轮为什么注入这些"。

### 6.4 "默认不进上下文"与"提高准确率"怎么平衡

**保持既有口径不动**（这是本设计里我唯一明确建议"少做"的地方）：

- `transcript` 仍然**默认不进上下文**，靠 `search_history` 按需取（`transcript.py:11-17`、
  `rationale.md:139-146`）——短上下文是这个仓的支点，不能被记忆功能绕过去；
- 有界摘要（`history_digest_turns=12` / `history_digest_chars=400`）继续带，
  因为实测小模型不会主动检索（`limits.py:155-169` 的 note）。

**提高准确率靠"进入质量"而不是"进入数量"**，三件事：

1. **进热记忆的条目本身变干净**（门禁 §5.3）——现在 9 条里 0 条是真事实；
2. **推送的门槛从"关键词命中"改成"关键词命中 + confidence ≥ 0.45 + 排序分前 3 名"**
   （`match_lessons` 现在只有 `confidence >= 0.45` 与子串命中，`lessons.py:93-107`）；
3. **给按需检索加"提示"而非"内容"**：新任务开局只在**指代/承接**命中时提醒一句
   （现有 `deictic_reminder`，`transcript.py:204-224`），并在系统提示里明确
   `recall` 的适用场景——**注入的是"你可以去查"而不是"这是答案"**。

**不这么定会怎样**：把语义记忆全量注入（或按"相关就注入十段"）会立刻吃掉 8K 窗口的
三成以上，而实测本机模型在上下文接近满载时质量显著下降
（`budget.py:3-5` 的原话：「上下文接近满载时模型质量显著下降」）。
那等于用准确率换准确率，净亏。

### 6.5 反例/不做什么（明确列出，避免过度设计）

| 不做 | 理由 |
|---|---|
| **不把向量库 / 嵌入模型 / Rerank 作为默认** | 决议 1 已放开依赖（§13.1），但**"默认"这道线不动**：P1 默认是纯 FTS5（方案 F），嵌入是**装了才启用、只作 `w_embed` 一个信号、不可用即可见回退**的可选增强（§14.2、§14.4）。理由不只是"依赖重"，而是**实测嵌入会丢符号级精确匹配**（查 `uv` 排错，§14.1.3）——它不能替代关键词 |
| **不把 `transcript` 整库灌进 `recall`** | 这是 T6 明确否掉的 C 方案（`roadmap.md:144`、`plans/2026-09-17-monitor-and-memory.md:49`） |
| **不做"每轮自动全量注入语义记忆"** | 见 §6.4；预算是硬的 |
| **不做自动删除（物理 delete）** | 记忆是底账。默认只软删除/归档，`--purge` 需要显式确认 |
| **不做跨工作区的自动共享** | 与 `search_history` 的"跨工作区依旧不做"口径一致（`transcript.py:360-362`） |
| **不做多用户 / 鉴权 / 云同步** | 与批次二同一条线：本机、单人（`specs/2026-09-27-...:147`） |
| **不做"记忆自动改写用户偏好"** | 偏好只能由用户或用户显式确认的路径写（`spool memory add --kind preference`） |
| **不给 `memory.md` 加自动格式迁移** | 热记忆仍是人类可读 Markdown（`hot.py:6-9`）；新增元数据放库，不放 Markdown |
| **不做进度/计划的再实现** | `progress.md` / 计划文件已解决"我做到哪了"（`loop.py:1241-1257`、`benchmarking.md:293-300`），记忆不重复承担 |

---

## 7. 生命周期

### 7.1 容量与淘汰

| 对象 | 上限 | 超限动作 | 依据 |
|---|---|---|---|
| `memory.md` 热索引 | `hot_budget`（token）+ `hot_max_items=40` | 先按"分节内低重要度优先"淘汰，**不再先进先出**；被淘汰者进 `semantic`（若还没进去）并 `status='archived'` | 治缺陷 3 |
| `semantic` | 每 `kind` 一个上限（`semantic_max_per_kind`） | 按 `score` 从低到高 `archived`（**不删**），写 audit | 新增 |
| `episode` | `episode_archive_threshold=1000` | 超过则把最旧的（按 `created_at`）搬进 `episode_archive` 表，检索默认不含已归档 | 治缺陷 13 |
| 教训推送池 | `MAX_PUSHED=3`（现有） | 现有 `prune_lessons` 降级不删（`lessons.py:146`）保留 | 既有设计对 |

**热记忆淘汰顺序的新判据**（替换 `hot.py:99-101` 的 FIFO）：
`(重要性, confidence, created_at)` 升序——先淘汰"低重要度 + 低置信 + 最旧"的。
理由：注释想要的（`hot.py:29-30`：事实最后才动）与代码做的（FIFO）不一致，
新判据让"重要"这件事在**分节内部**也生效。**这不是把 FIFO 换个方向**：
同样是 fact，一条被 5 个任务引用过的构建命令必须比一条没被引用过的目录约定活得久。

### 7.2 衰减与巩固

**衰减**：不物理过期，只降低排序分（`rec` 维度，§6.1）。
`archived` 的触发条件是"分数长期垫底 + 超过容量"，而不是"时间到了"。
理由：一条三个月没被检索到的构建命令依然是对的，不该因为时间被清掉。

**巩固（把多次情节压成一条语义）**——两级：

1. **情节 → 语义**：同一 `trigger` 主题下累计 ≥ `consolidate_min_episodes`（默认 5）条
   `verified=1` 的 episode，触发一次巩固调用（独立上下文），产出**一条**语义条目，
   `source='consolidate'`，`memory_link(relation='derived_from')` 指向那 5 条 episode。
   **原始 episode 不动**（它是事实记录）。
2. **语义 → 语义**：同 `kind` + 高相似度（≥ `dedupe_similarity`）的 N 条合并成一条，
   保留最早的 `created_at`、最高的 `confidence`、以及全部 `evidence`（拼接，去重）；
   被合并者标 `superseded` + `duplicate_of`。

**触发时机**：巩固**不在任务收尾的同步路径上**（收尾已经有蒸馏一次调用，
再叠一次会让收尾变慢、且失败率翻倍）。做成显式动作：
`spool memory consolidate`（P1），以及 `spool run` 收尾时**只记一个待巩固计数**，
到阈值时在**下一次任务开局**之前跑（可选，默认关，由决策 4 定）。

**为什么不在收尾同步做**：`_archive` 现在刻意吞掉蒸馏异常
（`loop.py:1276-1280`：「归纳失败不能让用户拿不到结果」）。巩固是比蒸馏更贵的一步，
放同步路径上只会让"收尾偶尔卡一下"变成常态。

### 7.3 归档与可恢复

- `spool memory archive --kind fact --before 2026-06-01`：把符合条件的条目
  `status='archived'`，并**导出**一份 JSONL 到 `.agent/memory/archive/<日期>.jsonl`；
- 每一行是完整条目（含 `content_hash` / `confidence` / `evidence` / 全部引用边）；
- `spool memory restore <文件>`：回放该 JSONL，**逐行走同一个写入门禁**——
  所以回放不会重新引入已被拒的内容，也不会产生重复（`content_hash` 挡住）；
- **回滚**：`memory_audit` 有 `before`/`after` 两列，`spool memory audit --undo <id>`
  可以反向执行一条 `edit`/`delete`/`supersede`。
  这是"改错了/删错了能回去"的操作面。

### 7.4 "删掉/改错一条记忆"的操作面

| 动作 | CLI | 网页壳 | 语义 |
|---|---|---|---|
| 看 | `spool memory list [--kind] [--status]` | 面板列表（P1 只读） | 列出条目与分值 |
| 查 | `spool memory search <q> [--explain]` | 面板搜索 | 走 §6.1 同一份排序 |
| 看单条 | `spool memory show <ref>` | 点开条目详情 | 含来源、引用边、审计 |
| 改错 | `spool memory edit <ref> --text "…"` | P2 编辑框 | 重算 `content_hash`；与既有条目冲突则走 ⑤ |
| 删 | `spool memory forget <ref> [--purge]` | P2 删除按钮（需确认） | 默认软删除；`--purge` 才物理删 |
| 信任度 | `spool memory trust <ref> --confidence 0.9` | P2 | 人给的置信度**不被自动重算覆盖**（记 `applied_count=-1` 之类标记，见下） |
| 合并 | `spool memory merge <a> <b>` | — | 巩固的手动版 |
| 审计 | `spool memory audit [--ref]` | 面板审计页 | 谁、何时、改了什么 |

**人给的置信度必须免疫自动重算**——否则用户手工修正的条目会被下一轮
`record_outcome` 的 `recompute_confidence`（`lessons.py:44-49`）冲掉，
这正好是"记忆若不能被人工审视修正，错误会长期污染行为"（`hot.py:7-8`）的反面。

---

## 8. 范围与隔离

### 8.1 三级

| 级 | `scope` / `scope_id` | 默认可见 | 存在于哪 |
|---|---|---|---|
| 会话 | `scope='session'`, `scope_id=<会话 id>` | 只有这条会话 | `<工作区>/.agent/memory.db` |
| 工作区 | `scope='workspace'`, `scope_id=''` | 本工作区全部会话 | `<工作区>/.agent/memory.db` |
| 机器 | `scope='machine'`, `scope_id=''` | 本机**跨工作区** | `%APPDATA%\spoolkit\memory.db`（新，与用户级 `.env` 同目录，`specs/2026-09-27-...:68-69`） |

**默认值**：写入默认 `workspace`（现在**没有 scope 概念**，只有"按工作区分库存"这个物理事实——
`transcript.py:361`、`session.py:84-88`）。

### 8.2 跨级引用要不要显式授权

**要，而且口径与 `search_history` 完全对齐**：

- **工作区级**：默认可见（本工作区的所有会话都能检索到，等同现在 `search_history` 的
  `session="*"` 在同一工作区内的效果）——但**检索结果必须标注来源会话**，
  与 `render_history_hits` 的既有做法一致（`transcript.py:464-485`）；
- **跨工作区（机器级）**：**默认不可见**，必须显式
  （`spool memory search --scope machine` / 工具参数 `scope="machine"`）；
  读一次写一条 audit（"谁在什么时候读了别的机器的记忆"）；
- **写机器级**：必须显式 `--scope machine`，**且默认不自动写**——
  自动蒸馏永远只写工作区级。

**与既有 `search_history` 口径的对齐点**：`resolve_scope`（`transcript.py:352-372`）
已经确立了这条原则——"没给 = 当前（会话）；要跨必须显式；结果里标来源"。
记忆沿用同一个函数形状与同一套文案口径（`no_hits_text` 的"这不是没发生过"，
`transcript.py:488-514`），**不新造第二套默认值**。

**为什么机器级默认不可见**：工作区的存在意义就是"信任边界"
（`cli.md:46`：「工作区只能由人创建……不允许 agent 自行开辟信任边界」）。
A 工作区的记忆自动出现在 B 工作区，等于让一个临时任务顺手把信任边界挪了。

---

## 9. 迁移方案

### 9.1 原则

**旧数据一条不丢、旧行为可回滚、双写可观测、随时能停。**

### 9.2 旧 `memory.md` 怎么迁

`memory.md` 是**人类可能手改过**的文件，所以迁移**必须可解释、可拒绝**：

1. `spool memory migrate --dry-run`：解析 `memory.md`（现有 `parse_hot`，`hot.py:74-87`），
   逐条走 §5.1 门禁，**打印**：会被收进 `semantic` 的、会被降级为 `episode` 的、
   会被判重复的、以及**为什么**；
2. 真实工作区的预期（**[实测]** 9 条 fact）：3 条逐字重复的寒暄 → 全部被 ④ 去重 / ② 判据拒；
   4 段答复 → J4 不过 → 降级 `episode`（它们的正文已经在 `episode.summary` 里了，
   所以是"重定向"而不是"新写"）；天气表 → J4 不过 → 降级；会话回顾 → J1 不过 → 降级；
3. `spool memory migrate` 执行；原文件**改名为 `memory.md.bak-<日期>` 保留**，不删；
4. 新 `memory.md` 由热索引从 `semantic` 重新渲染（`render_hot` 的格式不变，
   分节标题不变，人类观感不破坏）。

### 9.3 旧库怎么迁

`memory` 表**不改结构**。迁移脚本 `spool memory migrate`：

1. `ALTER TABLE episode ADD COLUMN ...`（§4.3 第 3 块）——加列是向后兼容的，
   旧代码读它照常工作（多出来的列不影响 `SELECT *` 的既有字段名访问）；
2. 把 `memory` 的每一行按 `kind` 复制进 `semantic`：继承 `id` 之外的
   `confidence`/`use_count`/`applied_count`/`success_count`/`created_at`；
   `content_hash` 现算；`evidence` 为空 → 标 `needs_review`（**不静默丢弃**）；
3. **`memory` 表保留为只读视图来源**，`memory_fts` 保留不删——
   旧代码路径（如果还有）不会因为表被删而炸；
4. 迁移是**幂等**的：`content_hash` 唯一索引 + `memory_write(source='migrate', round_key=sha1(old_id))`
   保证跑两次不会写两份；
5. `PRAGMA user_version` 记录 schema 版本，`init_memory_schema` 里按版本逐步升级
   （现在是 `executescript` 一把梭，`store.py:105-108`——**纯 `IF NOT EXISTS` 无法给既有表加列**，
   所以必须有版本号）。

### 9.4 双写与回放

- **P0 期间双写**：新写入同时落 `semantic` 与 `memory`（旧表），
  `spool memory verify-migration` 比对两边条数与内容哈希，报告差异；
- **P1 起单写**：只写 `semantic`；`memory` 表冻结不再更新，但仍可读；
- **回滚**：`semantic` 是**追加的新表**，回滚 = 把代码回到旧版本 +
  在 `limits.json` 里关掉新路径（`memory_v2=0` 这种开关不必进代码：
  用"-1 表示退回旧路径"的能力标定值即可）。旧表与旧文件都在，**没有不可逆动作**。

### 9.5 工期与验收

| 期 | 内容 | 验收标准 | 风险 |
|---|---|---|---|
| **P0**（最小可用改进，不动 schema 之外的东西） | ① 兜底不再写 `kind='fact'`：蒸馏空条目时 final 只进 `episode.summary`（治缺陷 1、2 的直接来源）；② `archive` 落盘前做 hash 精确去重（治缺陷 2）；③ 写入路径过 `redact_secrets`（治缺陷 9）；④ `record_lesson` 补 FTS（治缺陷 4）；⑤ `hot_memory` 超预算时按重要性而非 FIFO 淘汰（治缺陷 3） | `memory.md` 不再出现寒暄/整段答复/重复条目；`reload` 后 `recall` 能查到教训；全套测试无回归；**在真实工作区上 dry-run 迁移报告与实际一致** | ①会减少蒸馏产出条数（这是想要的），但可能让"任务的真实结论"也不进热记忆——由 `episode.summary` 兜住，验收要看"结论还能不能查到" |
| **P1**（显式写入 + 人可见面） | ① `remember` 工具 + 门禁（§5.2/5.3）；② `semantic` 表 + FTS 触发器 + `memory_link`/`memory_write`/`memory_audit`；③ `spool memory list/search/show/forget/edit/audit`；④ 重排序（bm25 + 五维 + 负反馈）；⑤ 迁移命令与回放；⑥ `memory_decision` 与 `--events memory`；**⑦ 嵌入信号（§14.6 三列 + `w_embed` + 回退链，默认关）** | 50 题评测口径（§10）在"多轮可达 token 下降 ≥ 15% 且通过率不降"上成立；**新增 M2'（§14.5）：16 条固定语料上 precision@5 / recall@5 不低于纯 FTS5 基线**；`spool memory why` 能说出每条的成因；**任一"没用嵌入"的情形都能在 `memory_decision`/`--events`/`why` 三个出口看到**；迁移在真实 `memory.db` 上幂等（跑两次结果一致） | 排序换算法会让既有用户看到的 `recall` 结果顺序变化（需要在 dogfooding 记一条 + 保留 `rank_*` 权重可调回旧值）；**嵌入引入一条可失败的初始化路径**（缓解：`w_embed` 默认 0.0、不可用即回退且可见，见 §14.4 规矩 6）；网页壳 P1 只读（决议 3） |
| **P2**（生命周期与跨级） | ① 巩固（两级，**只在显式命令跑**，决议 4）；② 衰减与容量上限；③ 归档/恢复/撤销；④ 机器级 `scope` + `%APPDATA%\spoolkit\memory.db`（**默认不可见**，决议 2）；⑤ 网页壳可写面板（决议 3）；⑥ **按需引入 `sqlite-vec` 做 ANN 与向量列迁移（§14.6，仅当 `semantic` 规模需要时）** | 巩固能在真实 episode 上压出可读的一条语义且保留 `derived_from`；`forget` + `restore` 往返后条目与原条目逐字段一致（除 `status`）；跨工作区检索需要显式且留 audit | 机器级记忆是**新的信任边界**，默认关闭；巩固的模型调用可能产生错误合并——验收要求"合并前可预览、合并后可撤销" |

---

## 10. 评测：怎么证明它真的提高了成功率与 token 效率

### 10.1 口径（基于本仓已有的 50 题回归集）

`benchmarks/tasks/01..50`（**50 道**，`src/spoolkit/bench.py:45` 的 `DEFAULT_BENCH_ROOT`）。
现有两条仪器（`docs/benchmarking.md:187-205`）：**一题一会话**（A）与**整批一条会话**（B/together）。
记忆的价值只在**多轮**里能显现，所以口径围绕 B 与其改造版：

| 编号 | 仪器 | 命令 | 度量 |
|---|---|---|---|
| M0 | 基线·无记忆 | `bench --no-setup`（现状默认 `--no-memory`，见 `benchmarking.md:598`） | 通过数、调用数、输入 token、墙钟 |
| M1 | 长任务多轮·有记忆 | 同一批前 N 道铺进一个工作区，**同一条会话分两轮**：第 1 轮做前一半，第 2 轮做后一半（记忆在轮间保留） | 通过率、**第 2 轮输入 token** |
| M2 | 检索精度（离线、不起模型） | 用真实工作区的 `memory.md`/`memory.db` 做查询集 | `recall` 的 precision@5 / recall@5 |
| M3 | 噪声率（离线） | 对 `memory.md` + `semantic` 全量跑 §5.1 判据 | **噪声率** = 被 J1–J5 拒的条目 / 全部条目 |

**主判据（M1）**：

> **长任务多轮：第二轮起可用 token 下降 ≥ 15%，而通过率不降。**

"可用 token" = `bench` 报告里的**输入 token**（服务端计数，不是估算——
`benchmarking.md:499` 明确"只有服务端计数能当结论"）。
"不降"的噪声容忍：本仓实测同一配置重复跑会在 ±1 道上波动
（`benchmarking.md:32-37`、`337`），所以判据写成
**"通过数不低于基线 − 1 道"**，且**两次跑都要冷启动 llama-server**
（`benchmarking.md:36-37`；⚠️ 本机 8080 上现在有服务在跑（pid 43096），
评测时必须由使用者决定何时重启，设计里只写要求）。

### 10.2 为什么这个口径不比现状更差

**现状在 M1 上量不出任何东西**，因为现在这条路上根本没有可用的记忆：

1. `bench` 默认 `--no-memory`（`benchmarking.md:598` 的第六轮跑就是
   "冷启动服务、`--no-memory`"）——**基线本身就是"没有记忆"**，
   所以 M1 与 M0 的差就是记忆的全部贡献，**不存在"替代了某个更差的口径"**；
2. 即使打开记忆，`recall` 现在也查不到东西：**[实测]** 真实工作区
   `memory` 表 0 行、`memory_fts` 0 行；教训从不进 FTS（缺陷 4）。
   所以 M1 现在预期是 **Δ ≈ 0**，这正好是一个**可证伪的起点**——
   如果 P1 之后 Δ 仍然是 0，那就说明这一层没做出来，而不是"量不准"；
3. M1 比现有口径**更严格**而不是更松：现有基线只记"通过 + 平均输入 token"，
   两轮拆开之后，**只看第 2 轮的输入 token**——那是记忆唯一能影响的那一段。
   第 1 轮不参与判据，避免了"记忆让第一轮变慢"污染结论。

**M2/M3 是离线仪器**（不起模型、不烧 GPU、单机可跑），
它们是"记忆质量"的直接度量，不依赖 50 题的偶然波动。两者一起用的理由与
本仓既有的"两条仪器都要留着"完全一致（`benchmarking.md:203-205`）。

### 10.3 判据的边界（必须写明）

- M1 **不能**用来判"记忆对复杂长任务的价值"——50 题是单函数小题
  （`benchmarking.md:116-118`："回归集是小题（平均 2 步、单文件）"）。
  复杂长任务的记忆收益按现有文档的口径只有 `--together` 那条路能看到
  （`benchmarking.md:187-231`）；
- M1 的 15% 是**待验证的预期**，不是实测值。**本文档不声称已测到任何数字**
  （这是"不写没取证的数字"的直接落实）；
- M3 的噪声率依赖 §5.1 的判据规则表，而规则表本身会有假阳（把真事实拒掉）。
  所以 M3 必须**配合人工抽样**（每期随机抽 20 条判据决定，人工复核），
  只报机器判定是不够的。

---

## 11. 开放决策清单（每条：选项 / 代价 / 建议）

> **本节状态（2026-10-02 更新）**：这 8 条**已由产品负责人拍板**，决议原文与
> "决定 / 理由 / 影响到的章节"见 **§13**。下表**保留决策当时的选项与代价**（历史记录，
> 便于回看"为什么当时这么权衡"），最后一列已改为"决议"并标注 §13 对应小节。
> 选型层面的第 9 条（嵌入后端具体选哪个）不在本节，见 **§14**。

| # | 决策 | 选项 | 代价 | 决议（原"我的建议"列，已定案见 §13） |
|---|---|---|---|---|
| 1 | **要不要引入可选嵌入依赖**（嵌入 + 向量检索） | A 纯 FTS5 + hash/编辑距离（零依赖）/ B 可选 extra（`spoolkit[embed]`，默认不装，装了才启用语义近邻）/ C 默认就装 | A：同义改写抓不到，去重与检索都靠字面；B：需要下载模型权重、占内存/显存、断网首次不可用、多一个可失败的初始化路径；C：破坏"零新依赖 + 断网可用" | **【已定案：引入嵌入依赖，但选型与用法先量后定，见 §13.1 与 §14】** 原建议：**A 为默认，B 作为 P2 之后的独立选项**，且必须满足：装了才有、没装时行为逐字不变（现有 `.[socks]` 的可选依赖先例）；决策 1 之前不写任何嵌入代码 |
| 2 | **机器级（跨工作区）记忆默认可见吗** | A 默认不可见，显式 `--scope machine` / 决策 3 / B 默认可见 | A：跨工作区复用需要多一步；B：违背工作区=信任边界（`cli.md:46`） | **【已定案：A，见 §13.2】** B 会让"临时任务顺手改了信任边界"，与批次二的设计冲突 |
| 3 | **用户面给到 CLI 还是也上网页壳** | A 只 CLI（P1）/ B CLI + 网页壳只读（P1）/ C CLI + 网页壳可写（P1） | A：网页用户看不到记忆；B：多一个只读端点与面板（小）；C：可写要过 `WRITE_PATHS` 与非回环只读两条既有边界（`server.py:111-118`），且删除是不可逆动作 | **【已定案：B，见 §13.3】** 网页壳 P1 只读、P2 再开"改/删"（要确认框 + audit），这与批次二"远程只能看"的分层一致 |
| 4 | **巩固什么时候跑** | A 只在显式 `spool memory consolidate` / B 任务开局前自动跑（到阈值）/ C 任务收尾同步跑 | A：可能长期不跑；B：开局偶发变慢（模型调用）；C：收尾失败率与耗时翻倍，而收尾已经刻意吞异常（`loop.py:1276-1280`） | **【已定案：A 为默认，B 可选且默认关；C 不做，见 §13.4】** |
| 5 | **`outcome` / 教训置信度要不要接验证结果** | A 保持"自称完成" / B 接自动验证与 `verify_file` 的结果 / C 只统计"用户显式确认" | A：置信度建立在模型自述上（缺陷 8）；B：只有部分任务有自动验证，会引入样本偏差；C：样本量太小 | **【已定案：B 为优先信号、A 为兜底，且两种来源分开记，见 §13.5】** 在 `episode.verify_detail` 里写清这一轮 outcome 是哪种来源——**不要把两种来源混成一个数** |
| 6 | **热记忆要不要继续做"人类可读 Markdown"** | A 保持（格式不变，新增元数据只进库）/ B 改成库为主、Markdown 变成导出产物 | A：两处状态需要一致性检查；B：破坏"记忆可被人工审视修正"（`hot.py:6-9`），且用户手改会丢 | **【已定案：A，见 §13.6】** `memory.md` 必须是"仍可手改"的；P1 加一条 `spool memory doctor` 检查"文件与库不一致" |
| 7 | **旧 `memory` 表怎么处置** | A 保留只读、不删 / B 迁移后删表释放空间 / C 迁移后改名 | A：多一张表占空间（真实工作区 77KB，可忽略）；B：不可逆；C：破坏"表结构一个字不改"的老规矩 | **【已定案：A，见 §13.7】** 在 P2 之后才讨论删除 |
| 8 | **`episode` 容量** | A 1000 条上限 + 归档表 / B 不限 / C 按时间（90 天） | A：需要归档表与检索过滤；B：真实工作区 26 条/18 天，一年后数千条，OR-LIKE 全表扫会变慢（`store.py:234-260`）；C：三个月前的教训仍然有用 | **【已定案：A，见 §13.8】** 阈值进 `limits.json` 可调 |

---

## 12. 我不建议做的方向（反例）

1. **不建议把记忆做成"每轮全量注入的知识库"**。8K 窗口下这是把上下文让给历史，
   直接推翻本仓的支点（状态外置 + 精确取用，`roadmap.md:54-58`）。
2. **不建议为了"更像商业产品"先上向量库**。收益**尚未测量**——本仓的规矩是
   "未测量前无法判断其价值"（`specs/2026-09-11-...:670` 对向量检索库的原文）。
   **（决议 1 已放开"依赖"这道门，但这条纪律不变：先量再上，量的结果与纪律见 §14。）**
3. **不建议做自动物理删除 / 自动遗忘**。记忆是底账，误删不可逆；
   现有 `lessons.prune_lessons` 都只降级不删（`lessons.py:146-163`），这条口径要守住。
4. **不建议把 `transcript` 与语义记忆合并成一张表**。
   两者的读路径、保留策略、隐私边界都不同（一个是原文，一个是蒸馏结论），
   合并只会让 `search_history` 的作用域规则（默认只查当前会话）失去落点。
5. **不建议在记忆里做"用户画像/偏好推断"**。偏好只能被显式写入；
   自动推断出的偏好是"未经声明的越界"（`rationale.md:144-146` 的同一理由）。
6. **不建议这期做"技能自动执行"**（把 `skill` 变成可运行脚本）。
   它要引入沙箱、权限、版本三套新机制，收益未测量，风险等级远高于本期目标。
7. **不建议为记忆单独做一个服务或后台常驻进程**。
   单机单人场景下，`memory.db` 就在工作区里，进程内读写足够；
   多一个常驻进程会引入"库被两个进程写"这类新问题
   （而本仓已有"同一个工作区同时只跑一轮"的前提，`rationale.md:126`）。
8. **不建议现在就给记忆做 SLO/监控大盘**。P0/P1 阶段真正需要的是
   `memory_decision` + `--events memory` 这两条可观测出口（便宜、离线可看），
   大盘等 M1/M2/M3 口径稳定之后再说。

---

## 13. 决策记录（用户已拍板）

§11 的 8 条开放决策**已由产品负责人定案**，不再是"建议"。本节是决议原文，
**§11 那张表从此只作为历史与代价对照阅读**；本文档其余章节凡与本节冲突处，
以本节为准（冲突点已在 §13.9 逐条点名，并做了最小改动）。
选型层面的第 9 条决议（嵌入后端具体选哪个）不在本节，见 §14。

### 13.1 决策 1：引入嵌入依赖，但"选哪家、怎么用"必须先量后定

| 项 | 内容 |
|---|---|
| **决定** | **引入嵌入依赖**。原先"零新依赖"的硬约束**作废**。但依赖的**选择与使用方式**要求更慎重：优先小体积、无 torch、CPU 友好、断网可用、**不替我们管理上下文**的方案；具体后端见 §14 |
| **理由** | 用户原话：「引入嵌入依赖，我没说过一定要零依赖，只不过市面上的部分依赖往往过重，缺乏对小上下文的管理支持，会导致和我们的初衷有冲突，因此在这方面的选择和使用需要更加慎重。」→ 否决的不是"依赖"，是"**过重 + 会绕过我们的小上下文纪律**"的依赖 |
| **影响章节** | §4.3 标题与"零新依赖"措辞（已加注）、§4.1 结句、§6.5 反例表第 1 行（已改为"不作**默认**"）、§11 决策 1（已标定案）、**新增 §14**、§9.5 分期（嵌入落点见 §14.6） |

### 13.2 决策 2：机器级记忆默认不可见

| 项 | 内容 |
|---|---|
| **决定** | `scope='machine'` **默认不可见**。必须显式 `--scope machine` / 工具参数 `scope="machine"` 才可读；**写机器级必须显式且默认不自动写**（自动蒸馏永远只写工作区级）；每读一次写一条 audit |
| **理由** | 工作区就是信任边界（`cli.md:46`：「不允许 agent 自行开辟信任边界」）。默认可见等于让一个临时任务顺手把边界挪了 |
| **影响章节** | §8.1 默认可见列、§8.2、§6.2 表"每轮固定税（机器记忆）= 0"、§9.5 P2 |

### 13.3 决策 3：用户面 P1 只读，P2 才可写

| 项 | 内容 |
|---|---|
| **决定** | CLI 全部动作 P1 可用；**网页壳 P1 只读**（列表/搜索/详情/审计），**改与删放 P2**，且必须带确认框 + audit |
| **理由** | 与批次二"远程只能看"的分层一致；可写要同时过 `WRITE_PATHS` 与非回环只读两条既有边界（`server.py:111-118`），而删除是不可逆动作 |
| **影响章节** | §4.4 架构图"人可见面"（已标注 P1 只读 → P2 可写）、§7.4 表"网页壳"列、§9.5 P1/P2 风险列 |

### 13.4 决策 4：巩固只在显式命令跑

| 项 | 内容 |
|---|---|
| **决定** | 巩固**只在 `spool memory consolidate` 显式执行**。任务开局前自动跑作为**可选开关且默认关**；**任务收尾同步跑不做** |
| **理由** | `_archive` 已刻意吞掉蒸馏异常（`loop.py:1276-1280`：「归纳失败不能让用户拿不到结果」）。巩固比蒸馏更贵，放同步路径会把"收尾偶尔卡一下"变成常态 |
| **影响章节** | §7.2 "触发时机"段、§9.5 P1/P2 |

### 13.5 决策 5：outcome 优先接自动验证，且来源分开记

| 项 | 内容 |
|---|---|
| **决定** | **B 为优先信号、A 为兜底**：`outcome` 与教训置信度的首选依据是**自动验证与 `verify_file` 的结果**，模型自称完成只作兜底；**两种来源分开记**——`episode.verify_detail` 必须写清这一轮 outcome 是哪种来源，**不把两者混成一个数** |
| **理由** | 现状"这个教训有用"的判据是"模型说自己做完了"（缺陷 8）；但只有部分任务有自动验证，混成一个数会把"验证过"和"自称过"抹平，样本偏差与虚高会同时发生 |
| **影响章节** | §2.4 outcome 段、§5.3 ⑤、§7.4 "信任度"、§9.5 P1、§10.1 M1 口径说明 |

### 13.6 决策 6：热记忆保持人类可读 Markdown

| 项 | 内容 |
|---|---|
| **决定** | `memory.md` **继续是"仍可手改"的人类可读 Markdown**（分节标题与格式不变）；**新增元数据只进库，不进 Markdown**。P1 增加 `spool memory doctor` 检查"文件与库不一致" |
| **理由** | 破坏"记忆可被人工审视修正"（`hot.py:6-9`）会让用户手改直接丢失；两处状态需要一致性检查，但这是**可控的已知代价** |
| **影响章节** | §4.2 热记忆行、§7.1、§9.2 迁移第 4 步、§6.5 反例表"不给 `memory.md` 加自动格式迁移" |

### 13.7 决策 7：旧 `memory` 表保留只读

| 项 | 内容 |
|---|---|
| **决定** | 迁移后 **旧 `memory` 表保留为只读、不删不改名**；`memory_fts` 一并保留。删除推迟到 P2 之后再讨论 |
| **理由** | 沿用 `plans/2026-09-24-workspace-session-kernel.md:1156`「`memory.db` 表结构一个字不改」的老规矩；真实工作区整库 77KB，保留的代价可忽略，而删除不可逆 |
| **影响章节** | §4.3 原则一、§9.3 第 3 步、§9.4 "P1 起单写"、§4.1 |

### 13.8 决策 8：`episode` 1000 条上限 + 归档表

| 项 | 内容 |
|---|---|
| **决定** | `episode` 上限 **1000 条**，超出把最旧者搬进 `episode_archive` 表，检索默认不含已归档；阈值 `episode_archive_threshold` 进 `limits.KNOBS` / `.agent/limits.json` 可覆盖 |
| **理由** | 不限量的话 `search_episodes` 的 OR-LIKE 全表扫（`store.py:234-260`）会随真实工作区（**[实测]** 26 条 / 18 天）线性变慢；按时间（90 天）淘汰则会把仍然有用的教训清掉 |
| **影响章节** | §4.3 容量上限段、§7.1 表、§9.5 P2 |

### 13.9 为落实上述决议而做的**最小改动**（编号一律不动）

本节只列"因为拍板而必须改字"的地方；**章节号、表号、行序全部保持不变**：

1. **§4.3 标题**：`数据模型（全部用现有 SQLite，零新依赖）` → `数据模型（P1 基线全部用现有 SQLite；嵌入见 §14）`，并在原则二后加一行说明"零新依赖"只描述 **P1 基线**，不再是全设计的硬约束。
2. **§4.1 结句**：把"完全建立在现有 SQLite/FTS5 与零新依赖之上"改成"P1 基线建立在现有 SQLite/FTS5 之上；嵌入是**可选信号**，见 §14"。
3. **§4.2 四层框图标题行**：`语义 semantic` 那一格里"嵌入"不是载体，未改动；**热记忆行补注**"仍是人类可读 Markdown（决议 6）"。
4. **§6.5 反例表第 1 行**：由「**不引入向量库 / 嵌入模型 / Rerank 作为默认**」改为「**不把向量库 / 嵌入模型 / Rerank 作为默认**（已按 §14 决议：嵌入可作**可选信号**，但**不作默认**、不作唯一检索路径）」；理由列同步改写。
5. **§6.2 token 预算表**：末尾追加两行（`embed_candidates` 与 `embed_rerank_k` 的**内存/时间**上界说明），见 §14.4；**不改任何既有行的数字**。
6. **§11 决策 1、2、3、4、5、6、7、8**：在"我的建议"列前加 `【已定案，见 §13.x】` 前缀，**表格结构与选项/代价列一字不改**（保留决策当时的权衡记录）。
7. **§12 反例第 2 条**：补一句"（决议 1 已放开依赖，但'收益未测量前不落地'这条纪律不变，见 §14）"。

---

## 14. 嵌入后端选型与上下文预算纪律

本节是本次的主体。**所有数字都是本轮实测**（复现方式见附录 A.5–A.7）；
凡是没测到的，在 §14.7 与附录 B 如实列出，**不写估数**。

### 14.1 第一步：实测（先量再说）

#### 14.1.1 已有的本机资源：8080 上的 llama-server —— **此路不通**

只读探针（**没有重启、没有停止、只打了 2 个短请求**，超时 20s）：

```
POST http://127.0.0.1:8080/v1/embeddings
body: {"input": "归档", "model": "x"}

→ HTTP 501，wall 0.032 s
{"error":{"code":501,"message":"This server does not support embeddings. Start it with `--embeddings`","type":"not_supported_error"}}
```

用真实模型名 `D:\llm\llama\models\Qwen3.8-27B-UD-IQ2_S.gguf` 再打一次，**同一错误原文**，
`wall 0.001 s`（说明连模型都没加载，是**服务端启动参数缺 `--embeddings`**，不是模型问题）。

| 记录项 | 结果 |
|---|---|
| 能不能用 | **不能**。`/v1/embeddings` 返回 **501 not_supported_error** |
| 维度 | **未取得**（没有成功响应）。旁证：`/v1/models` 的 `meta.n_embd = 5120`，但那是**该 27B 生成模型的隐层宽度，不是嵌入维度**，不能当结果用 |
| 单条延迟 | **不适用**（错误路径 0.032s / 0.001s，不含任何推理） |
| 错误原文 | `This server does not support embeddings. Start it with \`--embeddings\`` |

**为什么这条路即使能修也不该选为方案 A**（两条独立理由，都不依赖上面那个 501）：

1. `/v1/models` 的 `capabilities` 只有 `["completion"]`，且该模型是**decoder-only 的 27B 因果语言模型（IQ2_S，2.5 bpw）**。
   用生成式 LLM 的隐层做句向量偏离其训练目标，质量无保证；即使加 `--embeddings` 也只是"能返回一个向量"；
2. 本机服务**已经在被使用**（本任务被明确要求不得重启），把它变成记忆子系统的**硬依赖**，
   等于让"记忆能不能搜"取决于"那个 27B 有没有在跑"。这与 §12 第 7 条（不给记忆单独做常驻服务）同一条纪律。

> **结论：方案 A（复用 llama-server `/v1/embeddings`）实测不可用，且方向上也不该依赖它。**

#### 14.1.2 本机已装了什么（决定"边际成本"）

用仓库 venv 的 python（`D:\workSpace\agents_dev\.venv\Scripts\python.exe`，**Python 3.14.3**）
逐个 `find_spec` 探测：

| 模块 | 状态 |
|---|---|
| `numpy` | **MISSING** |
| `onnxruntime` | **MISSING** |
| `fastembed` | **MISSING** |
| `sentence_transformers` | **MISSING** |
| `torch` | **MISSING** |
| `model2vec` | **MISSING** |
| `sqlite_vec` | **MISSING** |
| `transformers` / `tokenizers` / `huggingface_hub` / `onnx` | **全部 MISSING** |

**[实测]** 该 venv 的 `site-packages` **总计 12.4 MB**，明细只有
`pygments 4.7 / _pytest 3.1 / anyio 1.1 / httpx 0.7 / httpcore 0.6 / packaging 0.5 / idna 0.4 / h11 0.2 / certifi 0.2`
（+ 若干 0MB 的 dist-info）。**该 venv 甚至没有 `pip`。**

> **这是本轮最重要的一条成本事实：`numpy` 都不在。**
> 所以"边际成本"不是"多装一个小模型"，而是**从零引入整条数值/推理链路**。
> 任何"某方案零新增依赖"的说法在本机**当前状态下都不成立**（除了纯 FTS5 那一条）。

**但有一个反向的重要事实**：HF 缓存里**已经有可离线使用的多语言嵌入模型**：

| 缓存项 | 事实 |
|---|---|
| `C:\Users\32615\.cache\huggingface\hub\models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2` | **真实存在**，`blobs` 里的 `model.safetensors` **448.8 MB** + tokenizer **8.7 MB**（snapshot 是符号链接，**不是 0 字节占位**；只有 47 个非权重文件是 0 字节） |
| 架构 | `BertModel`，`hidden_size=384`，`num_hidden_layers=12`，`vocab_size=250037`，**117.7 M 参数**（实测） |
| 断网可用 | **是**。设 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` 后成功加载，**未发生任何下载** |
| 本机网络 | **出口不通**：`pypi.org` / `huggingface.co` / 镜像站全部 `status=000`（`schannel: SEC_E_NO_CREDENTIALS`）；系统配了 `ProxyServer=127.0.0.1:7890`（`ProxyEnable=1`）但**该端口无监听**。→ 本轮**无法安装任何新包**，也无法用网络核对全部候选的体积<br>**[2026-10-02 二轮实测更正：此行结论已过期]** `7890` **在听**，`pypi.org` 与 `hf-mirror.com` 均 **HTTP 200**（`huggingface.co` 仍被重置）；**装得上包**（本轮就这么装的）。`schannel` 报错是**本机 TLS 栈问题，不是网络问题**。详见 §14.3.5 第 3 条与附录 B.7 |

**新增体积量级（能查到的按事实写，查不到的写"不确定"）**：

| 项 | 量级 | 来源/性质 |
|---|---|---|
| `numpy` (win_amd64 wheel) | 约 **十几 MB** 量级 | **不确定**：未取得精确 wheel 大小（网络不通），只知本机另一环境解包后 **21.3 MB** |
| `onnxruntime` **1.30.0** `cp314-win_amd64` wheel | **14 675 319 B ≈ 14.0 MB**（解包后更大；本机另一环境 1.23.2 解包 **37.3 MB**） | **[实测取自 PyPI API]**，`requires_python>=3.11`，依赖 `flatbuffers / numpy>=1.21.6 / packaging / protobuf>=4.25.8` |
| `sqlite-vec` | wheel 约 **0.16 MB**（`0.0.1a37-py3-none-win_amd64.whl` = 166 273 B）；最新版 `0.1.9` 的 wheel 大小**未取到** | 部分 **[实测]**（取自 PyPI API 的历史 release）；`py3-none-win_amd64` **纯二进制 tag，不带 cp 前缀 → 不受 Python 版本限制** |
| `model2vec` | wheel **59 865 B ≈ 0.06 MB**；声明的"主要依赖只有 numpy"，基础包**不拖 torch**（`torch` 仅在 `[distill]/[onnx]/[train]` extras 里）；自带模型 ~8 MB / ~30 MB | **[实测取自 PyPI API]**（0.9.0，`requires_python>=3.10`） |
| `fastembed` | 包体本身小，但**必然拖 `onnxruntime` + `tokenizers` + `huggingface_hub`**，且**默认模型会在首次使用时下载** | **不确定**：未取到 wheel 精确大小与默认模型体积（网络不通），**不作为已核事实** |
| `sentence-transformers` / `torch` | **本机另一环境的实测解包体积：`torch` = 5 528.6 MB**（其中 `torch\lib` 5 439.8 MB，含 `torch_cuda.dll` 1 250.8 MB、`cudnn_engines_precompiled64_9.dll` 504.3 MB）；`transformers` 53.6 MB、`tokenizers` 7.3 MB、`safetensors` 0.7 MB、`huggingface_hub` 3.0 MB、`sympy` 33.6 MB | **[实测]**（`D:\workSpace\pyvideotrans\.venv`）。注意那是 **cu128 构建**；CPU-only 构建会小得多，**具体小多少本轮没测** |

#### 14.1.3 一次小规模对照实验（真跑）

**试验设计**：16 条中文短语，人工分 4 组语义（G1 归档/存档、G2 依赖管理、G3 上下文预算、G4 检索/索引），
10 条查询。两条检索路径吃**同一批文本**：

- ① **纯 FTS5 关键词**（**逐字复刻** `store.py:66,82-94` 的 `normalize_for_fts` + `_match_query`，
  建临时库、`bm25()` 排序）；
- ② **嵌入**：`paraphrase-multilingual-MiniLM-L12-v2`，**离线加载本机缓存**，
  均值池化 + L2 归一化，余弦相似度 top5。

样本集（编号即结果里的 `[n]`）：

| # | 组 | 文本 |
|---|---|---|
| 1 | G1 | 归档旧的任务记录 |
| 2 | G1 | 把过期条目存档 |
| 3 | G1 | 清理历史数据并备份 |
| 4 | G1 | 删除三个月前的日志 |
| 5 | G2 | 本项目统一用 uv 管理依赖 |
| 6 | G2 | 不要往主依赖里加包 |
| 7 | G2 | requirements 要锁版本 |
| 8 | G2 | 安装体积要可控 |
| 9 | G3 | 上下文预算必须给出最坏占用 |
| 10 | G3 | token 超了就按优先级让位 |
| 11 | G3 | 小窗口下不要全量注入 |
| 12 | G3 | 截断前先做摘要 |
| 13 | G4 | 全文检索用 FTS5 |
| 14 | G4 | 给检索结果打分排序 |
| 15 | G4 | 索引要和主表同步 |
| 16 | G4 | 关键词命中不等于相关 |

**嵌入侧成本实测**：

| 指标 | 值 |
|---|---|
| 模型加载（离线） | **0.84 s**（另一次 1.10 s） |
| 16 条语料一次批量编码 | **165 ms**（≈ 10.3 ms/条，含首次图构建） |
| 批 32 条稳态 | **72 ms**（≈ **2.3 ms/条**） |
| 单条查询（30 次平均） | **14.2 ms/条** |
| 向量维度 | **384** |
| 向量存储量 | 200 条 = 0.29 MB / 1000 条 = 1.46 MB / 5000 条 = 7.32 MB（float32） |
| 每轮上下文增加 | **0**（嵌入只影响排序，见 §14.4） |

**① 现有 FTS5 口径的真实命中**（`bm25` 列已取负号，越大越相关）：

| 查询 | `_match_query` 展开 | 命中（按 bm25 降序） |
|---|---|---|
| `归档` | `归 OR 档` | [1] 4.2183、[2] 1.9047 |
| `归` | `"归"` | [1] 2.4067 |
| `存档` | `存 OR 档` | [2] 4.4351、[1] 1.8116 |
| `备份` | `备 OR 份` | [3] 4.5891 |
| `依赖管理` | `依 OR 赖 OR 管 OR 理` | [5] 6.8386、[6] 3.4543、[3] 1.7271 |
| `uv` | `"uv"` | [5] 2.0989 |
| `上下文预算` | `上 OR 下 OR 文 OR 预 OR 算` | [9] 8.7137、[13] 2.0078、[11] 1.6502 |
| `让位` | `让 OR 位` | [10] 4.3848 |
| `全文检索` | `全 OR 文 OR 检 OR 索` | [13] 7.5654、[14] 3.0535、[11] 1.6502、[9] 1.4558、[15] 1.3912 |
| `排序` | `排 OR 序` | [14] 4.5891 |

**② 嵌入的真实命中（余弦 top5）**：

| 查询 | top5（编号 + cos） |
|---|---|
| `归档` | [2] 0.7393（**把过期条目存档**）、[1] 0.6330、[12] 0.5835、[3] 0.5185、[13] 0.4585 |
| `归` | [2] 0.6017、[1] 0.5006、[12] 0.4419、[10] 0.4120、[3] 0.3965 |
| `存档` | [2] 0.7651、[12] 0.5709、[1] 0.5654、[10] 0.5268、[13] 0.4827 |
| `备份` | [2] 0.7059、[1] 0.5742、[3] 0.5630、[12] 0.5422、[10] 0.4641 |
| `依赖管理` | [5] 0.7201、[8] 0.4985、[2] 0.4580、[10] 0.3740、[6] 0.3151 |
| `uv` | [2] 0.5214、[12] 0.4234、[10] 0.3988、[1] 0.3763、[11] 0.2925 ← **失手** |
| `上下文预算` | [9] 0.5722、[5] 0.4265、[2] 0.4223、[8] 0.3241、[10] 0.3153 |
| `让位` | [2] 0.6472、[10] 0.5364、[12] 0.5307、[11] 0.3703、[6] 0.3434 |
| `全文检索` | [12] 0.6948、[13] 0.6539、[2] 0.6482、[14] 0.5697、[1] 0.5069 |
| `排序` | [14] 0.7385、[12] 0.5312、[2] 0.4755、[15] 0.4364、[1] 0.4352 |

**关键案例：查"归档"与查"归"**——**这条必须如实改写，比原文档说的更微妙**：

| 口径 | 查 `归档` | 查 `归` | 两者相同？ |
|---|---|---|---|
| 现有 FTS5 | `[1, 2]` | `[1]` | **否** |
| 嵌入 top5 | `[2, 1, 12, 3, 13]` | `[2, 1, 12, 10, 3]` | **否** |

- **[修正先前论断]** §2.2 与缺陷 6 写的"查 `归档` 与查 `归` 返回同一批结果"，
  在**16 条**语料上**不成立**：查 `归档` 走 `归 OR 档` 命中 [1,2]，查 `归` 只命中 [1]，
  且 `bm25` 给"命中整词"的 [1]（2.4067）高于只中单字的路径。
  **原论断是在极小语料上得出的**；本轮在 4 条语料的复现里 `bm25` 全为 0.0000，
  说明**小语料下 `bm25()` 退化为无信息量**，那个观察更像是量具伪影而不是行为事实。
  → 该论断在 §2.2/§3/§6.1 的**定性结论仍然方向正确**（逐字 OR 让"归"和"档"平权），
  但**"结果相同"这句话应当弱化为"粒度被拉到单字、整词信息丢失"**。
- **真正的差异在排序**：查 `归档`（想要"归档这件事"）时，**FTS 把 [1]「归档旧的任务记录」排在 [2]「把过期条目存档」前面**；
  **嵌入反过来把 [2]（cos 0.7393）排在 [1]（0.6330）前面**——因为 [2] 谈的是"条目过期存档"这个**概念**，
  而 [1] 只是字面含"归档"二字。这正是"字面命中 ≠ 相关"（样本 #16 那句话本身）的实例。

**嵌入真正拉开差距的地方（同义改写）**：

| 查询 | FTS5 命中（组） | 嵌入 top3（组） |
|---|---|---|
| `清理旧文件` | [3]G1、[1]G1、[13]**G4**、[5]**G2**、[9]**G3**（5 条里 3 条跑组） | [3]G1 0.7072、[2]G1 0.6398、[4]G1 0.5661 —— **全部 G1** |
| `窗口快满了怎么办` | [11]G3、[10]G3（**字面撞上"窗/口/满/了"**才命中） | [11]G3 0.7565、[8]G2 0.3520、[2]G1 0.3238 |
| `打包管理`/`包管理` | [5]G2、[6]G2、[3]G1 | [2]**G1** 0.4866、[10]**G3** 0.4106、[8]G2 0.3795 ← **失手** |

**嵌入的失败案例必须写进去**（否则就是把选型建立在乐观读数上）：

1. **技术缩写/裸标识符**：查 `uv` → 嵌入 top1 是 [2]「把过期条目存档」（0.5214），
   而 FTS5 精确命中 [5]（唯一含 `uv` 的那条）。**符号级精确匹配是 FTS5 的强项，嵌入会丢掉**；
2. **领域词法**：查 `包管理` → 嵌入 top1 是 G1 的 [2]，把 G2 排到第 3；
3. 因此**嵌入不能替代 FTS5**——这正是 §14.4 把两者做成"两路候选 → 合并重排"的实测依据。
4. **[2026-10-02 二轮更正，见 §14.3]** 上面 1、2 两条是**这个模型（MiniLM）的失手**，
   **不是"嵌入"这一类的失手**：同一份 16 条语料 + 同一批查询下，
   `minishlab/potion-multilingual-128M`（model2vec）查 `uv` 的 top1 **就是 [5]**
   （cos 0.5613，第二名 0.0573，差一个数量级），查 `包管理` 的 top1 **也是 [5]**。
   → 「符号级精确匹配嵌入一定会丢」**在 D 上不成立**，只在 MiniLM 上成立。
   第 3 条（**嵌入不能替代 FTS5**）**仍然成立，但依据要换**：
   换成本轮实测的**弱项**——`清理旧文件` 上 D 的 P@5（0.60）仍低于 MiniLM（0.80），
   且"符号级查询这次做对"只有 n=2 的样本，**不足以担保稳定**。
   所以两路合并的结论不变，**只是不再拿 `uv` 当反例**。

> **同节的另一处更正**：本节末尾说"换成任何本机没有缓存的模型，在本机当前网络下会直接失败"。
> **[2026-10-02 二轮实测]** 代理 `127.0.0.1:7890` 现在**在听**，`pypi.org` 与 `hf-mirror.com`
> 都返回 **HTTP 200**（`huggingface.co` 仍被重置）。所以现在**下载得到**（本轮就是这么拿到 D 的），
> 那句"必然失败"已过期；详见 §14.3.5 第 3 条。

> **实验侧注意**：本实验用的模型**本来就在本机 HF 缓存里**，所以"离线可用"这条是**实测成立**的；
> 换成任何**本机没有缓存**的模型，在**本机当前网络下会直接失败**（§14.1.2 的出口不通）。

### 14.2 第二步：候选矩阵

评分维度固定 9 项。**"体积"一列凡标"不确定"的都是本轮没取到事实的**，不许当结论用。

| 维度 | **A** 复用本机 llama-server `/v1/embeddings` | **B** `sqlite-vec` + 小 ONNX 模型 | **C** `fastembed` 一类 onnxruntime 封装 | **D** `model2vec` 静态嵌入 | **E** `sentence-transformers` / torch | **F** 纯 BM25/FTS5 |
|---|---|---|---|---|---|---|
| **决定** | **否决** | **候选（可选索引层）** | **候选（仅在需要 ONNX 时）** | **推为首选默认** | **否决** | **永远可用的回退与基线** |
| 安装体积 | 0（**但那是唯一优点**） | `sqlite-vec` ~0.16 MB + ONNX 运行时（1.30.0 cp314 wheel **14.0 MB**，解包 37.3 MB 参考）+ `numpy` 十几 MB | 同 B 的 ONNX 链路，**再叠一层封装**；包体本身小但**传递依赖多** | wheel **0.06 MB** + **实为 24 个包（`site-packages` 解包 67.4 MB，见 §14.3.5）**；模型：**英文单语** 15–248 MB，**唯一多语言项 `potion-multilingual-128M` 实测 512.47 MB 落盘**（**比 MiniLM 还大**） | **torch 解包 5 528.6 MB（实测）** + transformers 53.6 MB | **0** |
| 是否拖 torch | 否 | 否 | 否 | **否**（`torch` 只在 extras） | **是（必然）** | 否 |
| 断网可用 | 取决于服务在不在跑；**实测 501** | **是**（ONNX 文件落盘后） | **是，但首次要下载**（默认模型） | **是**（模型文件落盘后） | 是 | **是（本来就无网络需求）** |
| Windows 支持 | 是（但**本机未开 `--embeddings`**） | `py3-none-win_amd64` 纯二进制 tag，**不受 Python 版本限制**；本机 `sqlite3` 3.50.4 **`enable_load_extension` 实测 OK**、未编译 `OMIT_LOAD_EXTENSION` | 依赖链均提供 win_amd64 | 纯 Python + numpy | 提供 win_amd64 | 是 |
| 许可 | 随 llama.cpp（MIT）/模型各自许可 | `sqlite-vec` **MIT OR Apache-2.0**；模型各自许可 | 库 Apache-2.0 系；**模型许可要逐个看** | **MIT**（库），模型各自许可 | Apache-2.0 | 随 SQLite（public domain） |
| CPU 延迟 | 未测（501）；**且要占 27B 模型的算力** | 未测 ONNX 侧；**SQLite 内检索**通常亚毫秒级（**未测**） | 未测 | 官方称"最多快 500×"（**厂商声明，非本机实测**） | 本机实测参照：MiniLM(117.7M, torch CPU) 单查询 **14.2 ms**、批 32 稳态 **2.3 ms/条** | **本机实测**：16 条语料下 FTS 查询基本即时（未单独计时） |
| 与 SQLite 的集成 | 无（外部 HTTP） | **最好**：索引与数据同库，`vec0` 虚表 + 事务 | 无（向量在 Python 内存） | 无（向量在 Python 内存） | 无 | **原生**（同一库同一事务） |
| **我们能否完全掌控上下文预算** | **不能**：服务端行为、维度、超时都不在我们手里；且它同时是**生成**服务，负载不可预期 | **能**：候选数 N 与重排数 K 由我们定，向量不回传进上下文 | 能，但**首次下载行为不由我们掌控**（默认模型、缓存路径） | **能**，且代价最小（无 session，纯前向查表） | 能，但为它付 5.5 GB 与常驻内存 | **完全能**（唯一一个"零不确定"项） |
| 不可用时的回退行为 | 服务没跑 → 直接没有信号（**且很容易变成静默降级**） | 扩展加载失败 → 退 BM25（**要显式可见**） | 模型没下载/断网 → 退 BM25 | 模型文件缺失 → 退 BM25 | 环境没装 → 退 BM25 | 它本身就是回退终点 |

**逐条说明**：

- **A 否决**：不只是因为实测 501。即便加上 `--embeddings`，它也会把"记忆检索"绑在
  一个**同时服务生成、由用户决定何时重启的 27B 进程**上（本任务本身就被禁止重启它），
  违反 §12 第 7 条"不给记忆单独做常驻服务"的同一条纪律，并让**上下文预算的一部分交给我们无法控制的进程**。
- **E 否决**：**代价是 5 528.6 MB 的 `torch`（实测解包）**，换来的是"比 D 更好的语义质量"，
  而本仓的场景是**单机单人、中文短条目、几百到几千条**。
  与本轮实测的收益（把"清理旧文件"从 5 条跑 3 组变成 3 条全对）相比，**5.5 GB 的常驻代价不成比例**。
  另外它会把"记忆能不能用"绑在"CUDA/CPU 版 torch 装没装对"上，多一条可失败初始化路径。
  **注意**：否决的是"**默认引入**"，不是"永远不许"；如果将来 M2/M3 量出 D 的质量不够，
  允许在**开发者本地**用 E 做对照实验，但**不进产品默认路径**。
- **D 首选的理由**：唯一一个"**没有 session、没有图、没有运行时**"的方案——
  静态嵌入就是"查表 + 池化"，`numpy` 是主要依赖，**不拖 torch、不需要 onnxruntime、CPU 友好、MIT、断网可用**，
  **单查询延迟 0.042 ms**（MiniLM 的 15.96 ms，约 **380×**，§14.3.2）。
  **代价如实写（已按 §14.3 实测更正）**：静态嵌入的语义分辨力**低于** transformer 嵌入——
  **这条在本轮中文语料上没有复现**（D 的 top1 1.000 反而**高于** MiniLM 的 0.692），
  但这只是 16 条语料上的读数，**不能推翻厂商的总体结论**；
  **体积优势也不成立**：D 的**多语言**模型实测 **512.47 MB**，
  比 §14.1.2 里 448.8 MB 的 MiniLM **还大**（"8–30 MB"只是**英文单语**模型，不适用中文）；
  D 真正的优势是**不拖 5.5 GB 的 torch**、**不需要 onnxruntime**、**延迟低约 380×**。
  质量证据见 §14.3.2。
- **B 作为可选**：只在"候选集大到需要在 SQLite 内做 ANN"时才值得引入
  （`sqlite-vec` 与数据同库、事务一致、体积极小、**不受 Python 版本限制**）。
  本仓规模（`semantic` 每 kind 上限 50–200 条）**根本不需要 ANN**——
  几百条向量在 Python 里一次点积是微秒级。**所以 B 不进 P1**，列为 P2 之后的按需项。
- **C 作为可选**：`fastembed` 的价值是"省掉自己写 ONNX 前后处理"。
  但它**默认会联网下载模型**，而本仓要求"不许悄悄联网下载几十 MB 到用户目录而不告知"（§14.5）。
  要用它就必须：显式 `--cache-dir` 指向工作区/`%APPDATA%\spoolkit`、首次使用必须**用户确认**、
  并且**在 `limits.json` 里登记模型名与体积**。满足这些条件时可用，否则退 D 或 F。
- **F 是回退终点**：它不是"备选"，是**基线**。§10 的 M1/M2/M3 全部以它为零点。

### 14.3 那次待补测量：已完成（2026-10-02 二轮实测）

> **本节状态已变更。** 原标题是「若证据不足，还差哪一次测量」——那一次测量**已经跑完**，
> 因此本节改为记录**实测数字、判据与结论**。所有数字都是本轮实跑读数。
> **语料与查询已写进脚本文件**（不再只存在内存里），路径见 §14.3.6。
> **仍然没测到的**单列在 §14.3.7，**不许**当结论用。

#### 14.3.1 结论（先给结论）

| 问题 | 结论 |
|---|---|
| MiniLM 基线可复现吗？ | **可复现**，且在**两个互相独立的环境**里各复现一次（§14.3.3） |
| 因此用哪条判据？ | **判据 A**（原设计那条）：D 的 top1 ≥ MiniLM top1 的 80% |
| D（`model2vec`）达标吗？ | **达标且大幅超出**：top1 **1.0000** vs 阈值 **0.5538** |
| `w_embed` 默认值改吗？ | **不改，仍是 `0.0`**（opt-in） |
| 够不够格进 P1 可选增强？ | **够**（作为**默认关闭**的可选增强，不替换 FTS5） |

**一句话结论**：`model2vec`（`minishlab/potion-multilingual-128M`）**有资格作为 P1 的
「默认关闭的可选增强」**；它**不是**默认开启，也**不是** FTS5 的替代品。

#### 14.3.2 对照表（同一份 16 条语料、同一批查询、同一套指标）

**查询集是 13 条，不是 10 条**：§14.1.3 定了 10 条；本任务另外点名必须进集合的 3 条——
`清理旧文件`、`窗口快满了怎么办`（§14.1.3 记的**两处 FTS5 弱项**）
与 `包管理`（§14.1.3 记的 **MiniLM 失手**；`uv` 本来就在那 10 条里，同属 MiniLM 失手）。
这 3 条加进去后**四个后端跑的都是同一批 13 条**，
所以可比性没有被破坏；要与 §14.1.3 的 10 条对齐，看下面的 core-10 子集。

**全部 13 条查询**（P@5 / R@5 / top1 都是 13 条的平均）：

| 后端 | 维度 | 加载 s | 单查询 ms | **P@5** | **R@5** | **top1** | 说明 |
|---|---|---|---|---|---|---|---|
| **FTS5（`store.py` 口径）** | — | 0.008 | 0.025 | **0.338** | **0.596** | **0.769** | `MATCH` + `_rank`（本实验里退化=写入序） |
| FTS5（`bm25()` 排序） | — | 0.008 | 0.019 | 0.338 | 0.596 | **1.000** | §14.1.3 表格用的那个排序 |
| **model2vec** `potion-multilingual-128M` | **256** | **2.51** | **0.042** | **0.477** | **0.769** | **1.000** | model2vec 0.9.0 / numpy 2.5.3 |
| MiniLM `paraphrase-multilingual-MiniLM-L12-v2` | **384** | **6.33** | **15.96** | **0.446** | **0.673** | **0.692** | sentence_transformers 5.4.1 / transformers 5.3.0 / torch 2.10.0+cpu |

**core-10 子集**（与 §14.1.3 的 10 条同集合）：

| 后端 | P@5 | R@5 | top1 |
|---|---|---|---|
| FTS5（`store.py` 口径） | 0.320 | 0.625 | 0.800 |
| FTS5（`bm25()`） | 0.320 | 0.625 | 1.000 |
| **model2vec** | **0.440** | **0.775** | **1.000** |
| MiniLM | 0.400 | 0.650 | 0.700 |

**必须有的一句话**：**`bm25()` 排序的 FTS5 在 top1 上也是 1.000**，
所以**top1 这一项分不出「FTS5 vs model2vec」**——
真正拉开差距的是 **P@5 与 R@5**（0.338/0.596 → 0.477/0.769）。
只用 top1 判优劣会把这条测量读成"两者一样好"。

**逐条查询的真实读数**（`[n]` 是语料编号；embedding 后附余弦）：

| 查询 | FTS5（store 口径） | model2vec top5 | MiniLM top5 |
|---|---|---|---|
| `归档` | `[1, 2]` | `[1, 2, 7, 3, 9]` (0.613/0.458/0.222) | `[2, 1, 12, 3, 13]` (0.739/0.633/0.584) |
| `归` | `[1]` | `[1, 5, 6, 3, 15]` (0.439/0.178/0.133) | `[2, 1, 12, 10, 3]` (0.602/0.501/0.442) |
| `存档` | `[1, 2]` | `[2, 1, 7, 9, 3]` (0.701/0.458/0.292) | `[2, 12, 1, 10, 13]` (0.765/0.571/0.565) |
| `备份` | `[3]` | `[3, 2, 15, 12, 9]` (0.399/0.313/0.238) | `[2, 1, 3, 12, 10]` (0.706/0.574/0.563) |
| `依赖管理` | `[3, 5, 6]` | `[5, 6, 7, 8, 1]` (0.488/0.457/0.223) | `[5, 8, 2, 10, 6]` (0.720/0.499/0.458) |
| **`uv`（点名失败案例）** | `[5]` | **`[5, 10, 8, 9, 1]` (0.561/0.057/0.017)** | `[2, 12, 10, 1, 11]` (0.521/0.423/0.399) ← **失手** |
| `上下文预算` | `[9, 11, 13]` | `[9, 15, 5, 11, 2]` (0.614/0.194/0.178) | `[9, 5, 2, 8, 10]` (0.572/0.427/0.422) |
| `让位` | `[10]` | `[10, 14, 2, 4, 8]` (0.442/0.312/0.238) | `[2, 10, 12, 11, 6]` (0.647/0.536/0.531) ← **失手** |
| `全文检索` | `[9, 11, 13, 14, 15]` | `[13, 14, 15, 12, 9]` (0.559/0.445/0.363) | `[12, 13, 2, 14, 1]` (0.695/0.654/0.648) ← **失手** |
| `排序` | `[14]` | `[14, 12, 2, 10, 1]` (0.620/0.223/0.151) | `[14, 12, 2, 15, 1]` (0.739/0.531/0.476) |
| **`清理旧文件`（成功案例）** | `[1, 3, 5, 9, 13]`（组内 **2/5**，P@5=0.40） | `[3, 1, 4, 9, 12]`（组内 **3/5**，P@5=0.60） | `[3, 2, 4, 12, 1]`（组内 **4/5**，P@5=0.80） |
| **`窗口快满了怎么办`（成功案例）** | `[10, 11]`（组内 **2/5**，P@5=0.40） | `[11, 7, 8, 10, 9]`（组内 **3/5**，P@5=0.60） | `[11, 8, 2, 12, 10]`（组内 **3/5**，P@5=0.60） |
| **`包管理`（点名失败案例）** | `[3, 5, 6]` | **`[5, 6, 1, 15, 8]` (0.243/0.223/0.153)** | `[2, 10, 8, 5, 1]` (0.487/0.411/0.380) ← **失手** |

**三点必须如实写出来的读数**：

1. **§14.1.3 的 FTS5 表被逐字复现**：13 条查询的 `_match_query` 展开式与命中集合
   （`归档`→`归 OR 档`→`[1,2]`、`上下文预算`→`[9,11,13]`、`清理旧文件`→`[1,3,5,9,13]`、
   `窗口快满了怎么办`→`[10,11]`……）**与 §14.1.3 的表完全一致**。
   → 那份 FTS5 基线是可信的，本节的比较有一个真基线。
2. **§14.1.3 的 MiniLM 表也被逐字复现**：10 条核心查询的 **cos 值四位小数全等**
   （`归档`→`0.7393/0.6330/0.5835/0.5185/0.4585` 与原文一模一样），
   另外 3 条也全等。→ 上一轮的 MiniLM 读数**不是估算，是真跑**。
3. **D 把 MiniLM 的两处"嵌入失手"都做对了**：`uv` 的 top1 是 `[5]`（cos 0.5613，
   第二名只有 0.0573，差一个数量级），`包管理` 的 top1 也是 `[5]`。
   → **这两处失败是 MiniLM 这个模型的失败，不是"嵌入"这一类的失败**（§14.1.3 已据此更正）。

#### 14.3.3 疑点澄清：MiniLM 到底能不能跑、谁跑的

上一轮报告里"借 HF 缓存里的 MiniLM 真跑了对照"缺一个交代：**用哪个解释器加载的**？
（仓库 `.venv` 里 `torch`/`transformers`/`onnxruntime` **全部 MISSING**，见 §14.1.2。）
本次查清如下，**两个疑点都有确定答案**：

**疑点 1：那个模型目录的 blobs 是不是真的？——是，而且完整。**

| 项 | 实测 |
|---|---|
| `blobs` 条目数 | **10 个** |
| blobs 求和 | **479 729 010 B = 457.5 MB** |
| `model.safetensors` | **470 641 600 B = 448.8 MB**（与 §14.1.2 写的 448.8 MB 一致） |
| `tokenizer.json` | **9 081 518 B = 8.66 MB**（§14.1.2 写 8.7 MB，一致） |
| **0 字节的 blob** | **0 个**（**该模型目录里没有任何空壳**） |
| snapshot 里的 `model.safetensors` | 是符号链接，指向上面那个 448.8 MB 的 blob，**不是 0 字节占位** |

→ §14.1.2 「HF 缓存里已经有可离线使用的多语言嵌入模型」这句**成立**，
在**这个模型目录上**没有"0.0 MB 空壳"问题（空壳问题在本机其它几个缓存目录里可能存在，但与本模型无关）。

**疑点 2：哪个解释器/包能加载它？——本机至少有 5 个环境能，且 A.7 早就写了是哪个。**

本机全盘探针（12 个候选解释器）结果：

| 解释器 | Python | 能加载 MiniLM 的包 |
|---|---|---|
| `D:\workSpace\pyvideotrans\.venv\Scripts\python.exe` | 3.10.19 | torch 2.7.1+cu128 / transformers 4.57.6（**无** `sentence_transformers`） |
| `C:\Users\32615\.pyenv\pyenv-win\versions\3.14.3\python.exe` | 3.14.3 | torch 2.10.0+cpu / transformers 5.3.0 / sentence_transformers 5.4.1 |
| `C:\Python314\python.exe` | 3.14.3 | 同上 |
| `C:\Users\32615\.pyenv\pyenv-win\versions\3.11.9\python.exe` | 3.11.9 | torch 2.11.0+cpu / transformers 5.7.0 / sentence_transformers 5.4.1 |
| `C:\Users\32615\AppData\Local\Programs\Python\Python311\python.exe` | 3.11.9 | 同上 |

- **上一轮用的就是上表第一行**（附录 A.7 已经写明）；
  它**没有** `sentence_transformers`，所以那一轮只能走 `transformers` 的 `AutoModel` + 手写均值池化。
  → 「没说是用哪个解释器加载的」这个缺口，**是 §14.1.2 只探了仓库 venv、没指向 A.7**，
  **不是那一次的读数站不住**。
- **本轮把这条路真跑了一遍**：在 `pyvideotrans\.venv` 里
  （torch 2.7.1+cu128 / transformers 4.57.6 / numpy 1.26.0，离线、不装任何包）
  用 `AutoModel` + 均值池化复算查询 `归档`，得到
  `[2] 0.7393, [1] 0.6330, [12] 0.5835, [3] 0.5185, [13] 0.4585`
  —— 与 §14.1.3 表**逐位相同**。
- 另一路（pyenv 3.14.3 + `sentence_transformers` 5.4.1）复算全部 13 条查询，
  **cos 值同样与 §14.1.3 四位小数全等**。

> **结论：MiniLM 基线可比、可复现，而且是"换一个环境、换一条实现路径仍然复现"。**
> 因此 §14.3 的判据**用判据 A**（而不是自足判据 B）。
> 注：上一轮 Appendix A.7 记的加载耗时 0.84 s 与本次 6.33 s 不矛盾——
> 本次的「加载」是**从进程启动算起、含 `import torch`**（其中 import 5.15 s、建模 1.18 s），
> A.7 报的是**进程内的加载**；两者量的不是同一段。

#### 14.3.4 判据评估（两条都算，结论一致）

**判据 A（原设计，因 MiniLM 可复现而生效）**：

```
MiniLM top1       = 0.6923
阈值 = 80% × 0.6923 = 0.5538
model2vec top1    = 1.0000
→ 0.5538 ≤ 1.0000   PASS（不仅达标，而且高于基线本身）
```

**判据 B（自足判据，作为交叉验证）**——两条必须同时成立：

| 条件 | 实测 | 结果 |
|---|---|---|
| ① `model2vec` P@5 ≥ FTS5 P@5 | 0.4769 ≥ 0.3385 | **满足** |
| ① `model2vec` R@5 ≥ FTS5 R@5 | 0.7692 ≥ 0.5962 | **满足** |
| ② 在 §14.1.3 点名的 FTS5 失败案例上给出正确 top1 | `清理旧文件` 组内 2/5→**3/5**（P@5 0.40→0.60）；`窗口快满了怎么办` 组内 2/5→**3/5**（P@5 0.40→0.60） | **满足** |

> **判据 B 的口径要说清**：任务书把 `uv`/`包管理` 也列为"FTS5 失败案例"，
> 但按 §14.1.3 的原文，这两条是**嵌入（MiniLM）失手、FTS5 做对**的案例。
> 所以②只在**真正的 FTS5 弱项**上评估（`清理旧文件`、`窗口快满了怎么办`），
> 而 `uv`/`包管理` 作为**回归检查**另列：D 在这两条上**都做对了**（top1 均为 `[5]`），
> **没有复现 MiniLM 的回归**。
> 另外「`归档` vs `归` 的粒度问题」在**四个后端上"结果都不同"**
> （FTS5 `[1,2]`/`[1]`；model2vec `[1,2,7,3,9]`/`[1,5,6,3,15]`；
> MiniLM `[2,1,12,3,13]`/`[2,1,12,10,3]`）
> → §14.1.3 对附录 A.3 那句"两者返回同一批"的更正**仍然成立**。

**因此：结论是「够格进 P1 的可选增强」，`w_embed` 默认仍为 `0.0`。**

#### 14.3.5 本轮顺带推翻的三处旧结论（必须改口径，否则会误导选型）

1. **"model2vec 的主要依赖只有 numpy"——不成立。**
   实测 `uv pip install model2vec numpy` 一次拉进 **24 个包**，
   `site-packages` 解包 **67.4 MB**（numpy 19.7 + numpy.libs 20.2 + hf_xet 9.1 +
   tokenizers 7.7 + huggingface_hub 3.1 + jinja2/fsspec/joblib/safetensors/httpx…）。
   下载的 wheel 里 `numpy` 12.1 MiB、`tokenizers` 2.7 MiB、`hf-xet` 3.8 MiB。
   → §14.2 的 D 行"是否拖 torch：否"**仍然对**（确实没拖 torch），
   但**"1 个依赖"的说法不成立**：它是**一条 24 包的依赖链**。
2. **"model2vec 模型 8–30 MB、比 MiniLM 小 1–2 个数量级"——只在英文模型上成立。**
   实测审计了 `minishlab/*` **全部 17 个模型仓**的体积：
   `potion-base-2M` 15.3 MB / `-4M` 29.7 MB / `-8M` 58.6 MB / `-32M` 248 MB —— **都是英文单语**；
   **唯一的多语言模型**是 `potion-multilingual-128M`，
   它的 `model.safetensors` = **512 361 560 B = 488.6 MB**，
   落盘（含 tokenizer 17.75 MB + vocab 6.06 MB，已跳过 onnx 副本）**合计 512.47 MB**。
   → **它比 MiniLM 的 448.8 MB 还大**。
   §14.2 「D 首选的理由」里"模型 8–30 MB 与 448.8 MB 差 1–2 个数量级"这句
   **对中文场景是错的**；§14.5 的"`numpy` + 8 MB 级模型"同样要改。
   **D 相对 E 的体积优势（不拖 5.5 GB 的 torch）仍然成立**，
   但**相对 MiniLM 的体积优势不存在**。
3. **网络与代理的旧结论已过期。** 实测：`127.0.0.1:7890` **在听**（TCP 握手成功，
   `CONNECT pypi.org:443` 返回 `HTTP/1.1 200 Connection established`），`1080` **主动拒绝**；
   走 7890 时 `pypi.org` **HTTP 200**、`hf-mirror.com` **HTTP 200**，
   但 **`huggingface.co` 被重置**（`WinError 10054`）。
   §14.1.2 / 附录 B.7 写的"出口不通、7890 无监听"**现在不成立**。
   另有一条与网络无关的坑要记下来：**Windows 的 schannel 全线失败**
   （`Invoke-WebRequest`/`curl.exe` 报 `SEC_E_NO_CREDENTIALS`），
   所以那两条命令**测不出网通不通**，必须换 Python/OpenSSL 或 `uv`（rustls）才准。

#### 14.3.6 复现路径（脚本与语料都在仓里，不在内存里）

**位置：`.tmp\embed-bench\`**（临时目录，不进产品、不进 `pyproject.toml`）。

| 文件 | 作用 |
|---|---|
| `bench_common.py` | **语料 16 条 + 查询 13 条 + 相关度标签 + 指标定义**（本节唯一的口径来源） |
| `run_fts5.py` | 后端①：逐字复制 `store.py` 的 `normalize_for_fts`/`_match_query`，附 `bm25` 排序 |
| `run_embed.py` | 后端②③：`model2vec` / `minilm`（按参数切换，各自在能跑的解释器下运行） |
| `download_m2v.py` | 经 `hf-mirror` 取 `potion-multilingual-128M`（跳过 `onnx/*`） |
| `report.py` | 汇总对照表 + 自动判判据 A/B；输出 `REPORT.txt` |
| `results_*.json` | 四个后端的**逐条原始读数**（命中序列、cos、延迟、版本、解释器路径） |
| `check_latency.py` / `check_appendix_a7_interp.py` | 延迟独立复核 / A.7 解释器复核 |
| `probe_interp.py` / `list_models.py` / `probe_hosts.py` | 解释器探针 / 模型体积审计 / 端点连通性 |

**环境（一次性、隔离）**：`.tmp\embed-venv`（`uv 0.10.8` 建，CPython **3.14.3**），
装的是 `model2vec==0.9.0` + `numpy==2.5.3`。**仓库 `.venv` 全程未被触碰**（未装、未改）。
模型落在 `.tmp\hf\models\potion-multilingual-128M`。

> **纪律**：这份依赖只存在于临时 venv 里。**产品侧零新依赖的对象性没有变**——
> `pyproject.toml` **一个字都没改**，本节只是在论证"若将来加 `embed-light` extra，值不值"。

#### 14.3.7 本节仍然没测到的

1. **`onnxruntime` / `sqlite-vec` / `fastembed` 一条路径都没跑**（方案 B/C）。
   网络现在通了，但本轮的任务只要求测 D；**B/C 的延迟与 Windows 行为仍是空的**。
2. **没有测常驻内存**（RSS）：与 §14.7 第 4 条同一原因，探针拿不到读数。
   已知的只有模型**落盘**体积（D 512.47 MB / MiniLM 457.5 MB）与**向量本体**体积（§14.1.3）。
3. **语料太小（16 条）、查询太少（13 条）**：分组查询的 P@5 上限只有 0.8、
   R@5 上限 0.8，**绝对数字不能外推**到几百条的真实库；能外推的只有**相对排序**。
4. **每组只有 4 条**，`包管理`/`uv` 这类"符号级"查询各只有 1 条样本，
   **不足以断言 D 的符号级召回稳定**（它这次做对了，但 n=2）。
5. **没有跑 §10 的 M1/M2' 端到端回归**——M2' 现在有了基线数字，但**尚未变成 CI 里的护栏**。
6. **没有验证中文以外的多语言质量**（`potion-multilingual-128M` 声称 101 种语言，本轮只测中文）。

### 14.4 第三步：上下文预算纪律（硬规矩）

这一节是用户最在意的一点，**逐条都是可检查的规矩，不是原则宣言**。

#### 规矩 1：嵌入只是五维打分里的**一个信号**

- `rel`（§6.1 的相关度维度）由**两路候选合并后**得到，公式固定为：

  ```
  rel = normalize( w_bm25 * bm25_norm  +  w_embed * embed_cos )
  ```

  `w_bm25` / `w_embed` 全部登记进 `limits.KNOBS`（`CAPABILITY`，可按工作区覆盖），
  **默认 `w_embed = 0.0`**——即**没装嵌入时行为与现状逐字一致**。
- **禁止**：用嵌入替换 FTS5；用嵌入单独决定取哪几条；把 `embed_cos` 当"相关"的同义词。</br>
  依据就是 §14.1.3 的实测：`uv` 与 `包管理` 两个查询上**嵌入错、FTS5 对**。

#### 规矩 2：切块策略（按什么切、多大）

- **切块单位 = 一条 `semantic` 条目**，不做二次切分。理由：`remember_max_chars = 400`（§4.3）
  已经给单条封了顶，且条目本身是原子语义单元；再切会把"一条结论"拆成不成句的碎片，反而伤 `bm25`。
- **嵌入输入文本** = `text` + `\n` + `trigger`（`trigger` 是逗号分隔关键词，对 lesson/skill 必填）；
  **`evidence` 不进嵌入输入**（它是 `file:line` 这类高熵短串，会污染句向量），但**进 FTS5 索引**（§4.3 已定）。
- **超限处理**：条目文本 > 256 token 时**只取前 256 token 做嵌入**（**[实测]** 该截断在本机 MiniLM 上生效，
  `attention_mask.sum()` = 256），**但 `text` 原样存储、原样检索**——即"嵌入被截断"不影响可读性与 FTS 命中。
- **块大小上界（写进 `limits`）**：`embed_chunk_chars = 400`（= `remember_max_chars`，**两者必须相等**，
  否则会出现"能存进去但嵌不全"的静默不一致）。

#### 规矩 3：候选 → 重排两阶段（N / K 全部进 `limits` 且可覆盖）

```
查询 ──┬── FTS5/bm25 取 topN_bm25 ──┐
       │                            ├── 合并去重 ──▶ 五维打分重排 ──▶ 取 topK ──▶ 进上下文
       └── 嵌入余弦取 topN_embed ───┘         （rel/rec/imp/use/penalty）
```

| 旋钮 | 默认 | 含义 | 可覆盖 |
|---|---|---|---|
| `embed_candidates_n` | **50** | 每路各取多少候选（两路相加后合并去重，实际唯一候选 ≤ 100） | `CAPABILITY`，`.agent/limits.json` |
| `embed_rerank_k` | **与现有 `recall_limit` 同值（5）** | 重排后真正进上下文几条 | 同上 |
| `embed_min_cos` | **0.25** | 低于此余弦值**不参与 `w_embed` 项**（防噪声项拉偏排序） | 同上 |
| `embed_timeout_ms` | **50** | 单轮嵌入编码的墙钟上限；超时即按规矩 5 回退 | 同上 |

**硬约束**：`embed_rerank_k ≤ recall_limit`，且**重排只能减少进上下文的条数，不能增加**。
理由：现在 `recall` 一次最多 5 条 × 240 字（§6.2），重排若能把 K 顶到 10，
预算表里那 1200 token 立刻失效。

#### 规矩 4：最坏占用（token 与内存各给上界与算法）

**token 上界（算法）**：嵌入**不新增任何上下文通道**，它只改变"这 K 条是哪几条"。
因此：

```
记忆每轮最坏 token = hot(1044) + lessons(696) + recall(K × 240 字)
                    且受 memory_budget_ratio(0.30) × 有效预算 = 2089 封顶
```

**结论：引入嵌入后，§6.2 那张表的每一个数字都不变。** 这是刻意的设计结果，不是巧合：
把嵌入限定在"排序信号"这个位置，是**唯一**能保证"加能力不加预算"的接法。

**内存上界（算法）**：

```
embed_mem_bytes ≈ n_active × dim × 4              # 向量本体
                + n_active × embed_chunk_chars × 2  # 候选文本（临时，重排后可释放）
                + model_bytes                       # 模型权重常驻
```

以本机实测值代入（`dim = 384`，`float32`）：

| `n_active` | 向量本体 | 模型（MiniLM 448.8 MB） | 模型（model2vec ~8 MB 级） |
|---|---|---|---|
| 200 | 0.29 MB | 449.1 MB | ~8.3 MB |
| 1000 | 1.46 MB | 450.3 MB | ~9.5 MB |
| 5000 | 7.32 MB | 456.1 MB | ~15.3 MB |

**结论**：**内存的大头是"模型权重"，不是向量库**。所以"检索留在 SQLite 内"（方案 B）
省下的是**亚 MB 级的向量存储**，而不是那几百 MB —— 这一条直接解释了为什么 B 不值得进 P1。

**时间上界**：单条查询 **14.2 ms**（**[实测]**，MiniLM/torch CPU，30 次平均）+ 重排
（≤100 条 × 384 维点积，微秒级）。`embed_timeout_ms = 50` 给了约 3 倍余量。

#### 规矩 5：超预算与不可用时的让位顺序

**让位顺序沿用 §6.2、不改**：`recall` > 教训推送 > 热记忆（热记忆是每轮都要的底账，最后让）。
嵌入作为 `recall` 通道内部的一个信号，**让位时先降 `w_embed` 再降 `embed_candidates_n`，最后整个关掉嵌入**——
**绝不允许**用"少给热记忆"来换"多给嵌入候选"。

#### 规矩 6：回退链（**行为不许崩、不许静默降级**）

```
嵌入可用 ──▶ 两路候选合并重排
   │
   ├─ 没装依赖（ImportError）──────┐
   ├─ 模型文件缺失 / 首次未预热 ────┤
   ├─ 断网且无本地模型 ────────────┼──▶ 退纯 BM25（= 现状行为），
   ├─ 编码超时（> embed_timeout_ms）┤    且必须**在三个出口同时可见**：
   └─ 余弦全部 < embed_min_cos ─────┘    ① memory_decision 写 used_embed=0 + reason
                                          ② --events memory 事件带 "embed":"off:<reason>"
                                          ③ spool memory why 打印同一行
```

**"不许静默降级"的可检查定义**（这是本节最重要的一条）：

- `memory_decision` 表**新增 `embed_status` 列**（`ok` / `off:not_installed` / `off:no_model` /
  `off:offline` / `off:timeout` / `off:below_threshold`）——**加列，不改既有列**；
- `--events memory` 的既有一行里**只加字段**，不改既有语义（沿用 §6.3 与心跳 `wait` 的同一做法）：
  `{"type":"memory","channel":"recall","count":5,"tokens":1180,"budget":2089,"embed":"off:no_model",...}`；
- **验收时直接 grep 这三个出口**：任何一次"实际没用嵌入"都必须能在至少一个出口看到。
  做不到就是静默降级，视为不合格。

#### 规矩 7：离线与首次使用（**不许悄悄联网下载**）

- **模型来源优先级**：① 工作区内 `<工作区>/.agent/memory/models/`（随工作区走，最可控）
  → ② `%APPDATA%\spoolkit\models\`（与用户级 `.env` 同目录，`specs/2026-09-27-...:68-69`）
  → ③ 本机 HF 缓存（如本机已有的 `paraphrase-multilingual-MiniLM-L12-v2`）
  → ④ **联网下载，且必须用户显式确认**。
- **首次使用流程**：`spool memory embed setup [--model <name>]` 才允许下载；
  执行前**打印**预计体积与落盘路径并等确认；下载中显示进度；失败不改变任何既有行为。
  日常路径（`recall` / `spool run`）**永远不触发下载**——检测到模型不在就按规矩 6 退 BM25 并标 `off:no_model`。
- **预热**：`spool memory embed warm`（或 P1 开局的可选一次），
  目的是把 §14.1.3 量到的 **0.84 s 模型加载**挪出首轮交互；
  预热失败**不阻塞**任务。
- **清理**：`spool memory embed clean [--model] [--all]` 删除模型与缓存，并写 audit；
  `spool memory doctor` 报告"模型在哪、多大、是否可用、当前权重 `w_embed` 是多少"。
- **环境变量**：进程内固定设 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`（**[实测]** 这两个变量下
  本机缓存加载成功且零下载），**除非**用户显式走了 `embed setup`。

### 14.5 第四步：把选型落进分期

#### 默认方案 + 可选方案

| 层 | 方案 | 是否要用户额外装东西 |
|---|---|---|
| **P1 默认（0 安装）** | **F：纯 FTS5 + `bm25()` 五维重排 + 负反馈**（§6.1 的既有设计） | **不需要**。装上就有，没装时逐字不变 |
| **P1 默认增强（可选依赖，推荐）** | **D：`model2vec` 静态嵌入**（多语言模型实测 **512.47 MB 落盘**，见 §14.3.5），只作 `w_embed` 信号 | 装 `embed-light` extra（**实为 24 个包**，含 `numpy`/`tokenizers`/`huggingface_hub`）；模型要预热一次 |
| **P1 可选（2 个依赖）** | **B/C：ONNX 运行时 + ONNX 模型**（用本机**已有**的 448.8 MB MiniLM，或自己导出小模型） | 装 `onnxruntime`（cp314 wheel 14.0 MB）+ 模型；**离线可行** |
| **永不进默认** | **E（torch 系）**、**A（llama-server 常驻依赖）** | —— |

**"用户不额外装东西也能用"落在 F 上，这是默认**；D 是**装了才启用**的增强。
沿用本仓 `.[socks]` 的可选依赖先例（`pyproject.toml`：`socks = ["httpx[socks]>=0.27"]`）——
**没装时行为逐字不变**。建议的 extra 形状（本节只是设计，不改代码）：

```
embed-onnx  = ["onnxruntime>=1.24", "numpy>=1.26"]   # 方案 B/C
embed-light = ["model2vec>=0.9", "numpy>=1.26"]      # 方案 D
```

> **本节的确定程度要说清楚**（已完成一次，2026-10-02 二轮）：
> **确定的**是——要不要嵌入（要）、**哪一类**嵌入（静态、无 torch、可回退、只作排序信号）、
> **具体用哪个模型**（**`minishlab/potion-multilingual-128M`**：§14.3.4 判据 A 达标，
> top1 **1.0000** ≥ 阈值 **0.5538**）、以及**用什么纪律用**（§14.4 七条）。
> **仍然不是"已实现"**：本节只是**选型定案 + 纪律**，**代码一行没改**；
> 且 M2' 的端到端护栏、常驻内存、B/C 路线**都还没测**（§14.3.7）。
> **`w_embed` 默认 0.0、行为等同方案 F**——"选定了模型"不等于"默认开启"。

#### 嵌入落在 P1 还是 P2

**结论：落 P1，但以"默认关闭的增强"形式落 P1。**

理由（三条，都是本轮的实测/已定事实）：

1. **P1 的验收指标恰恰是嵌入能帮上的那一条**（第 2 轮输入 token 降 ≥15%）。
   嵌入改善的是 `recall` 的**命中质量**，命中质量直接决定"能不能少塞"——
   放 P2 等于让 P1 在"没有检索信号"的条件下证明"记忆能省 token"，
   而这在本轮实测里已经被证明是**弱的**（§14.1.3：FTS5 在 `清理旧文件` 上 5 条里 3 条跑组）；
2. **P1 本来就要动排序**（`bm25` + 五维 + 负反馈）。把 `w_embed` 一次性做进同一个
   打分函数，比 P2 再回来改一遍排序**风险更小**——排序只改一次；
3. **代价被 §14.4 的纪律钉死了**：`w_embed` 默认 0.0、不新增上下文通道、不可用即回退且可见。
   也就是说**"嵌入进 P1"不会给 P1 引入新的预算风险**，只引入**一个可失败但可回退的初始化路径**。

#### P1 验收指标：保持既有，只加一条

**既有判据（不变）**：

> **长任务多轮：第二轮起可用 token 下降 ≥ 15%，而通过率不降**
> （通过数不低于基线 − 1 道，两次跑都要冷启动 llama-server，见 §10.1）

**新增一条（必须加，且要说明为什么值得加）**：

> **M2'（离线检索精度）：在 §14.1.3 那个 16 条语料 + 10 条查询的固定集合上，
> `recall` 的 precision@5 与 recall@5 都不低于纯 FTS5 基线。**
> 基线值就是 §14.1.3 表里的 FTS5 命中列表；`recall@5` 的"相关"标签用**人工分组**（G1–G4）。

**为什么这一条值得加**：

- §10 的 M1 是**端到端**指标，它**测不出嵌入是帮忙还是添乱**——
  如果嵌入把 `uv` 这类查询排错（§14.1.3 的失败案例），M1 的 token 数可能仍然是"降了"，
  但降的原因是"**少召回**"而不是"**少而准**"；
- M2' 是**离线、不起模型、不烧 GPU、单机可跑**的（与 §10.2 对 M2/M3 的定位一致），
  成本几乎为零，且**它正好覆盖本轮唯一有真实读数的那个数据集**；
- 它给 `w_embed` 一个**可回归的锚**：将来调 `w_embed` / `embed_min_cos` 都必须通过 M2'。
  **没有这条，`w_embed` 就成了一个没人敢动的魔数。**

**不加的**：不加"嵌入本身的延迟/内存 SLO"——P1 阶段记 `memory_decision` 即可，
大盘留到 §12 第 8 条说的 M1/M2/M3 口径稳定之后。

#### 明确**不做**的事

1. **不做常驻向量服务 / 向量数据库进程**。`memory.db` 在工作区里、进程内点积足够；
   多一个常驻进程会引入"库被两个进程写"（§12 第 7 条）。
   → **直接否决了方案 A 的产品化形态**；
2. **不让依赖替我们管理上下文**。嵌入只输出**排序分**，**任何情况下不得**让 `onnxruntime`/`model2vec`/
   向量库决定"塞多少条进上下文"；切块大小、N、K、超时**全部在 `limits` 里**（§14.4 规矩 3）；
3. **不默认下载大模型**。默认路径永不联网（§14.4 规矩 7）；
   首次下载必须 `embed setup` 显式确认；**448.8 MB 那种体积不可能是默认**（它只作为"本机已有、可选"）；
4. **不引入 torch 系**（方案 E 否决）；
5. **不把嵌入接进 `memory.md`**。热记忆仍是人类可读 Markdown（决议 6），
   嵌入向量只存库（存哪见 §14.6）；
6. **不做"每轮重新嵌入全库"**。向量写入时算一次、`content_hash` 变化时才算（§14.6）。

### 14.6 存储与生命周期（本轮一并定下，保持 P1 可落地）

- **向量存哪**：P1 **不建 `vec0` 虚表**（那要 `sqlite-vec` = 方案 B）。
  改为在 `semantic` 表**加列**（`ALTER TABLE` 加列是向后兼容的，沿用 §9.3 第 1 步的同一做法）：

  ```sql
  ALTER TABLE semantic ADD COLUMN embed_model TEXT NOT NULL DEFAULT '';  -- '' = 未嵌入
  ALTER TABLE semantic ADD COLUMN embed_dim   INTEGER NOT NULL DEFAULT 0;
  ALTER TABLE semantic ADD COLUMN embed_vec   BLOB;                       -- float32 小端，len = dim*4
  ```

  理由：**加列而不是新表**——向量是条目的属性，不是独立实体；
  且 P2 若真要上 `sqlite-vec`，从 `embed_vec` BLOB 迁到 `vec0` 是纯机械搬运。
- **何时算**：写入路径（§5.3 门禁通过后）算一次；`text`/`trigger` 变更时重算；
  **`embed_model` 与当前配置不一致的条目视为"未嵌入"**（换模型不会静默复用旧向量）。
- **未嵌入的条目怎么办**：**不丢**。检索时它们**只参与 `bm25` 那一项**（`w_embed` 项记 0），
  并在 `memory_decision` 里计入 `dropped` 的 reason。**绝不因为"没向量"而让一条记忆搜不到**——
  这是"嵌入是可信号，不是门槛"的兜底。
- **`memory doctor` 要检查的四件事**：① `memory.md` 与库是否一致（决议 6）；
  ② 有多少条 `embed_model=''`；③ 模型文件在哪、多大、能不能加载；④ 当前生效的
  `w_embed` / `embed_candidates_n` / `embed_rerank_k` 各是多少。

### 14.7 本轮没测到的（不许当结论用）

1. **没有实测任何 ONNX 路径**（方案 B/C）：本机无 `onnxruntime`（仓库 venv），
   **`sqlite-vec` 的实际检索延迟与 `vec0` 在 Windows 上的行为全部未测**。
   只测到"扩展加载**机制上**可行"（`enable_load_extension` OK、未编译 `OMIT_LOAD_EXTENSION`）。
   > **2026-10-02 二轮更正**：原文理由"网络不通装不上"**已过期**——代理已实测可用
   > （`pypi.org` HTTP 200，见 §14.3.5 第 3 条）。**但 B/C 本轮仍未跑**（任务只要测 D），
   > 所以这条"未测"的**结论不变，理由改了**：不是"装不上"，是"本轮没测"。
2. **[2026-10-02 二轮：本条已移出"未测"]** 原文为"没有实测 `model2vec`（首选方案 D）：
   包不在本机、网络不通，D 的所有质量数字只有厂商声明"。
   现已在 `.tmp\embed-venv` 里实测 `model2vec==0.9.0` + `potion-multilingual-128M`：
   **top1 1.0000、P@5 0.477、R@5 0.769、单查询 0.042 ms、模型落盘 512.47 MB**——
   全部在 **§14.3.2**。**仍未测的是**：它的**常驻内存**、**几十条以上语料的稳定性**、
   以及**符号级查询的稳定性**（§14.3.7）。
3. **没有实测 `fastembed`**，其默认模型名/体积/首次下载行为**未核实**。
4. **没有测过本机 CPU 的内存占用**：探针里的 RSS 读取用 `GetProcessMemoryInfo` 失败（返回 0），
   所以**模型常驻内存没有实测值**，§14.4 的内存只给了**算法上界**与向量本体的实测值。
5. **没有测 GPU 路径**：本机有 `torch 2.7.1+cu128` 且 `cuda.is_available()=True`（在
   `D:\workSpace\pyvideotrans\.venv`），但**仓库 venv 里没有 torch**，且 GPU 不是本设计的路径。
6. **没有跑 §10 的 50 题回归集**（与附录 B 第 1 条同一原因：要重启 8080 上的服务，本任务不许）。
   因此 **M1 的 15% 与新增 M2' 都仍是待验证预期，不是实测值**。
7. **没有核对 §13 各决议在代码里的落地代价**：本节不改代码，所以 P1/P2 的工期估计
   （尤其"加 3 列 + `embed_status` 列"对既有 `store.py` 的影响）未评估。

---

## 附录 A：取证复现方式

全部为只读操作。命令用仓库自带的 python（`load_workspace_dependencies` 报出的路径），
`PYTHONPATH` 指向 `D:\workSpace\agents_dev\src`。**不读取该工作区的 `.env`。**

1. **`memory.md` 内容与容量**
   `read_hot(memory.md)` → `render_hot` → `OfflineTokenCounter().count()`；
   `hot_budget(8192)`；逐条累加 token（结果：9 条、1011/1044）。
2. **`memory.db` 结构**
   `sqlite3.connect("file:...memory.db?mode=ro", uri=True)`；
   `sqlite_master`（表/索引/触发器清单）、`PRAGMA table_info`、
   各表行数（memory 0 / memory_fts 0 / episode 26 / transcript 53 / session 9）。
3. **FTS 覆盖与排序**
   在 `D:\workSpace\agents_dev\.tmp\memprobe\` 下的临时库里：
   查 `"fact"` → 0 条；查 `"正则"`（只在 trigger 里）→ 0 条；
   查 `"归档"` 与 `"归"` → 同一批；
   `touch` 一次后重排 → 用过的排前；
   连续 `recall` 四次 → `use_count` 4–5；
   `inspect.getsource(_rank)` 里不含 `confidence`/`bm25`/`kind`。
   （临时目录已删除，未触碰任何被测工作区的数据。）
   > **2026-10-02 修正**：其中"查 `"归档"` 与 `"归"` → 同一批"这一条
   > **在更大语料上不成立**（详见附录 A.7 与 §14.1.3）：16 条语料上
   > 查 `归档` → `[1,2]`、查 `归` → `[1]`，**并不相同**。
   > 当时的临时库语料极小，`bm25()` 在所有行上都返回 0，**读数没有分辨力**。
   > 定性结论（逐字 OR 把粒度拉到单字）仍然成立，但"结果相同"这句应弱化。
4. **DDL 原文**：`store.py:17-64`、`store.db.open_db`（`:52-58`）。
5. **llama-server 嵌入可用性（§14.1.1）**
   `POST http://127.0.0.1:8080/v1/embeddings`，body `{"input": "归档", "model": "x"}`，
   超时 20s，**只打 2 个请求**；另读 `/v1/models`。
   **全程只读：没有重启、没有停止、没有大批量。** 复现脚本：
   `D:\workSpace\agents_dev\.tmp\embedprobe\probe_llamaserver.py`。
6. **本机依赖清点（§14.1.2）**
   用仓库 venv 的 python 逐个 `importlib.util.find_spec`；另用
   `Get-ChildItem site-packages` 统计解包体积；用 `pypi.org/pypi/<pkg>/json` 取 wheel 事实。
   脚本：`.tmp\embedprobe\dep_inventory.py`。**不读该工作区的 `.env`。**
7. **FTS5 vs 嵌入对照实验（§14.1.3）**
   脚本 `.tmp\embedprobe\contrast_experiment.py`（16 条语料 / 10 条查询）
   与 `.tmp\embedprobe\fts_repro.py`（4 条语料的机制复核），
   以及 `.tmp\embedprobe\cost_measure.py`（延迟与体积）、`.tmp\embedprobe\sqlite_caps.py`
   （`sqlite3` 能力）。
   运行环境：**不是**仓库 venv，而是本机另一个已装 torch 的 venv
   （`D:\workSpace\pyvideotrans\.venv\Scripts\python.exe`，Python 3.10.19，
   `numpy 1.26.0` / `torch 2.7.1+cu128` / `transformers 4.57.6`），
   设 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`，
   模型取本机 HF 缓存 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`。
   **只读该环境、未安装任何包、未改动它。**
8. **嵌入选型的第二次测量（§14.3，2026-10-02 二轮）**
   全部脚本在 **`.tmp\embed-bench\`**（清单见 §14.3.6），语料与查询固定在
   `bench_common.py` 里。运行方式：
   - **装包**：`uv 0.10.8`（`C:\Users\32615\.pyenv\pyenv-win\shims\uv.bat`）建
     `.tmp\embed-venv`（CPython 3.14.3），`uv pip install model2vec numpy` →
     `model2vec 0.9.0` / `numpy 2.5.3`；`HTTP_PROXY`/`HTTPS_PROXY=http://127.0.0.1:7890`、
     `UV_CACHE_DIR` 指向 `.tmp\uv-cache`。**仓库 `.venv` 未被触碰（未装、未改）。**
   - **取模型**：`HF_ENDPOINT=https://hf-mirror.com`、`HF_HUB_DISABLE_XET=1`、
     `HF_HOME=.tmp\hf`，`download_m2v.py` 拉 `minishlab/potion-multilingual-128M`
     （`ignore_patterns=["onnx/*"]`，省掉 488.63 MB 的 onnx 副本）。
   - **FTS5 基线**：`run_fts5.py`（`normalize_for_fts`/`_match_query` 逐字抄自 `store.py:66-94`）。
   - **model2vec**：`run_embed.py model2vec`（用 `.tmp\embed-venv` 的 python）。
   - **MiniLM**：`run_embed.py minilm`（用 `pyenv 3.14.3`，`HF_HUB_OFFLINE=1`；
     另用 `check_appendix_a7_interp.py` 在 `pyvideotrans\.venv` 里以裸 `transformers` 复核）。
   - **汇总**：`report.py` → `REPORT.txt`；逐条原始读数在 `results_*.json`。
   **只读**了 `pyvideotrans\.venv` 与仓库 `.venv`（只运行解释器，未安装/未改动）；
   **未读取** `D:\workSpace\agentWorkSpace\.env`；**未改任何代码、未改任何其它文档**。

## 附录 B：本次未取证到的部分

1. **没有在真实工作区上跑过 50 题回归集**（那要重启 8080 上的服务，
   而 8080 正在被使用、且本任务不允许起长驻进程）。§10 的 M1 口径
   只有**基线数字**引用自 `docs/benchmarking.md`，**没有任何本轮实测**。
2. **没有实测"巩固"与"编辑距离去重"的效果**——它们是为本设计提出的新机制，
   效果需要 P1/P2 落地后用 M2/M3 量。
3. **没有核对 `web/` 的面板改动代价**：本设计只读了
   `web/server.py` 的 `WRITE_PATHS`/`do_GET`/`do_POST` 与 `web/runner.py:515-524`，
   没有读 `page.py` 的全部渲染逻辑，P2 的网页壳工作量估计可能有偏差。
4. **没有量过 `semantic` 表的检索性能上限**：`episode` 从 26 涨到 1000 条时的
   OR-LIKE 全表扫耗时没有实测；§7.1 的 1000 是一个**待标定**的初值。
5. **没有查 `weft` 仓的实际接口**：`roadmap.md:98` 说它已经跑到向量层，
   但本仓里 weft 代码 0 行，接口契约在 `D:\workSpace\weft\docs\SPOOLKIT.md`——
   本次没有读那个仓，所以"记忆与 weft 的关系"只按 `roadmap.md:159-165` 的口径写。
6. **没有复核 `memory.md` 里天气表那一条的确切来源 episode**：
   长度 401 与 `episode #19` 的 420 接近但不相等，未逐字符比对。

### 附录 B 续：本轮（2026-10-02，嵌入选型）新增的未取证项

7. **[2026-10-02 二轮更正]** 原文写"网络出口不通"（`status=000`、
   `SEC_E_NO_CREDENTIALS`、且 `7890` 无监听）。**这一条现在不成立**，实测：
   - `127.0.0.1:7890` **在听**（TCP 握手成功；`CONNECT pypi.org:443` → `HTTP/1.1 200 Connection established`）；
     `1080` **主动拒绝**；
   - 走 7890 时 `pypi.org/simple/...` → **HTTP 200（21 383 B）**，`hf-mirror.com` → **HTTP 200**；
     **`huggingface.co` 仍被重置**（`WinError 10054`）；
   - **Windows schannel 全线失败**（`Invoke-WebRequest`/`curl.exe` → `SEC_E_NO_CREDENTIALS`），
     所以那两个命令**不能用来判断网通不通**——要用 OpenSSL（Python）或 rustls（`uv`）。
   → 原文"`numpy`/`fastembed`/`sqlite-vec 0.1.9` 的精确体积没取到"**已可补**，但本轮没补。
8. **[2026-10-02 二轮更正]** 原文写"方案 B/C/D 一个都没跑起来……`model2vec` 的中文质量是空的"。
   **D 已经不空**：`.tmp\embed-venv` 里装了 `model2vec==0.9.0` + `numpy==2.5.3`（经 7890 从 PyPI 取），
   模型经 `hf-mirror` 取到 `potion-multilingual-128M`，全部读数在 **§14.3.2**。
   **B/C 仍然是空的**（`onnxruntime`/`sqlite-vec`/`fastembed` 本轮一个都没跑）。
   唯一真跑过的嵌入**不再只有 MiniLM**：现在有三个后端（FTS5 / model2vec / MiniLM）同集合可比。
9. **内存没有实测值**：`GetProcessMemoryInfo` 在探针里返回 0，
   所以 §14.4 只给了**算法上界**，没有"实际常驻多少 MB"的读数。
10. **没有核对 llama-server 的启动参数**：只从 501 错误文本得知缺 `--embeddings`，
    **没有读它的命令行**（读进程命令行属于对用户运行环境的额外侵入，本轮没做）。
11. **没有验证 `sqlite-vec` 与"external content FTS5 + 触发器"共存**：
    只验证到"本机 `sqlite3` 允许 `enable_load_extension`"（`sqlite_caps.py`），
    **没有真的加载过 `vec0`**。
