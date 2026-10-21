# 已知失败

更新：2026-10-22。分支 `feat/task-flow-contract`。15 条已全部处理，没有剩余失败，也没有标成跳过。

分类：14 条 stale-test，1 条 real-bug。证据与处置在 `recon/full-test-triage.md`。

命令：

```text
python -m pytest -q tests/agent tests/tools
```

当前结果：**996 passed**。

| # | 用例 | 分类 |
| --- | --- | --- |
| 1 | `tests/agent/test_decisions.py::test_下一节能看见上一节的结论且正文不进提示` | stale-test |
| 2 | `tests/agent/test_p3_budget_wire.py::test_响应后记下实测且次轮用它算预算` | stale-test |
| 3 | `tests/agent/test_supervise_loop.py::test_撞上限时督导续期而不是停` | stale-test |
| 4 | `tests/agent/test_supervise_loop.py::test_督导说收手就以它的理由收尾` | stale-test |
| 5 | `tests/agent/test_supervise_loop.py::test_督导不可用时退回原行为` | stale-test |
| 6 | `tests/agent/test_supervise_loop.py::test_关掉督导就回到硬上限` | stale-test |
| 7 | `tests/agent/test_supervise_loop.py::test_续期不会越过总上限` | stale-test |
| 8 | `tests/agent/test_supervise_loop.py::test_督导那次调用也算进用量` | stale-test |
| 9 | `tests/agent/test_supervise_loop.py::test_督导建议要求表态且允许带理由拒绝` | stale-test |
| 10 | `tests/agent/test_supervise_loop.py::test_督导判可收尾时由模型写结论` | stale-test |
| 11 | `tests/agent/test_supervise_loop.py::test_督导交还后它仍调工具则不执行并沿用原话` | stale-test |
| 12 | `tests/agent/test_supervise_loop.py::test_督导说完成后按成功收尾` | stale-test |
| 13 | `tests/agent/test_supervise_loop.py::test_督导说完成时带上它给的话` | stale-test |
| 14 | `tests/agent/test_supervise_loop.py::test_结论里的整份源码不会当成已经落盘` | stale-test |
| 15 | `tests/tools/test_dispatch.py::test_派发那一趟的账记进主循环` | real-bug |

real-bug 是派发记账没数 `chat_stream`。其余 14 条是夹具与当前协议、重复护栏或提示位置不一致，断言都保留。
