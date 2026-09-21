# 循环监测补强 + 任务记忆补全 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把督导从「读 40 行 trace 猜有没有进展」补成「机械层先报矛盾/短环，再叫醒督导」；同时把任务结论写进可检索记忆，让隔几轮再问「刚才说了什么」能召回。

**Architecture:** 监测与记忆是两条独立轨，互不阻塞。监测只产可检验计数，写入已有 `Evidence`/`asking`，**不直接 stop**（停不停仍是督导 + 天花板）。记忆只完善「任务残渣」三层（热 / 冷 / episode），**不把长文小说灌进 `memory.md`**。10M 语料走网页同款切块通道，单独立项，本计划不做。

**Tech Stack:** 现有 Python 包、SQLite FTS5、督导 JSON schema。不新增向量库、不接 embedding、不改 llama.cpp 请求字段（logprobs 本轮不做）。

## Global Constraints

- 不新增第三方依赖。
- 机械层只报可检验信号；语义判断仍归督导。含糊续期比不续期危险：监测失败不得改成自动续期。
- 标识符英文，注释与面向人的文案中文。
- 每个任务自带 pytest；改完在 `docs/dogfooding.md` 记一条（本计划落地后）。
- 数量上限进 `limits.KNOBS`，代码里只留默认并注明按 27B / 8K 标定。
- **测试在沙箱内单独跑，git 提交在沙箱外单独执行，两者绝不合并成一条命令。** 未明确要求提交则不要 commit。

---

## 两轮交流收束（为什么这么拆）

### Soft loop 三层 vs 督导现状

| 层 | 督导现在 | 缺口 |
|---|---|---|
| 目标无界 | 用进展换续期 + `ceiling` | 换参仍在动时，督导只能读 trace |
| 盲点共享 | 不看主循环推理 | 同一 `gateway`，能力盲点仍共享 |
| 幻觉耦合 | 工具成败/验证是真信号 | `write_file` 成功会被读成进展 |

四条工程监测的取舍：

| 方案 | 本计划 | 理由 |
|---|---|---|
| #4 动作矛盾（写了又覆盖、同关键词再搜） | **做** | 循环已看得见，补幻觉耦合最便宜 |
| #2 瘦身：工具名短环 `A→B→A→B` | **做** | 主循环没有执行树；名字环能抓 `write_file → run_command` 换参回路 |
| #1 窗口余弦相似度 | **不做**（可后补廉价字符串相似） | embedding 新模型；schema 约束下整段 JSON 假阳性高 |
| #3 logprobs 崩塌 | **不做** | `ChatResponse` 无 logprobs；温度 0 + GBNF 让概率脏；与 70%/90% 预算守卫抢职责 |

### 记忆 vs 10M 小说

当前记忆是**任务残渣**（distill 条目、教训、episode），不是知识库。小说应走 `fetch/store` 那种「切块 + 目录 + 按需取一块」。本计划只修残渣链路：

