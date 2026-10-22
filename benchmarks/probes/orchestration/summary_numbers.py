"""把这一轮（真模型、三臂）的读数打成一份可以贴的汇总（报告用）。"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
P = json.loads((HERE / "real-readings.json").read_text(encoding="utf-8"))
R = P["readings"]


def stats(task, temp, arm, key):
    values = [one[key] for one in R
              if one["task"] == task and one["temperature"] == temp and one["arm"] == arm]
    if not values:
        return "—"
    median = statistics.median(values)
    if len(set(values)) == 1:
        return f"{median:g}"
    return f"{median:g}（{min(values):g}–{max(values):g}）"


print(f"读数条数 {len(R)}；生成 {P['generated_at']}；跑批耗时 {P['elapsed_seconds']}s")
print("每跑一次的版本/装配：", sorted({(one["head"], one["tool_count"], one["system_tokens"]) for one in R}))
print("完成（模型自己 done=True）的运行：", [(one["label"], one["steps"]) for one in R if one["finished"]])
print("orchestrate 调用总数：", sum(one["orchestrate_calls"] for one in R),
      "；子调用总数：", sum(one["subcalls"] for one in R),
      "；plan 合法/被拒：",
      sum(one["orchestrate_plans_ok"] for one in R),
      sum(one["orchestrate_plans_rejected"] for one in R))
print("上下文重置（丢历史重发）次数分布：",
      {n: sum(1 for one in R if one["resets"] == n) for n in sorted({one["resets"] for one in R})})
print("模型自己一步最多几个调用：", max(one["model_max_calls_in_turn"] for one in R))
print()
for task in ("A-todo", "B-failing-test"):
    for temp in (0.0, 0.4):
        for arm in ("off", "on", "control"):
            row = f"{task:15s} t{temp} {arm:7s} 步 {stats(task, temp, arm, 'steps'):18s}" \
                  f" 调用 {stats(task, temp, arm, 'model_calls'):18s}" \
                  f" 输入 {stats(task, temp, arm, 'prompt_tokens'):22s}" \
                  f" 输出 {stats(task, temp, arm, 'completion_tokens'):20s}" \
                  f" 工具 {stats(task, temp, arm, 'tool_calls'):16s}"
            print(row)
        print()
