"""P0 取证（第二份）：从真实现场反推「预算被放大」的证据，并数清各机制。

只读。输入还是 `real-readings.json` 与沙箱（路径在读数里）。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
P = json.loads((HERE / "real-readings.json").read_text(encoding="utf-8"))
R = P["readings"]

print("== 每条运行的 步数 vs 配置上限（放大证据）==")
for one in sorted(R, key=lambda x: (x["task"], x["arm"], x["label"])):
    over = one["steps"] - one["max_steps"]
    mark = f"  ← 超 {over} 步（放大后跑了 {one['steps']}）" if over > 0 else ""
    print(f"{one['label']:30s} max_steps={one['max_steps']:2d} 实跑={one['steps']:2d} "
          f"finished={one['finished']!s:5s} resets={one['resets']}{mark}")

print()
print("== 撞上限（finished=False 且 final 是判词）的条数 ==")
VERDICT = ("已达步数上限，任务未完成", "任务没做完，收手的原因",
           "模型在提出改动后停止产出可执行的回合")
buckets = Counter()
for one in R:
    final = one["final"] or ""
    if one["finished"]:
        key = "模型完成"
    elif final.startswith("已达步数上限"):
        key = "步数上限"
    elif final.startswith("任务没做完"):
        key = "收手（督导/其它）"
    elif final.startswith("模型在提出改动后停止"):
        key = "空回合中断"
    else:
        key = "运行失败/被拦（未完成）"
    buckets[key] += 1
for k, v in buckets.most_common():
    print(f"   {k:22s} {v}")

print()
print("== 上下文重置：发生在哪些条、重置前后步数 ==")
for one in sorted(R, key=lambda x: x["label"]):
    if one["resets"]:
        print(f"{one['label']:30s} resets={one['resets']} 总步={one['steps']} "
              f"输入tok={one['prompt_tokens']} finished={one['finished']}")

print()
print("== 搜索/调用失败循环的形状：同一工具连续出现 ≥3 次 ==")
for one in sorted(R, key=lambda x: x["label"]):
    names = one["tool_names"]
    best = run = 0
    tool = ""
    prev = None
    for n in names:
        if n == prev:
            run += 1
        else:
            run = 1
            prev = n
        if run > best:
            best, tool = run, n
    if best >= 3:
        print(f"{one['label']:30s} 最长连续同名 {tool} × {best}（共 {len(names)} 次调用）")

print()
print("== 模型最后两个回合在干什么（turns 里 done/调用数）==")
tail_counter = Counter()
for one in R:
    turns = one["model_turns"]
    if not turns:
        continue
    last = turns[-1]
    tail_counter[(bool(last.get("done")), last.get("tool_calls"))] += 1
for key, v in sorted(tail_counter.items(), key=lambda kv: -kv[1]):
    print(f"   末回合 done={key[0]} 调用数={key[1]}: {v} 条")
