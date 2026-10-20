# 已知失败

记账日期：2026-10-21。分支 `feat/task-flow-contract`，当时 HEAD `bca9464`。本页只记录，不改产品代码，也不把这些用例标成跳过。

命令：

```text
python -m pytest -q tests/agent tests/tools
```

结果：**15 failed，947 passed**。下面 15 条就是全部失败。本机 PATH 上有 `rg`（Cursor 自带），这 15 条都不是「找不到 rg」。

B1 之前：把 `src/spoolkit/tools/types.py` 还原到 `bca9464` 的父提交后，决策、督导续期、派发记账这 3 条仍以同一断言失败。其余 12 条的栈在 `loop.py`、`phase_budget.py`、`fake.py`，不经过 B1 的契约模块。B1 没有改 loop、supervisor、工具实现。

## 决策

| 用例 | 现象 | 原因 |
| --- | --- | --- |
| `tests/agent/test_decisions.py::test_下一节能看见上一节的结论且正文不进提示` | 断言第一条消息含「决定：」，实际 system 消息停在「已定约束：术语用窗口」 | 「决定：」写在 user 消息（`sections._messages`）。B1 前已存在 |

## 预算

| 用例 | 现象 | 原因 |
| --- | --- | --- |
| `tests/agent/test_p3_budget_wire.py::test_响应后记下实测且次轮用它算预算` | 脚本 2 条，第三次 `chat_stream` 抛「假模型脚本已耗尽」 | `loop.py` 模型调用（约 2919 行）经 `chat_with_escalation` 多打了一枪。日志：`phase_budget` 拿不到 `prompt_tokens`，按字数估算 `n_past=632`。`supervise=False`。B1 前已存在 |

## 督导

共同现象：裁决没有按 `{"action": ...}` 执行。一部分收成「督导没能给出结论（调用失败或结论不合格式）」；一部分把裁决 JSON 当成模型回合，提示里出现「缺少字符串字段 thought」；还有三条在脚本用完后异常直接抛出，没有收成 `LoopResult`。

| 用例 | 现象 |
| --- | --- |
| `tests/agent/test_supervise_loop.py::test_撞上限时督导续期而不是停` | `finished` 为假，收尾是「督导没能给出结论」 |
| `tests/agent/test_supervise_loop.py::test_督导说收手就以它的理由收尾` | 收尾里没有「缺一个读不到的目录」，仍是「督导没能给出结论」 |
| `tests/agent/test_supervise_loop.py::test_督导不可用时退回原行为` | 期望接住耗尽并在收尾里写明督导；实际 `fake.py` 抛「假模型脚本已耗尽」 |
| `tests/agent/test_supervise_loop.py::test_关掉督导就回到硬上限` | `supervise=False`，脚本 2 条，第三次 `chat_stream`（`loop.py` 约 2919 行）抛「假模型脚本已耗尽」 |
| `tests/agent/test_supervise_loop.py::test_续期不会越过总上限` | 同上，脚本耗尽，异常抛出 |
| `tests/agent/test_supervise_loop.py::test_督导那次调用也算进用量` | `model_calls` 为 3，网关请求已有 4 条；收尾仍是「督导没能给出结论」 |
| `tests/agent/test_supervise_loop.py::test_督导建议要求表态且允许带理由拒绝` | 提示里是「缺少字符串字段 thought」，没有「不接受是允许的」 |
| `tests/agent/test_supervise_loop.py::test_督导判可收尾时由模型写结论` | 期望的结论句是空字符串 |
| `tests/agent/test_supervise_loop.py::test_督导交还后它仍调工具则不执行并沿用原话` | `finished` 为假，收尾是「督导没能给出结论」 |
| `tests/agent/test_supervise_loop.py::test_督导说完成后按成功收尾` | 同上 |
| `tests/agent/test_supervise_loop.py::test_督导说完成时带上它给的话` | 同上 |
| `tests/agent/test_supervise_loop.py::test_结论里的整份源码不会当成已经落盘` | 提示里是「缺少字符串字段 thought」，没有「不会落盘」 |

这 12 条在 B1 前已存在。`test_撞上限时督导续期而不是停` 在还原 `types.py` 后复测过。

## 派发

| 用例 | 现象 | 原因 |
| --- | --- | --- |
| `tests/tools/test_dispatch.py::test_派发那一趟的账记进主循环` | `model_calls` 为 3，期望 6 | 子智能体那一趟没有记进主循环。B1 前已存在（还原 `types.py` 后同一断言） |