- T6-A：`episode.summary` 带截断后的 `final`
- T6-B 弱化版：distill 空且 `final` 非空 → 兜底一条低置信 fact
- T6-C（transcript 进 `recall`）**不做**
- FTS 整句短语过严 → 改为分词 OR
- 向量库、热记忆加比例硬塞长文：**不做**

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/spoolkit/agent/monitor.py` | **新建**。纯函数：工具名短环、查询归一化、同路径内容振荡。loop 只调用，不把计数逻辑再堆进 `loop.py` |
| `src/spoolkit/agent/loop.py` | 每步把 `(name, args, ok)` 记入窗口；越阈时设置 `asking` 并叫督导 |
| `src/spoolkit/agents/supervisor.py` | `Evidence` 增加矛盾/短环字段，`render()` 带给督导 |
| `src/spoolkit/context/templates.py` | `SUPERVISE` 提示词增加两条机械统计的读法 |
| `src/spoolkit/limits.py` | `name_cycle_window` / `name_cycle_at` / `oscillation_at` / `episode_final_chars` |
| `src/spoolkit/memory/archive.py` | `summarize` 接收 `final`；写入 episode |
| `src/spoolkit/memory/session.py` | `archive(..., final=)` 传下去 |
| `src/spoolkit/memory/distill.py` | 空条目兜底 |
| `src/spoolkit/memory/store.py` | FTS 查询从短语改为 OR；episode 检索仍 LIKE（本轮不建 episode_fts，避免迁移） |
| `tests/agent/test_monitor.py` | 监测纯函数 |
| `tests/agent/test_supervise_loop.py` | 越阈会叫督导（若现有夹具允许；否则只测 Evidence.render） |
| `tests/memory/test_archive.py` | summary 含 final |
| `tests/memory/test_distill.py` | 空兜底 |
| `tests/memory/test_store.py` | OR 召回 |

---

### Task 1: 监测纯函数（工具名短环 + 动作矛盾）

**Files:**
- Create: `src/spoolkit/agent/monitor.py`
- Test: `tests/agent/test_monitor.py`

**Interfaces:**
- Consumes: 无（不要 import `loop`）
- Produces:
  - `ToolSight(name: str, path: str = "", query: str = "", content_hash: str = "", ok: bool = True)`
  - `normalize_query(text: str) -> str`：小写、去空白、中英文标点剥掉
  - `name_cycle(names: Sequence[str], window: int = 12) -> str`：若最近窗口里存在周期 2 或 3 的名字环且重复 ≥2，返回 `"write_file→run_command"`，否则 `""`
  - `path_oscillations(sights: Sequence[ToolSight]) -> int`：同一 `path` 上 `content_hash` 出现 A→B→A（至少来回一次）的次数
  - `same_query_count(sights: Sequence[ToolSight]) -> int`：归一化后 query 非空且同一 query 出现次数的最大值
  - `MonitorReport(cycle: str, oscillations: int, same_query: int)`
  - `inspect(sights: Sequence[ToolSight], *, window: int = 12) -> MonitorReport`

- [ ] **Step 1: 写失败测试**

```python
# tests/agent/test_monitor.py
from spoolkit.agent.monitor import (
    ToolSight,
    inspect,
    name_cycle,
    normalize_query,
    path_oscillations,
    same_query_count,
)


def test_工具名短环_write_再_check() -> None:
    names = [
        "write_file", "run_command",
        "write_file", "run_command",
        "write_file", "run_command",
    ]
    assert name_cycle(names) == "write_file→run_command"


def test_参数不同但名字成环仍能抓到() -> None:
    """dogfood：node --check 的文件名每次都变，完整 signature 抓不到。"""
    names = ["write_file", "run_command", "write_file", "run_command"]
    assert "write_file" in name_cycle(names)


def test_正常推进不成环() -> None:
    assert name_cycle(["read_file", "write_file", "run_command"]) == ""


def test_同路径内容来回覆盖算振荡() -> None:
    sights = [
        ToolSight("write_file", path="index.html", content_hash="aaa"),
        ToolSight("write_file", path="index.html", content_hash="bbb"),
        ToolSight("write_file", path="index.html", content_hash="aaa"),
    ]
    assert path_oscillations(sights) >= 1


def test_同关键词再搜() -> None:
    sights = [
        ToolSight("search_code", query="贪吃蛇 键盘"),
        ToolSight("search_code", query="贪吃蛇   键盘"),
        ToolSight("search_code", query="贪吃蛇 键盘"),
    ]
    assert same_query_count(sights) >= 3
    assert normalize_query("贪吃蛇   键盘") == normalize_query("贪吃蛇 键盘")


def test_inspect汇总() -> None:
    sights = [
        ToolSight("write_file", path="a.html", content_hash="1"),
        ToolSight("run_command", query="node --check x.js"),
        ToolSight("write_file", path="a.html", content_hash="2"),
        ToolSight("run_command", query="node --check y.js"),
    ]
    report = inspect(sights)
    assert report.cycle
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_monitor.py -q`

Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `monitor.py`**

要点：
- `name_cycle`：只看 `names[-window:]`。对 period in (2, 3)，若 `len >= period * 2` 且 `seq[-period*2:-period] == seq[-period:]`，返回 `"→".join(seq[-period:])`。
- `normalize_query`：`casefold()`，`re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)` 对中文会把字留下来（`\W` 不含 CJK）。不要把汉字删掉。
- `path_oscillations`：按 path 收集 hash 序列，若存在 `h[i]==h[i+2] and h[i]!=h[i+1]` 计一次。
- `content_hash` 由调用方传入（loop 里对 write/replace 的 `new_text` 或 args 做 `hashlib.sha1`）；纯函数不读盘。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_monitor.py -q`

