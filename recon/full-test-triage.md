# 全量测试基线 triage（P2.3）

基线：`5769f86`（P2.2b）。本阶段只处理 `tests/agent` 与 `tests/tools` 的失败，不加功能，不改任务流、界面和主题。

复现命令（Windows 临时目录与沙箱插件，否则会整批 `FileNotFound`，不是这 15 条）：

```text
PYTEST_DEBUG_TEMPROOT=D:/workSpace/agents_dev/.tmp/ptroot-known
PYTHONPATH=src;scripts
python -m pytest -q tests/agent tests/tools --tb=short -p no:cacheprovider -p pytest_sandbox_compat
```

复现结果：15 failed，980 passed。

## 分类

| # | 测试 | 分类 | 处置 |
| --- | --- | --- | --- |
| 1 | `tests/agent/test_decisions.py::test_下一节能看见上一节的结论且正文不进提示` | stale-test | 断言改到承载节指令的 user 消息 |
| 2 | `tests/agent/test_p3_budget_wire.py::test_响应后记下实测且次轮用它算预算` | stale-test | 第二次 `list_dir` 换路径，两步都计入预算 |
| 3 | `tests/agent/test_supervise_loop.py::test_撞上限时督导续期而不是停` | stale-test | 两步用不同目录，结论脚本对准督导 |
| 4 | `tests/agent/test_supervise_loop.py::test_督导说收手就以它的理由收尾` | stale-test | 同上 |
| 5 | `tests/agent/test_supervise_loop.py::test_督导不可用时退回原行为` | stale-test | 同上；脚本在督导那一跳耗尽 |
| 6 | `tests/agent/test_supervise_loop.py::test_关掉督导就回到硬上限` | stale-test | 两步都计入后，关掉督导仍是恰好 2 次调用 |
| 7 | `tests/agent/test_supervise_loop.py::test_续期不会越过总上限` | stale-test | 续期脚本按「第一步后问、顶到天花板再问」重排 |
| 8 | `tests/agent/test_supervise_loop.py::test_督导那次调用也算进用量` | stale-test | 同 3；结论能解析时这次调用仍进用量 |
| 9 | `tests/agent/test_supervise_loop.py::test_督导建议要求表态且允许带理由拒绝` | stale-test | 第三次相同调用停用组合，下一条才是督导结论 |
| 10 | `tests/agent/test_supervise_loop.py::test_督导判可收尾时由模型写结论` | stale-test | 同 3 |
| 11 | `tests/agent/test_supervise_loop.py::test_督导交还后它仍调工具则不执行并沿用原话` | stale-test | 同 3 |
| 12 | `tests/agent/test_supervise_loop.py::test_督导说完成后按成功收尾` | stale-test | 同 3 |
| 13 | `tests/agent/test_supervise_loop.py::test_督导说完成时带上它给的话` | stale-test | 同 3 |
| 14 | `tests/agent/test_supervise_loop.py::test_结论里的整份源码不会当成已经落盘` | stale-test | 注册写工具，落盘退回才适用 |
| 15 | `tests/tools/test_dispatch.py::test_派发那一趟的账记进主循环` | real-bug | 记账包装同时数 `chat_stream`，并补回归 |

环境项：无。这 15 条都不是缺 `rg`、无模型、临时目录权限或网络。

## 失败原文

### 1. 下一节能看见上一节的结论且正文不进提示

断言：`assert "决定：" in gateway.requests[0].messages[0].content`

实际：system 只有目标、大纲和已定约束：

```text
目标：写清楚

大纲：
1. 背景｜要点｜400
2. 问题｜要点｜400
3. 方案｜要点｜400

已定约束：
- 术语用窗口
```

`sections.SECTION_INSTRUCTION` 仍要求节末写「决定：」，但 `generate_section` 把本节要求放在 user，system 只放大纲和约束。去重、40 字和正文不进约束的断言没有失败。

### 2. 响应后记下实测且次轮用它算预算

断言未执行到。`FakeModel.chat` 抛出 `RuntimeError: 假模型脚本已耗尽，请补充足够的应答条目`。日志：`拿不到 prompt_tokens，按消息长度估算 n_past=632`（第一次调用时还没有实测，属预算估算，不是失败原因）。

脚本只有两条，且都是 `list_dir "."`。第二次被重复护栏判为无用重复，调用未执行，步数退还，循环继续要第三条。

### 3–8、10–13. 督导接点（同一根因）

代表断言与实际：

