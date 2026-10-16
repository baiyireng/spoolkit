# 交接：接下来要做的（2026-10-16）

给接手的人/agent（Cursor）看的一页。**先读这一页，再动手**；细节都在指到的文件里。

## 0. 三十秒现状

- 仓库是纯 Python（`requires-python >= 3.12`，本机 venv 是 3.14.3），CLI 叫 `spool`。
- **关口已经建成并且是绿的**：

  ```bash
  uv sync --extra dev          # 装开发依赖（含 mypy，版本锁在 uv.lock）
  python scripts/gate.py             # 全套：环境体检 + 测试 + 类型检查
  python scripts/gate.py --only doctor   # 只体检（**受限环境下先跑它**）
  ```

  最近一次实测：`3128 passed, 5 skipped`，类型检查 `161/161`（基线内、无新增），关口通过。
- 5 条 skipped = 4 条缺 `process_kill` 能力（受限沙箱杀不了进程）+ 1 条平台不支持符号链接。
  **它们带着原因跳过，不是没跑**（`-rs` 摘要里能看见）。

## 1. 先做这一步：环境体检（别跳过）

```bash
python scripts/gate.py --only doctor
```

这张表回答三件事：**什么可用 / 算不算受限 / 不可用时该怎么办**（每条不可用项下面直接
印着"受限下这么做"）。本机（DSH 受限会话）的典型结果：

| 能力 | 状态 | 对策（表里会印） |
|---|---|---|
| 写 `.venv` | 不可用 | `uv pip install --target .tmp/tools-<名字>`；要装项目依赖时切一次完全权限跑 `uv sync` |
| 子进程改「工具层文件」 | **部分不可用**（`events.py`/`settings.py`/`test_secrets.py` 等） | 用 `edit` 工具改；**别再重试** `git checkout/stash`、`Copy-Item`、Python `write_bytes` |
| node 子进程管道 | 不可用（`ERR EPERM`） | 不跑 `vite build` / `vitest run`；类型检查走 `vue-tsc` |
| 按进程树杀进程 | 不可用 | 用例打 `@pytest.mark.needs("process_kill")` 跳过 |

**踩坑代价**：这三条各自让上一轮白花过时间。表里有答案就别再试错。

## 2. 必须守住的规矩（违反它们就是引入退化）

1. **不放宽判据、不放宽上限**。护栏/触发条件/`_REFRESH_LIMIT` 这类阈值一个都不许为了
   让某次绿而调松；要改必须说明理由并单独提交。
2. **话术必须说真话**。句子断言了事实（"自动修 N 轮后"、"非代码的测试数据"），事实不成立
   就改句子——不能改成含糊话。
3. **测试要能证伪**。每条 `git diff` 里的约束，都要有一次"故意做错 → 该用例变红"的变异自检
   （做法：临时改源码 → 跑目标用例 → 期望失败 → 还原；本会话用过三次）。
4. **不要改测试来凑绿**。唯一例外是"黄金表确实落后于实现"（例如
   `tests/test_prompt_mechanics.py` 的 `FROZEN_PLACEHOLDERS` 少了 `tools`），且必须写明理由。
5. **CLI 用户可见文案是英文**（在途批次的决定，见
   `docs/superpowers/plans/2026-10-13-cli-rich-term.md:18`）；**注释与文档是中文**。
6. **不要提交**，除非用户明确要求。工作树里有 90+ 项未提交改动，其中一大批是**别人的在途
   CLI 批次**（Rich 配色 + 英文文案），不是本会话的。
7. `.ps1` 文件**没有 BOM**：里面写中文会被 Windows PowerShell 5.1 按 ANSI 解码、吃掉引号导致
   解析失败。壳脚本保持 ASCII-only，中文解释放 `.py` 里。

## 3. 本会话已经做完的（别重做）