Expected: PASS

---

### Task 2: 计数旋钮 + Evidence 带上监测报告

**Files:**
- Modify: `src/spoolkit/limits.py`（KNOBS 列表末尾、`supervisor_max_grant` 附近）
- Modify: `src/spoolkit/agents/supervisor.py`（`Evidence` 与 `render`）
- Modify: `src/spoolkit/context/templates.py`（`SUPERVISE` 机械统计段）
- Test: `tests/agents/test_supervisor.py`
- Test: `tests/test_templates.py`（断言提示词含「短环」或「振荡」）

**Interfaces:**
- Consumes: Task 1 的 `MonitorReport` 字段名（cycle / oscillations / same_query）
- Produces:
  - knobs 默认：`name_cycle_window=12`，`name_cycle_at=1`（检测到环即升级），`oscillation_at=1`，`same_query_at=3`
  - `Evidence.cycle: str = ""`，`Evidence.oscillations: int = 0`，`Evidence.same_query: int = 0`
  - `render()` 增加这三键
  - `SUPERVISE` 正文增加：短环/振荡/重复查询是**机械层已经判定的打转**，优先 `redirect` 或 `stop`，不要因为又看到 `write_file` 成功就 `extend`

- [ ] **Step 1: 写失败测试**

```python
def test_证据带监测字段() -> None:
    evidence = _evidence(cycle="write_file→run_command", oscillations=1, same_query=3)
    rendered = evidence.render()
    assert rendered["cycle"] == "write_file→run_command"
    assert rendered["oscillations"] == 1
    assert rendered["same_query"] == 3


def test_督导模板要求读短环() -> None:
    from spoolkit.context import templates as T
    assert "短环" in T.SUPERVISE or "cycle" in T.SUPERVISE
```

`_evidence()` 已有，给 `Evidence` 加字段时要带默认值，旧测试不能碎。

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 改 `Evidence`、`render`、`SUPERVISE`、三个 knob**
- [ ] **Step 4: 跑 `tests/agents/test_supervisor.py tests/test_templates.py -q` 确认通过**

---

### Task 3: 主循环接入监测并叫醒督导

**Files:**
- Modify: `src/spoolkit/agent/loop.py`（工具调用循环内、叫督导的条件）
- Test: `tests/agent/test_supervise_loop.py`（已有督导触发夹具则扩一条；没有则写最小假模型脚本：连续 write_file/run_command 四次应出现 `叫督导` 且 asking 含「短环」）

**Interfaces:**
- Consumes: `inspect`、`ToolSight`、`config.limit("name_cycle_window"|"name_cycle_at"|"oscillation_at"|"same_query_at")`
- Produces: 新触发：`stuck_on_cycle` / `stuck_on_oscillation` / `stuck_on_query`；`asking` 文案示例：
  - `"工具路径在 write_file→run_command 打转（参数每次都不同，精确指纹抓不到）"`
  - `"同一文件内容来回覆盖 1 次，净效应可能为零"`
  - `"同一检索词已经查了 3 次"`

实现要点（写进 loop，不要另起循环）：

1. `sights: list[ToolSight] = []` 与 `repeats` 并列，跨步累计，成功完成任务时随函数结束丢弃。
2. 每次工具调用后 append。`path` 从 `arguments.get("path")` 取；`query` 从 `query`/`goal`/`pattern` 里第一个字符串取；`content_hash` 仅对 `write_file`/`replace_text`/`replace_lines`：对 `content` 或 `new`/`new_text` 做 `hashlib.sha1(text.encode()).hexdigest()[:12]`，没有内容则空。
3. 叫督导的 `if` 与现有 `stuck_on_repeat`/`stuck_on_roam` **或** 起来。门槛翻倍逻辑对短环同样适用：问过一次把 `name_cycle_at` 本地变量加倍，避免每步都问。
4. `_ask_supervisor(...)` 增加把 `cycle/oscillations/same_query` 传入 `Evidence`。

