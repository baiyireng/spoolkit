# 任务卡审计

脚本回合，不是真机采样。`FakeModel`，只注册 `list_dir` 和 `read_file`，`context_window=8192`，目标「写一份测试缺口的盘点报告。」。抓的是 `_assemble` 放进 system 的 `task_state` 原文。

## 同一轮

| 项 | 原文 |
|---|---|
| 第 1 轮送出的 task_state | `目标: 写一份测试缺口的盘点报告。` |
| 同轮模型输出 | `state.current=列出根目录`，`state.verify=确认有没有测试目录`，`state.hypothesis=根目录能看出测试在哪`，调用 `list_dir(.)` |
| 第 2 轮送出的 task_state | `目标: 写一份测试缺口的盘点报告。` / `当前: 列出根目录` / `待验证: 确认有没有测试目录` / `下一步假设: 根目录能看出测试在哪` |
| 同轮模型输出 | `state=null`，`final=根目录是空的，没有测试可盘点。` |

`current` / `verify` 只在模型给出 `StateDelta` 之后写上，下一轮原文与增量一致。模型写 `state=null` 时，循环不另填这两个字段。

## 历史装不下之后

同一份已更新的 `TaskState`，`_assemble` 另送 8 条长工具消息。装入 6 条，区段 `dropped` 为空。`task_state` 仍是上面第 2 轮那四行。

## 结论

够用。`goal` 每轮都在。`current` / `verify` 由 `StateDelta` 更新，压缩历史不丢掉任务卡。不需要新字段，也不需要改渲染。
