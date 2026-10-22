# slots

会议室占用时段的合并逻辑。前台排期页用的就是它。

```
python -m unittest discover -s tests
```

时段都是 `HH:MM` 字符串。规则写在 `slots/merge.py` 的模块说明里：
重叠的并起来，**首尾相接的也并起来**。