- [ ] **Step 1: 读 `tests/agent/test_supervise_loop.py`，按现有假模型写法加一条「名字环触发督导」**
- [ ] **Step 2: 先跑新测试，确认失败**
- [ ] **Step 3: 改 loop**
- [ ] **Step 4: 跑 `tests/agent/test_supervise_loop.py tests/agent/test_monitor.py -q` 通过**
- [ ] **Step 5: 跑全套里与 loop/supervisor 相关的既有测试，确认 repeats/漫游旧行为不碎**

---

### Task 4: T6-A episode.summary 带任务结论

**Files:**
- Modify: `src/spoolkit/memory/archive.py`（`summarize`、`archive_task`）
- Modify: `src/spoolkit/memory/session.py`（`archive` 增加 `final: str = ""`）
- Modify: `src/spoolkit/agent/loop.py`（`_archive` 把 `final` 传给 `memory.archive`）
- Test: `tests/memory/test_archive.py`

**Interfaces:**
- Consumes: `limits.knob("episode_final_chars")` 默认 `400`
- Produces:
  - `summarize(state, final: str = "", max_final_chars: int = 400) -> str`
  - 格式：`"{goal}：已完成 {steps}\n结论：{clipped_final}"`；`final` 空白则不加「结论」行（旧行为兼容）
  - `archive_task(..., final: str = "")` → `summarize(state, final=final, ...)`

- [ ] **Step 1: 写失败测试**

```python
def test_归档摘要带上结论(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
        final="厦门近两天约 28°C。",
    )
    row = conn.execute("SELECT summary FROM episode").fetchone()
    assert "28°C" in row["summary"]
    assert "给 parser 加增量更新" in row["summary"]


def test_没有结论时摘要不含结论行(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    archive_task(
        conn,
        tmp_path / "memory.md",
        _state(),
        session_id="s1",
        counter=OfflineTokenCounter(),
        context_window=8000,
    )
    row = conn.execute("SELECT summary FROM episode").fetchone()
    assert "结论：" not in row["summary"]
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 改 summarize / archive_task / session.archive / loop._archive**
- [ ] **Step 4: 跑 `tests/memory/test_archive.py -q` 通过**

验收语义：`recall("28")` 仍可能因 LIKE/FTS 对不上（下一任务才放宽）；本任务只保证 **summary 文本里有结论**。

---

### Task 5: distill 空条目时兜底一条 fact

**Files:**
- Modify: `src/spoolkit/memory/distill.py`（`distill` 返回前）
- Test: `tests/memory/test_distill.py`（已有则扩；没有则新建。用假 gateway 返回 `{"entries":[]}`）

**Interfaces:**
- Consumes: Task 4 的 `episode_final_chars` 或 distill 内部常量 `FLOOR_CHARS = 400`
- Produces: 若 `entries` 为空且 `final.strip()` 非空，追加 `("fact", clipped_final)`。`DistillResult.raw` 可保持模型原文，便于对照「模型空、系统兜底」。

不要改 `DISTILL` 提示词强迫模型「必须产一条」——那会污染归纳质量。兜底发生在解析之后、归档之前。

- [ ] **Step 1: 写失败测试**：假模型返回空 entries，`distill(..., final="得分规则是每吃一个 +10")`，结果 entries 长度为 1 且 text 含 `+10`
- [ ] **Step 2: 空 final 则仍为空列表**（骨架步骤无结论不该造记忆）
- [ ] **Step 3: 实现兜底**
- [ ] **Step 4: 跑 distill 相关测试通过**

---

### Task 6: FTS 召回从整句短语改为分词 OR

**Files:**
- Modify: `src/spoolkit/memory/store.py`（`_phrase_query` 改名或旁路 `_match_query`）
- Test: `tests/memory/test_store.py`

**Interfaces:**
- Consumes: 现有 `normalize_for_fts`
- Produces:
  - `_match_query(query: str) -> str`：token 用空格切开后，长度 ≥1 的 token 用 ` OR ` 连接；单个 token 仍可短语。空 query 返回 `""`（调用方已处理）
  - `search_memories` 改用 `_match_query`
  - `search_episodes`：**本任务也改**，对 `task`/`summary` 用多个 `AND (task LIKE ? OR summary LIKE ?)` 会组合爆炸。更简单：把 query 按空白和 CJK 连续串切成 token，**每个 token 一条 LIKE**，行要命中**至少一个** token（OR）。token 短于 1 个字丢弃；中文单字允许（「度」「分」可能噪，先做、用测试钉住「28」和「厦门」能中）

旧行为「整句必须按字序出现」会让「刚才厦门多少度」打不中「厦门近两天约 28°C」。OR 之后可能多召回，`_rank` 仍按使用频率——可接受。不要上向量。

- [ ] **Step 1: 写失败测试**

```python
def test_中文部分词也能召回() -> None:
    conn = _conn(tmp_path)  # 测试函数要带 tmp_path
    add_memory(conn, kind="fact", text="厦门近两天约 28°C")
    hits = search_memories(conn, "刚才厦门多少度")
    assert any("28" in item.text for item in hits)


