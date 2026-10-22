"""把会议室的占用时段合成一段一段。

时段用 (开始, 结束) 表示，都是 `HH:MM` 的字符串，字典序与时间序一致。
两条规则：

- 重叠的要并起来；
- **首尾相接的也要并起来**（09:00-10:00 和 10:00-11:00 是同一个人连着占的，
  对排期来说就是 09:00-11:00 一整段）。

返回按开始时间升序排列的列表。
"""


def merge_slots(slots):
    """合并重叠或相接的时段。"""
    merged = []
    for start, end in sorted(slots):
        # 判据是 `<=` 而不是 `<`：`start == 上一段的 end` 就是首尾相接，
        # 也要并起来。用 `<` 会把相接的两段原样留着，前台就会看到
        # 09:00-10:00 与 10:00-11:00 两条，明明是同一个人连着占的。
        if merged and start <= merged[-1][1]:
            last_start, last_end = merged[-1]
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged
