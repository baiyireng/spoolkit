# 题目 7 第 4 次 list_dir(.) 护栏打点

重放条件与 `scripts/p3_debug_turns.py` 相同：temperature 0、seed 1、`protocol_v2=1`、只注册 `list_dir` 和 `read_file`、空临时目录。第 4 条控制响应是 `list_dir(.)`，trace 步号是 step3。

```text
step3 list_dir(.) level=2 gate=True kind=refresh cause=changed lost=False pending_changed=False
observed=da39a3ee5e6b current=d2c277b75e9f
结论：放行原因是 other（对象指纹变了，cause=changed）
```

三元组：`kind=refresh`，`cause=changed`，理由是「这次要看的对象与上次执行时不一样了，旧结果描述的是旧内容」。

`lost=False`，`pending_changed=False`，`gate=True`。`observed` 是空清单的指纹（空串 sha1 的前 12 位），`current` 与它不同，所以没有走压缩放行，也没有走待确认清单放行。

## 清单差异

只读 trace，不改判据。同一条重放里，第 4 次 `list_dir(.)` 的 `_listing_of(".")` 前 5 项：

| 项 | 值 |
|---|---|
| 调用 | 第 4 次响应，trace step3，`list_dir(.)` |
| listing 前 5 项 | `.agent/` |
| observed | `da39a3ee5e6b`（空清单） |
| current | `d2c277b75e9f` |

清单从空变成只有 `.agent/`。指纹变化来自运行时目录 `.agent`。
