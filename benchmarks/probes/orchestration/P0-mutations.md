# P0 修法与变异的对照记录

这份是**可证的现场**：每一条修法都配一条「把它改回去 → 跑 → 哪条红 → 用 edit 还原」。
变异一律用 `edit` 工具改、再 `edit` 还原（**没有**用 `git checkout --`）。

跑法（受限沙箱下必须带插件与工作区外的临时根，见 `scripts/pytest.ps1`）：

```powershell
$env:PYTHONPATH = "<repo>\src;<repo>\scripts"
$env:PYTEST_ADDOPTS = "-p pytest_sandbox_compat -p pytest_hang_guard"
$env:PYTEST_DEBUG_TEMPROOT = "<仓库外、新建的空目录>"
# 本机回环被系统代理劫持（HTTP_PROXY=http://127.0.0.1:7890 + NO_PROXY 含 [::1]）
# 会让 httpx 抛 `InvalidURL: Invalid port: ':1]'`，13 条桩用例因此红——那不是回归。
Remove-Item Env:HTTP_PROXY, Env:HTTPS_PROXY, Env:NO_PROXY -ErrorAction SilentlyContinue
python -m pytest -q -p no:cacheprovider
```

| # | 变异（改哪里 → 改成什么） | 期望红的那一条 | 实测 |
| --- | --- | --- | --- |
| M1 | `agent/loop.py` 放大处：`grown = clamp_step_limit(...)` → `grown = wanted`（去掉硬上限夹取） | `tests/agent/test_repeat_guard.py::test_步数预算不放大的时候硬上限就是配置的值` | **红**：`AssertionError: 配置 10 步却跑了 12 步 / assert 12 <= 10` |
| M2 | `agent/loop.py` 收束注入：`if converge_at > 0 and state.step >= converge_at:` → `if False and ...` | `tests/agent/test_converge_budget.py::test_临近预算时收束指令进了模型上下文` | **红**：`收束那一步没有收束指令` |
| M3 | `agent/loop.py` 的 `model_verdict_line` → 直接 `return str(self.final or "")`（系统判词又当成结论） | `tests/agent/test_converge_budget.py::test_素材够了却一直不写结论_收束后产出结论` | **红**：`assert '【模型结论】' in '结论：只有一处赋值，不需要改动。'` |
| M4 | `agent/loop.py` 模型调用异常处理：`if not _is_model_call_failure(exc): raise` → 直接 `raise`（LLM/分词失败改回静默） | `tests/agent/test_run_failure_loud.py::test_分词失败是显式失败而不是未完成`、`::test_中途那一步坏了同样记失败` | **红 2 条**：`LlamaCppError: 服务返回 502：（没有说明）` 直接穿出 `run()`，读数里没有失败这一档 |
| M5 | `limits.py` 的 `converge_at_steps` 默认值 `2` → `0`（默认关掉收束） | `tests/agent/test_converge_budget.py::test_临近预算时收束指令进了模型上下文` + `tests/test_limits.py::test_p1_旋钮全部登记且可覆盖` | **红 2 条**：默认值不是 2；收束指令不再出现 |
| M6 | `limits.py` 的 `hard_trigger_ratio` 默认值 `0.98` → `0.90`（把「早重置」那条口径搬回来） | `tests/cli/test_prompt_for_turn.py::test_run里不给session时只查当前会话`、`::test_run里显式session_星号才跨会话` | **红 2 条**：`assert '只查了当前会话' in ...` —— 重置提前发生，工具输出被丢掉，模型那一轮只看到收束指令（真机同一形状见 `p0-turns.json`：第 5 步重置后回到「先列项目根目录」） |

还原之后的复核（同一条命令）：

```
389 passed in 40.69s   # tests\cli\test_prompt_for_turn.py + tests\agent
                       # + tests\context + tests\test_limits.py + tests\test_prompt_mechanics.py
```

`git diff -- src` 里只剩注释里的「变异对照」四个字（说明文字），**没有**任何变异代码残留。
