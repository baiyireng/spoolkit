# protocol.v2 开关与回滚

默认关闭。打开后新开的一轮才生效；进行中的一轮不改协议。

| 项 | 值 |
|---|---|
| 旋钮 | `protocol_v2`（方案里的 `protocol.v2`） |
| 默认 | `0`：控制回合仍是带 `final` 的统一 JSON |
| 打开 | 工作区 `.agent/limits.json` 写 `{"protocol_v2": 1}`，然后新开一轮 |
| 回滚 | 把该键改成 `0`，或删掉该键，然后新开一轮 |
| 不要 | 删旧解析；把代码默认改成 `1`；在 json 里写 `true`（布尔会被忽略，等于没设） |

| 开关 | 控制回合 | 终稿 |
|---|---|---|
| `0` | JSON，`done` 时要 `final` | 从 `final` 取正文，不发第二次请求 |
| `1` | JSON，无 `final`。连续两次旧格式后，这一轮降回旧解析并打日志 | `done` 后另发纯文本终稿 |

回滚后确认：

```text
python -c "from spoolkit.context.phase_budget import protocol_v2_enabled; print(protocol_v2_enabled())"
```

打印 `False`。短答仍走 `final` 字段。