def test_事件摘要能按结论数字搜到(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    add_episode(conn, "s1", "查天气", "查天气：已完成 无记录\n结论：厦门近两天约 28°C。", "success")
    rows = search_episodes(conn, "厦门 温度")
    assert rows
    assert "28" in rows[0]["summary"]
```

（`search_episodes` 第二个查询若仍整句 LIKE `%厦门 温度%` 会失败，必须改成 OR。）

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 改 store.py**
- [ ] **Step 4: 跑 `tests/memory/test_store.py tests/memory/test_archive.py -q` 通过**

---

### Task 7: 文档回写

**Files:**
- Modify: `docs/manual.md` 记忆一节：补「任务结论会进 episode；recall 是关键词 OR；长文不进热记忆」
- Modify: `docs/dogfooding.md` 追加计划出处（落地后补实测；本任务只写「计划已落盘」一句亦可，避免假装已经量过）
  （内部交接备忘只写在开发机本地，不进仓库）

- [ ] **Step 1: 改文档，T6 待定方案删掉「先不实现」**
- [ ] **Step 2: T8 写明：10M 小说复用 `fetch/store` 切块，不进本记忆计划**

---

## 明确不做（防止执行时膨胀）

- 余弦 embedding / 新 embedding 模型
- llama.cpp `logprobs`
- 完整执行状态机
- `recall` 扫全部 transcript
- 向量数据库
- 本地小说/手册切块工具（T8，需另开计划：目录 + FTS 扫块 + 按需 read，镜像 `web_read`）
- 把热记忆比例加大来「记住」长文

## 建议落地顺序

```
Task 1 → 2 → 3     （监测轨，可单独合并）
Task 4 → 5 → 6     （记忆轨，可单独合并）
Task 7             （两轨都完成后再写文档）
```

两轨无代码依赖，可并行；不要在监测未完成时把「督导提示词」和记忆混在一个 diff 里，review 会糊。

## 验收（计划级，不是单测）

- 监测：用假模型复现 `write_file` / `run_command` 换参四拍，trace 出现 `叫督导` 且 asking 含短环；精确指纹那条旧测试仍过。
- 记忆：归档带「28°C」的 final → `search_episodes("厦门")` 能命中；`distill` 空 + 有 final → 至少一条 fact；无 final → 仍可零条。
- 全套 pytest 绿（允许已知 service flaky 重跑）。

---

## 执行交接

Plan 写在 `docs/superpowers/plans/2026-09-17-monitor-and-memory.md`。

**1. Subagent-Driven（推荐）** — 每任务一个新子代理，任务间审查

**2. Inline Execution** — 本会话按 executing-plans 逐任务做，中间设检查点

选哪一种？