| 做了什么 | 位置 | 证据 |
|---|---|---|
| `EventWriter.emit` 加位置限定 `/` + `emit_note`：`emit(NOTE, kind=...)` 不再 `TypeError` | `src/spoolkit/cli/events.py` | `tests/cli/test_events.py`（2 条新用例，变异自检过） |
| `pending_actions_note` 按**会话**找清单（原先非默认会话静默丢这句话） | `src/spoolkit/cli/settle.py`、`cli/commands/run.py` | `tests/cli/test_pending_actions_note.py`（4 条） |
| 13 条文案断言改成英文（secrets/settings/rename_compat） | `tests/test_secrets.py` 等 | 全绿 |
| 测试不再读开发机代理（`test_fetch` 的假红来自 `HTTP_PROXY`） | `tests/conftest.py` 的隔离夹具 | `tests/test_fetch.py` 全绿 |
| 黄金表补 `tools` | `tests/test_prompt_mechanics.py` | 该用例绿 |
| 关口 + 体检 + needs 标记 + 豁免清单 | `scripts/gate.py`、`dev_env.py`、`pytest_needs.py`、`tools/known-env-failures.json`、`tools/mypy-baseline.json` | 见第 0 节 |
| 探针 `vue3-engineering`（seed + 手写 expected + 机械判据 + 清单条目） | `benchmarks/probes/**` | `tests/test_probe_runner.py` 通过 |

## 4. 接下来要做的（按优先级）

### A. 信号身份层：`StrEnum` + 注册表 + 穷尽断言 ← **建议先做这个**

**为什么**：本会话实测抓到一类反复出现的真缺陷——**信号的身份对不上**。
最典型的一条（已确认、可复现）：`MECHANICAL_KINDS` 里放的是 **note kind**（`"tests_weakened"`），
而账本里 finding 的 kind 是 `test_data_changed`/`assert_loosened`…，于是
**收尾重判忘不掉"测试被改弱"这一族**，旧判词会留给 T12 去拦完成声明。

**做法**：

1. 新建 `src/spoolkit/agent/signals.py`：
   - `class FindingKind(StrEnum)` / `class NoteKind(StrEnum)`（**两个不同的枚举**，这是关键）；
   - `Signal(family, audience, meaning)` 注册表，`audience ∈ {machine, model, human}`；
   - 导入期自检：缺 `family/audience/meaning` 即抛。
2. 消费集合全部**派生**：`MECHANICAL_KINDS = frozenset(k for k, s in SIGNALS.items() if s.family is MECHANICAL)`
   （`_WEAKENED_KINDS`、`BEHAVIOR_KINDS`、`L3_KINDS`、`_NOTE_KIND_BY_KIND` 同理）。
3. 收紧类型：`Finding.kind: FindingKind`、`note_kind(k: FindingKind) -> NoteKind`、
   `forget_kind(frozenset[FindingKind])`。`audit.py` 里 15 处 `add("...")` 字面量改成枚举成员。
4. 穷尽断言（`tests/agent/test_signals.py`）：`audience=machine` 的信号必须在
   `scripts/` / `web/` / `tests/` 里至少被读一次，否则进显式 `PENDING_CONSUMERS` 名单
   （名单本身也要校验）。参考现成范式：`tests/test_limits.py::test_表里列的每一项都真的接上了`。

**判据（做完的标志）**：① 上面那条 D1 缺陷消失（补一条"中途报 `test_removed` → 终态删掉 →
重判后账本里不许再有它"的用例）；② `forget_kind(NoteKind.X)` 被 mypy 判 `[arg-type]`；
③ 变异自检：新增一个 `Signal` 不接消费者 → 穷尽断言红。

### B. 类型检查阶段 1/2

- `limits.require_knob(name)`：消掉 `Knob | None` 那一族（~16 条），顺便让旋钮名写错时报人话；
- `[[tool.mypy.overrides]]` 按包开严，从 `spoolkit.agent.*` 起；
- **刷新基线（`--update-baseline`）必须单独提交**，diff 里要看得出承认了哪批债务。

### C. 审查层/探针的遗留缺陷（逐条都可证伪）