- `test_撞上限时督导续期而不是停`：`assert result.finished is True`，实际 `finished=False`，`final='任务没做完，收手的原因：督导没能给出结论（调用失败或结论不合格式）'`。
- `test_督导说收手就以它的理由收尾`：`assert "缺一个读不到的目录" in result.final`，实际仍是上面那句督导失败话。
- `test_督导不可用时退回原行为`、`test_关掉督导就回到硬上限`、`test_续期不会越过总上限`：脚本耗尽，`RuntimeError: 假模型脚本已耗尽`。关掉督导时期望 `final == "已达步数上限，任务未完成"` 且恰好 2 次调用。
- `test_督导那次调用也算进用量`：`assert result.model_calls == len(loop.gateway.requests)`，实际 `3 == 4`。多出来的那次是督导调用，但喂给它的不是结论 JSON，`verdict is None`，所以没记入 `model_calls`。
- `test_督导判可收尾时由模型写结论`：`assert result.model_final == written`，实际 `''`。
- `test_督导交还后它仍调工具则不执行并沿用原话`、`test_督导说完成后按成功收尾`、`test_督导说完成时带上它给的话`：`assert result.finished is True`，实际 `False`，收手原因同上。

轨迹（续期用例）：第二次 `list_dir "."` 被判 `kind=useless`，步数退还；下一条 `{"action":"extend",...}` 被主循环解析成「缺少字符串字段 thought」；督导真正被叫起时脚本已经错位，返回 None。

换两个不同目录后，同一条结论脚本能被督导解析：续期、收手理由、耗尽后退回、关掉后硬上限、用量相等、finish 后由模型写结论，这些断言都成立。产品解析和续期夹紧没有错。

### 9. 督导建议要求表态且允许带理由拒绝

断言：`assert "不接受是允许的" in prompt`（前面三条「别再看目录了」「建议 / 不是命令」「assessment」已通过）。

实际：拼接后的提示里没有这句。`三选一` 也不在。`SUPERVISOR_ASK` 仍包含「不接受是允许的」，但这次没有注入。

轨迹：第三次相同 `list_dir` 把组合停用并叫起督导，此时脚本下一条仍是第四次 `list_dir`，不是 redirect。结论 JSON 随后被主循环当成坏回合。前三条断言能过，是因为督导自己的提示和未解析 JSON 里带了那些字，不是因为表态段注入了。

改成三次相同调用后紧跟 redirect，注入恢复，四条断言都在。

### 14. 结论里的整份源码不会当成已经落盘

断言：`assert "不会落盘" in prompt`。

实际提示以「当前没有写工具，直接给结论。」结尾，没有「不会落盘」，也没有 `write_file`。只有 1 次调用，`model_final` 就是贴进去的整份 HTML。

`_bounce_unwritten` 在 `require_landing` 为假时整段跳过。编码目标且注册了写工具才要求落盘。这个夹具只注册了 `list_dir` / `read_file`。注册 `write_file` 之后，`CODE_IN_FINAL` 会退回，磁盘仍是旧文件，第二条结论被收下。

### 15. 派发那一趟的账记进主循环

断言：`assert result.model_calls == 6`。

实际：`3 == 6`。`finished=True`，`final='派发完成'`，`concluded_by='模型完成'`。网关请求是 6 条：主循环 2、实现者 2、审查者 1、判定 1。

`dispatcher._Counting` 只包了 `chat`。实现者和审查者经 `AgentLoop` 走 `chat_stream`（`on_delta` 非空且假模型有该方法），`__getattr__` 把流式调用透传到内层，不计数。判定走 `gateway.chat`，记了 1。主循环自己的 2 次另计。合计 3。注释写的是「包一层则一处不漏」，账是错的。

## 结果

产品 bug：`dispatcher._Counting` 同时记录 `chat` 与 `chat_stream`。回归：`tests/tools/test_dispatch.py::test_流式调用也记进派发的账`。集成断言仍是主循环 2 次加子智能体 4 次，合计 6。

过期测试：

- `test_decisions.py`：`决定：` 改到本节 user 指令。
- `test_p3_budget_wire.py`：第二次 `list_dir` 改为 `sub`。
- `test_supervise_loop.py`：预算步用不同目录；续期脚本按天花板重排；表态用例在第三次相同调用后放结论；源码用例注册 `write_file`。原断言都保留。

剩余环境项：无。

全量：`tests/agent` + `tests/tools` 996 passed（原 980 passed + 15 failed，另加 1 条回归）。

`web_ci`：451 passed。构建哈希未变（`app.03f3536dbfb3.css`、`app.fec6fb5961d3.js`）。

## 不改的产品行为

- 同一路径、清单未变的第二次 `list_dir` 不执行，并退还这一步预算。
- 节指令留在 user，system 只放大纲和已定约束。
- 没有写工具时不要求把结论交给 `write_file`。
- 督导结论解析、续期夹在总上限内、finish 交给模型写 final。