| # | 症状 | 位置 | 修法要点 |
|---|---|---|---|
| C1 | 新建的非 `.py` 测试文件被判成"测试数据被改"（`tests/unit/x.spec.ts` 中招） | `src/spoolkit/agent/audit.py:812` | 新建文件跳过 weakening；话术按事实（`.spec.ts` 是代码，不是"非代码 golden"） |
| C2 | 探针 `review_findings` 把 T2 **披露**算成发现（真发现 0 时读成 3） | `cli/commands/run.py:1879-1889` 用 `AUDIT_KIND`；`scripts/probe.py:244-249` | T2 披露换独立 kind，`REVIEW_NOTE_KINDS` 只留真发现 |
| C3 | `escalation_precision` 名不副实（本轮升级无任何高严重度发现却读成 1.0） | `scripts/probe.py:1546-1560` | 改名 `trigger_named_rate`，或按"有无高严重度发现支撑"派生 |
| C4 | `TaskState.verify`（"待验证"）**写了没人读** | `src/spoolkit/agent/state.py` | 要么收尾逐条结算，要么在 A 的注册表里显式标 `audience=model` |
| C5 | `episode.outcome` 在验收红时仍写 `success`（记忆层说谎） | `cli/...`：`loop.py:3476` 先写死 `"success"`，`3488` 才算 `finished` | 单一出口 `outcome_for(finished, review_blocked, verify)`；不变量测试："验收红 → 不许是 success" |
| C6 | `_refresh_reason` 死代码（无调用者） | `src/spoolkit/agent/loop.py` | 删掉，或接上调用点并说明 |

> C1–C3 是"真机探针跑出来的现象"，本会话只论证未修；每条都要配一条能红的用例。

### D. 真机探针复跑（可选，需本机 llama.cpp 在 127.0.0.1:8080）

```bash
python scripts/probe.py --probe vue3-engineering --max-steps 24 --step-ceiling 48 --timeout 2400
```

- 单探针跑会**覆盖** `benchmarks/probes/summary.json`（批次汇总）：**先备份、跑完还原**；
- `--no-save` 可完全不落盘；记录落在 `benchmarks/probes/records/`，台账追加在 `results.jsonl`；
- 工具链复验（要网络）：`python benchmarks/probes/checks/vue3_engineering.py --root <沙箱> --with-toolchain`
  （装依赖 + `vue-tsc --noEmit`；`vite build`/`vitest` 在受限会话里跑不了，如实标注）。

### E. 提交策略（等用户拍板）

工作树 90+ 项未提交，至少包含三批：**在途 CLI 批次（英文文案 + Rich 配色）**、
**本会话的关口/体检/探针/修复**、以及更早的 D3/D4b/D6 改动。
建议按主题分批提交（在途批次由它自己的作者提交），**不要一次性全提**。

## 5. 验收命令速查

```bash
python scripts/gate.py                     # 全套（推荐，唯一入口）
python scripts/gate.py --only doctor       # 体检
python scripts/gate.py --only tests        # 测试
python scripts/gate.py --only types        # 类型（带基线棘轮）
python scripts/pytest.ps1 tests/agent -q   # Windows 习惯入口（薄壳，环境准备同源）
```

**类型检查在受限会话里的前置**：`.venv` 不可写时 mypy 装在 `.tmp/tools-mypy`
（`uv pip install --target ...`），跑关口前 `$env:PYTHONPATH='.tmp\tools-mypy'`；
正常机器上 `uv sync --extra dev` 之后不需要这一步。

## 6. 别做的事

- 别为了让某条绿而放宽判据/上限/触发条件；
- 别用 `git checkout/stash` 去还原"工具层文件"（体检表会列出哪些不可写）——用 `edit`；
- 别在受限会话里跑 `vite build` / `vitest run`（`ERR EPERM`，是边界不是 bug）；
- 别把 `needs` 跳过的用例当成"没跑也没事"：它们**在正常环境里必须绿**，
  `tools/known-env-failures.json` 的条目过期会直接让关口红；
- 别在 `.ps1` 里写中文（无 BOM → PS 5.1 解析炸）。
